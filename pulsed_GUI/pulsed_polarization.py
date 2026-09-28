"""Eight static Alice/Bob settings, with two-channel PH330 T3 at each setting.

Run this file without arguments to open the parameter GUI. This is a static
optical baseline, not a pulse-indexed or random BB84 sequencer.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading

from laser_clock import clock_plan
from laser_clock_eom import eom_plan, prepared_clock
from ph330 import PH330, DEFAULT_DLL
import ph330_acquire as acquisition
from record_io import save_json

ROOT = Path(__file__).resolve().parent
STATES = ("H", "V", "R", "L")
INDEX_STATES = ("S0", "S1", "S2", "S3")
BASES = ("HV", "RL")
INTERNAL_SOURCES = {f"Laser internal {rate} MHz": rate * 1000000 for rate in (2, 20, 80)}
HELP_TEXT = (
    "Static baseline: 4 Alice states x 2 Bob bases, both detectors at each setting. "
    "Enter calibrated EOM target volts (-20 gain), not DAQ volts. Alice S0/S1/S2/S3 follow notebook array order; "
    "assign H/V/R/L labels only when confirmed. Close UniHarp and other AO/clock tools. "
    "Internal laser mode requires manual PFI12 disconnection and rate selection. "
    "Raw T3 is always ungated. Stop saves partial data and returns AO0/AO1 to 0 V."
)
FORM = [
    ("action", "Action", "Check settings", ["Check settings", "Record", "Analyze saved run"]),
    ("seconds", "Seconds at EACH voltage pair (8 recordings total)", "60", None),
    ("device", "NI device (USB-6351)", "Dev1", None),
    ("source", "Laser source / bypass NI trigger for troubleshooting", "NI external", ["NI external", *INTERNAL_SOURCES]),
    ("laser-hz", "External laser rate (Hz); internal rate is set by source above", "500000", None),
    ("internal-ready", "Internal mode: I disconnected PFI12 and selected that rate on the laser", "No", ["No", "Yes"]),
    ("high-ns", "NI trigger high time (ns; external only)", "100", None),
    ("warmup-s", "Laser warm-up before first recording (s)", "1", None),
    ("settle-ms", "Pause after each static voltage change (ms)", "100", None),
    ("alice-labels", "Alice labels", "Voltage index only", ["Voltage index only", "Assigned H/V/R/L"]),
    *[item for i, voltage in enumerate(("-152", "-50.87", "50.90", "152.69"))
      for item in ((f"alice-{i}-v", f"Alice notebook entry {i}: EOM target (V)", voltage, None),
                   (f"alice-{i}-state", f"Entry {i} physical state (only if Assigned H/V/R/L)", "", list(STATES)))],
    ("bob-hv-v", "Bob H/V basis target (V; notebook entry 0 candidate)", "47.52", None),
    ("bob-rl-v", "Bob R/L basis target (V; notebook entry 1 candidate)", "-47.44", None),
    ("hv-ch1", "H/V basis: CH1 outcome (CH2 is the other outcome)", "Unassigned", ["Unassigned", "H", "V"]),
    ("rl-ch1", "R/L basis: CH1 outcome (CH2 is the other outcome)", "Unassigned", ["Unassigned", "R", "L"]),
    ("dll", "PicoHarp DLL", str(DEFAULT_DLL), "file"),
    ("device-index", "PicoHarp device index", "0", None),
    ("serial", "PicoHarp serial", "1050578", None),
    ("binning", "T3 binning code (6 = 64 ps on this device)", "6", None),
    *[item for prefix, label, level, offset in (
        ("sync", "SYNC", "-60", "0"), ("ch1", "CH1", "200", "0"), ("ch2", "CH2", "200", "4.5"))
      for item in ((f"{prefix}-mode", f"{label} trigger mode", "edge", ["edge", "cfd"]),
                   (f"{prefix}-edge", f"{label} edge: match your working UniHarp setting", "falling" if prefix == "sync" else "rising", ["rising", "falling"]),
                   (f"{prefix}-level-mv", f"{label} edge threshold (mV, signed)", level, None),
                   (f"{prefix}-zero", f"{label} CFD zero crossing (mV; CFD only)", "-10", None),
                   (f"{prefix}-offset-ns", f"{label} channel offset (ns; applied once in hardware)", offset, None))],
    ("gate", "Optional gate for analysis ONLY", "No", ["No", "Yes"]),
    ("gate-start-ns", "Gate start (ns after SYNC, inclusive)", "0", None),
    ("gate-stop-ns", "Gate end (ns after SYNC, exclusive)", "100", None),
    ("rebin", "Native bins combined for display (1, 2, 4, 8, 16, ...)", "16", None),
    ("plot-stop-ns", "Plot end after SYNC (ns; 0 = full laser period)", "0", None),
    ("output", "Polarization data directory", str(ROOT.parent / "data" / "polarization"), "directory"),
    ("folder", "Session folder with session.json (Analyze only)", "", "directory"),
    ("note", "Calibration source / emitter / optical settings / notes", "", None),
]


def number(values, key, low, high):
    value = float(values[key])
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g}.")
    return value


def analysis_options(values):
    rebin = int(values["rebin"])
    if rebin <= 0 or 32768 % rebin:
        raise ValueError("Display rebin must be a positive divisor of 32768.")
    gate = None
    if values["gate"] == "Yes":
        gate = [number(values, "gate-start-ns", 0, 1e6), number(values, "gate-stop-ns", 0, 1e6)]
        if gate[1] <= gate[0]:
            raise ValueError("Gate end must exceed gate start.")
    return dict(gate=gate, rebin=rebin, plot_stop_ns=number(values, "plot-stop-ns", 0, 1e6))


def settings(values):
    """Validate before any DLL loading or NI task creation."""
    source_choice = values["source"]
    source = "Laser internal" if source_choice in INTERNAL_SOURCES else source_choice
    if source not in ("NI external", "Laser internal"):
        raise ValueError("Select the laser clock source.")
    hz = INTERNAL_SOURCES[source_choice] if source_choice in INTERNAL_SOURCES else number(values, "laser-hz", 1000, 80_000_000)
    clock = None
    if source == "NI external":
        if hz > 1_000_000:
            raise ValueError("This bench profile uses NI triggering up to 1 MHz; higher rates require manual internal mode.")
        clock = clock_plan(hz, number(values, "high-ns", 20, 300000))
        hz = clock["realized_nominal_frequency_hz"]
    elif hz not in (2_000_000, 20_000_000, 80_000_000):
        raise ValueError("Select the internal pulsed laser rate: 2000000, 20000000 or 80000000 Hz.")
    if values["alice-labels"] == "Assigned H/V/R/L":
        labels = [values[f"alice-{i}-state"] for i in range(4)]
        if set(labels) != set(STATES):
            raise ValueError("Assign H, V, R and L exactly once, or select Voltage index only.")
    elif values["alice-labels"] == "Voltage index only":
        labels = INDEX_STATES
    else:
        raise ValueError("Select Alice label mode.")
    voltages = {s: number(values, f"alice-{i}-v", -200, 200) for i, s in enumerate(labels)}
    order = STATES if values["alice-labels"] == "Assigned H/V/R/L" else INDEX_STATES
    alice = {s: voltages[s] for s in order}
    bob = {b: number(values, f"bob-{b.lower()}-v", -200, 200) for b in BASES}
    if len(set(alice.values())) != 4 or len(set(bob.values())) != 2:
        raise ValueError("Provide four distinct Alice voltages and two distinct Bob voltages.")
    mapping = {b: values[f"{b.lower()}-ch1"] for b in BASES}
    if any(mapping[b] not in (*b, "Unassigned") for b in BASES):
        raise ValueError("Invalid detector outcome mapping.")

    def trigger(prefix):
        offset = number(values, prefix + "-offset-ns", -99, 99)
        result = dict(mode=values.get(prefix + "-mode", "edge"), edge=values[prefix + "-edge"],
                    level_mv=int(values[prefix + "-level-mv"]), offset_ps=round(offset * 1000))
        if result["mode"] == "cfd":
            result["zero_cross_mv"] = int(values[prefix + "-zero"])
        return result

    ph = dict(device_index=int(values["device-index"]), serial=values["serial"].strip(),
              laser_hz=hz, sync_divider=1, binning=int(values["binning"]), sync=trigger("sync"),
              detectors=[dict(trigger("ch1"), channel=0, label="CH1 PBS transmitted"),
                         dict(trigger("ch2"), channel=1, label="CH2 PBS reflected")])
    acquisition.validate(ph, expected_laser_hz=None)
    options = analysis_options(values)
    if options["gate"] and options["gate"][1] > 1e9 / hz:
        raise ValueError("Gate must fit inside one laser period.")
    device = values["device"].strip()
    if not device or any(c in device for c in "/\\"):
        raise ValueError("Enter a NI device name such as Dev1.")
    return dict(source=source, device=device, clock=clock, alice=alice, bob=bob, mapping=mapping,
                seconds=number(values, "seconds", 0.1, 360000),
                settle_s=number(values, "settle-ms", 1, 60000) / 1000,
                warmup_s=number(values, "warmup-s", 0, 3600), ph330=ph,
                dll=values["dll"], note=values["note"], analysis=options,
                hv_gain=-20, ao_channels=["ao0", "ao1"], exact_excitation_count=None)


def pause(stop, seconds):
    if stop.wait(seconds):
        raise KeyboardInterrupt


def record(cfg, destination, stop, *, daq=None, constants=None, system=None,
           api_factory=PH330, capture=acquisition.run):
    """Keep laser clock continuous across static blocks; no photons during changes are assigned.

    Optional injected dependencies are for offline tests. We never reset a device
    or clear tasks owned by another process.
    """
    if stop.is_set():
        raise KeyboardInterrupt
    if daq is None:
        import nidaqmx as daq
        from nidaqmx import constants
        from nidaqmx.system import System
        system = System.local()
    device = system.devices[cfg["device"]]
    if device.product_type != "USB-6351" or device.dev_is_simulated:
        raise RuntimeError("Output requires a physical USB-6351.")
    api = api_factory(Path(cfg["dll"]))  # DLL load/version failure before output changes.
    destination.mkdir(parents=True, exist_ok=True)
    folder = destination / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    folder.mkdir()
    path = folder / "session.json"
    session = dict(schema="qkd-static-polarization-v1", status="preparing", complete=False,
                   settings=cfg, device_serial=device.dev_serial_num, blocks=[],
                   ao_zeroed_on_exit=False, cleanup_errors=[],
                   note="Static blocks only. No absolute NI-to-T3 pulse origin claimed.")
    save_json(path, session)
    ao = clock = None
    reserved = False
    clock_started = False
    try:
        ao = daq.Task()
        ao.ao_channels.add_ao_voltage_chan(f"{cfg['device']}/ao0:1", min_val=-10, max_val=10)
        ao.control(constants.TaskMode.TASK_VERIFY)
        ao.control(constants.TaskMode.TASK_COMMIT)
        reserved = True
        if cfg["clock"]:
            clock = prepared_clock(daq, constants, cfg["device"], cfg["clock"])
        session["status"] = "running"
        for state in cfg["alice"]:
            for basis in BASES:
                pause(stop, 0)
                number_ = len(session["blocks"]) + 1
                target = eom_plan(cfg["alice"][state], cfg["bob"][basis])
                block = dict(number=number_, alice=state, bob=basis, status="setting",
                             **target, host_start_utc=datetime.now(timezone.utc).isoformat(),
                             directory=f"{number_:02d}_Alice_{state}_Bob_{basis}")
                session["blocks"].append(block)
                save_json(path, session)
                print(f"Setting {number_}/8: Alice {state} ({cfg['alice'][state]:g} V), "
                      f"Bob {basis} ({cfg['bob'][basis]:g} V).", flush=True)
                ao.write(target["daq_v"], auto_start=True)
                pause(stop, cfg["settle_s"])
                if number_ == 1:
                    if clock:
                        clock.start()
                        clock_started = True
                    print(f"Laser warm-up: {cfg['warmup_s']:g} s.", flush=True)
                    pause(stop, cfg["warmup_s"])
                block["status"] = "acquiring"
                save_json(path, session)
                raw_folder = capture(api, cfg["ph330"], cfg["seconds"], folder / block["directory"],
                                     True, stop_event=stop, allow_dark_channels=True)
                block.update(status="completed", raw_folder=str(raw_folder.relative_to(folder)))
                save_json(path, session)
                from polarization_analysis import histogram
                counts, _, _ = histogram(raw_folder, stop_event=stop)
                block["photon_counts"] = [int(c.sum()) for c in counts]
                print(f"Setting {number_}/8 saved: CH1={block['photon_counts'][0]:,}, "
                      f"CH2={block['photon_counts'][1]:,} photons.", flush=True)
                save_json(path, session)
        session.update(status="completed", complete=True)
    except BaseException as exc:
        session.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                       complete=False, error=str(exc) or type(exc).__name__)
        if session["blocks"] and session["blocks"][-1]["status"] != "completed":
            session["blocks"][-1]["status"] = session["status"]
        raise
    finally:
        # Try every cleanup operation even when a preceding one fails.
        operations = []
        if clock:
            if clock_started:
                operations += [("stop clock", clock.stop)]
            operations += [("close clock", clock.close)]
        if ao:
            if reserved:
                def zero():
                    ao.write([0.0, 0.0], auto_start=True)
                    session["ao_zeroed_on_exit"] = True
                operations += [("zero AO", zero)]
            operations += [("close AO", ao.close)]
        for label, operation in operations:
            try:
                operation()
            except Exception as exc:
                session["cleanup_errors"].append(f"{label}: {exc}")
        if session["cleanup_errors"]:
            session.update(status="cleanup_error", complete=False)
            print("OUTPUT CLEANUP ERROR: " + "; ".join(session["cleanup_errors"]), flush=True)
        session["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(path, session)
        print(f"Run record: {path.resolve()}", flush=True)
        if cfg["source"] == "Laser internal":
            print("Internal laser remains under manual control; this program does not turn it off.", flush=True)
    if session["cleanup_errors"]:
        raise RuntimeError("Output cleanup failed; inspect session.json and hardware state.")
    return folder


def main(values, stop_event=None):
    stop = stop_event if stop_event is not None else threading.Event()
    try:
        from polarization_analysis import analyze
        if values["action"] == "Analyze saved run":
            if not values["folder"].strip():
                raise ValueError("Select the session folder containing session.json.")
            analyze(Path(values["folder"]), **analysis_options(values), stop_event=stop)
            return 0
        cfg = settings(values)
        print(json.dumps(cfg, indent=2), flush=True)
        if values["action"] == "Check settings":
            print(f"Settings valid: 8 static settings, {8 * cfg['seconds']:g} s recording plus pauses. No hardware accessed.")
            if cfg["source"] == "Laser internal":
                print("Before Record: disconnect PFI12 from BDL and select the internal laser rate manually.")
            return 0
        if values["action"] != "Record":
            raise ValueError("Unknown action.")
        if cfg["source"] == "Laser internal" and values["internal-ready"] != "Yes":
            raise ValueError("First disconnect PFI12 from the laser, select its internal rate, and confirm in the form.")
        folder = record(cfg, Path(values["output"]), stop)
        try:
            analyze(folder, **cfg["analysis"], stop_event=stop)
        except Exception:
            print(f"Raw data are saved at {folder.resolve()}. Retry using Analyze saved run.")
            raise
        return 0
    except KeyboardInterrupt:
        print("Stopped. Saved raw records are retained; incomplete blocks are excluded from analysis.")
        return 130
    except Exception as exc:
        print(f"Pulsed polarization: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch("Pulsed polarization")
