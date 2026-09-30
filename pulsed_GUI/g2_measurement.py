"""Pulsed T3 / CW T2 two-detector recording with optional NI clock/static AO."""
import ctypes as ct
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from figure_options import FIELD as DPI_FIELD, validated_dpi
from measurement_metadata import FORM as MEASUREMENT_FORM, read as measurement_context
import threading
import time

from ph330 import PH330
import ph330_acquire as ph
from laser_clock import clock_plan
from laser_clock_eom import prepared_clock, eom_plan
from pulsed_polarization import FORM as PH_FORM, INTERNAL_SOURCES
from record_io import save_json
import g2_analysis

TOOL = "Pulsed / CW g2"
CW = "CW (manual laser)"
NI = "NI external"
MANUAL = "Pulsed external source (manual)"
HELP = ("Two-detector full-file correlation: T3 for pulsed 500 kHz–80 MHz; T2 for CW. "
        "PBS outputs measure polarization-resolved cross-correlation, not automatically source HBT g². "
        "Raw timestamps are retained; reanalyze delay range/binning offline. CW requires no periodic signal on PicoHarp SYNC. "
        "Close UniHarp. Optional NI controls own Ctr0 and/or AO0/AO1 until Stop.")
FORM = [
    ("action", "Action", "Check settings", ["Check settings", "Record", "Analyze saved run"]),
    ("source", "Laser source", NI, [NI, *INTERNAL_SOURCES, MANUAL, CW]),
    ("seconds", "Recording duration (s)", "60", None),
    ("laser-hz", "NI / manual external repetition rate (Hz)", "500000", None),
    ("high-ns", "NI trigger width (ns; NI external only)", "100", None),
    ("warmup-s", "Wait before recording / laser warm-up (s)", "1", None),
    ("manual-ready", "Manual mode selected on laser; NI PFI12 disconnected", "No", ["No", "Yes"]),
    ("geometry", "Optical detector geometry (saved with every run)", "QKD PBS outputs", ["QKD PBS outputs", "Non-polarizing HBT beamsplitter"]),
    ("device", "NI device (if clock or static EOM control enabled)", "Dev1", None),
    ("control-eoms", "Set and hold both EOM voltages during recording?", "No", ["No", "Yes"]),
    ("eom1-v", "Alice / AO0 target (V; optional static control)", "0", None),
    ("eom2-v", "Bob / AO1 target (V; optional static control)", "0", None),
    *[item for item in PH_FORM if item[0] in ("dll", "device-index", "serial", "binning")
      or item[0].startswith(("sync-", "ch1-", "ch2-"))],
    ("corr-halfwidth-ns", "Correlation ±half-range (ns; 0 = automatic)", "0", None),
    ("corr-bin-ns", "Correlation bin width (ns)", "2", None),
    ("corr-zero-ns", "Pulsed central-peak position (ns; not another channel offset)", "0", None),
    ("peak-halfwidth-ns", "Pulsed peak-area ±half-width (ns; 0 = 0.4 periods)", "0", None),
    ("reference-first", "First reference side-peak index (both ± sides)", "2", None),
    ("reference-last", "Last reference side-peak index (both ± sides)", "5", None),
    ("gate", "Offline arrival gate on both detectors (pulsed only)", "No", ["No", "Yes"]),
    ("gate-start-ns", "Arrival gate start after SYNC (ns, inclusive)", "0", None),
    ("gate-stop-ns", "Arrival gate stop after SYNC (ns, exclusive)", "100", None),
    ("max-pairs", "Maximum pair computations before stopping analysis", "50000000", None),
    ("folder", "Saved g2 recording folder (Analyze saved run)", "", "directory"),
    ("output", "Raw recordings and analyses directory", str(Path(__file__).resolve().parents[1]/"data"/"g2"), "directory"),
    DPI_FIELD,
    *MEASUREMENT_FORM,
    ("note", "Emitter / polarization / power / optical path notes", "", None),
]


def settings(values):
    def number(key, low, high):
        x = float(values[key])
        if not math.isfinite(x) or not low <= x <= high:
            raise ValueError(f"{key} must be within {low:g}..{high:g}.")
        return x
    source = values["source"]
    if source not in (NI, MANUAL, CW, *INTERNAL_SOURCES):
        raise ValueError("Select a laser source.")
    seconds = number("seconds", .1, 36000)
    mode, clock = (2 if source == CW else 3), None
    hz = INTERNAL_SOURCES.get(source, 500000)
    if source in (NI, MANUAL):
        hz = number("laser-hz", 500000, 1000000 if source == NI else 80000000)
    if source == NI:
        clock = clock_plan(hz, number("high-ns", 20, 600))
        hz = clock["realized_nominal_frequency_hz"]
    def trigger(prefix):
        result = dict(mode=values[prefix+"-mode"], edge=values[prefix+"-edge"],
                      level_mv=int(values[prefix+"-level-mv"]),
                      offset_ps=round(number(prefix+"-offset-ns", -99, 99)*1000))
        if result["mode"] == "cfd":
            result["zero_cross_mv"] = int(values[prefix+"-zero"])
        return result
    config = dict(device_index=int(values["device-index"]), serial=values["serial"].strip(), laser_hz=hz,
                  sync_divider=1, binning=int(values["binning"]), sync=trigger("sync"),
                  detectors=[dict(trigger("ch1"), channel=0), dict(trigger("ch2"), channel=1)])
    ph.validate(config, expected_laser_hz=None)
    device = values["device"].strip()
    if not device or any(c in device for c in "/\\,:"):
        raise ValueError("Enter a NI device name such as Dev1.")
    if values["control-eoms"] not in ("No", "Yes"):
        raise ValueError("Select optional EOM control.")
    if values["geometry"] not in ("QKD PBS outputs", "Non-polarizing HBT beamsplitter"):
        raise ValueError("Select optical detector geometry.")
    if not values["output"].strip():
        raise ValueError("Select a data directory.")
    opts = g2_analysis.options(values)
    if mode == 2 and opts["gate_ps"]:
        raise ValueError("CW has no excitation-relative arrival gate; select Gate = No.")
    return dict(source=source, mode=mode, clock=clock, device=device,
                eom=eom_plan(values["eom1-v"], values["eom2-v"]) if values["control-eoms"] == "Yes" else None,
                duration_ms=round(seconds*1000), warmup_s=number("warmup-s", .2, 3600),
                ph330=config, dll=values["dll"], output=values["output"], note=values["note"], measurement=measurement_context(values),
                geometry=values["geometry"], analysis=opts)


def record(cfg, stop, *, daq=None, system=None, constants=None, api=None):
    if stop.is_set():
        raise KeyboardInterrupt
    folder = Path(cfg["output"]) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "metadata.json"
    meta = dict(schema="qkd-g2-tttr-v1", mode=cfg["mode"], settings=cfg, config=cfg["ph330"],
                status="preparing", complete=False, records=0, flags_seen=0, cleanup_errors=[],
                ao_zeroed_on_exit=False, requested_duration_ms=cfg["duration_ms"],
                host_start_utc=datetime.now(timezone.utc).isoformat(),
                raw_format="GenericT3" if cfg["mode"] == 3 else "GenericT2",
                raw_dtype="little-endian uint32")
    save_json(path, meta)
    ao = clock = stream = None
    opened = started = reserved = clock_started = False
    index = cfg["ph330"]["device_index"]
    failure = None
    try:
        if cfg["clock"] or cfg["eom"]:
            if daq is None:
                import nidaqmx as daq
                from nidaqmx import constants
                from nidaqmx.system import System
                system = System.local()
            device = system.devices[cfg["device"]]
            if device.product_type != "USB-6351" or device.dev_is_simulated:
                raise RuntimeError("NI output requires a physical USB-6351.")
            if cfg["eom"]:
                ao = daq.Task()
                ao.ao_channels.add_ao_voltage_chan(f"{cfg['device']}/ao0:1", min_val=-10, max_val=10)
                ao.control(constants.TaskMode.TASK_VERIFY)
                ao.control(constants.TaskMode.TASK_COMMIT)
                reserved = True
            if cfg["clock"]:
                clock = prepared_clock(daq, constants, cfg["device"], cfg["clock"])
        api = api if api is not None else PH330(Path(cfg["dll"]))
        ph.bind_acquisition(api)
        serial = ct.create_string_buffer(8)
        api.call("OpenDevice", index, serial)
        opened = True
        if serial.value.decode() != cfg["ph330"]["serial"]:
            raise RuntimeError("PicoHarp serial differs from configured device.")
        meta["hardware"] = ph.configure(api, cfg["ph330"], tttr_mode=cfg["mode"])
        meta["dll_version"] = api.version
        api.call("SetOflCompression", index, 2)  # Prevent large empty T2 overflow streams; decoder handles multiplicity.
        if stop.is_set():
            raise KeyboardInterrupt
        if ao is not None:
            ao.write(cfg["eom"]["daq_v"], auto_start=True)
        if clock is not None:
            clock.start()
            clock_started = True
        if stop.wait(cfg["warmup_s"]):
            raise KeyboardInterrupt
        before = ph.rates(api, index, meta["hardware"]["input_count"])
        meta["rates_before"] = before
        if cfg["mode"] == 2 and before["sync_hz"] > 1000:
            raise ValueError("CW/T2 selected but periodic SYNC is present. Set laser CW; disconnect BDL TRG OUT from PicoHarp SYNC if it still pulses.")
        if cfg["mode"] == 3 and abs(before["sync_hz"] / cfg["ph330"]["laser_hz"] - 1) > .05:
            raise ValueError("Measured SYNC does not match selected pulsed repetition rate.")
        raw_name = "events.t3raw" if cfg["mode"] == 3 else "events.t2raw"
        meta["raw_file"] = raw_name
        stream = (folder / raw_name).open("xb")
        if stop.is_set():
            raise KeyboardInterrupt
        api.call("StartMeas", index, cfg["duration_ms"])
        started = True
        if cfg["mode"] == 3:
            if stop.wait(.001):
                raise KeyboardInterrupt
            period = ph.scalar(api, index, "GetSyncPeriod", ct.c_double)
            if not math.isfinite(period) or period <= 0 or abs(period * cfg["ph330"]["laser_hz"] - 1) > .05:
                raise ValueError("Measured SYNC period does not match the laser rate.")
            meta["measured_sync_period_s"] = period
        meta["status"] = "running"
        save_json(path, meta)
        print(f"Acquisition record: {path.resolve()}", flush=True)
        print(f"Recording T{cfg['mode']} for {cfg['duration_ms']/1000:g} s; {cfg['geometry']}.", flush=True)
        buffer, actual = (ct.c_uint32 * ph.TTREADMAX)(), ct.c_int()
        empty = 0
        origin = time.monotonic()
        progress = origin
        bad_flags = ph.BAD_FLAGS if cfg["mode"] == 3 else ph.BAD_FLAGS & ~4
        while True:
            if stop.is_set():
                raise KeyboardInterrupt
            if clock is not None and clock.is_task_done():
                raise RuntimeError("NI laser trigger stopped unexpectedly.")
            flags = ph.scalar(api, index, "GetFlags")
            meta["flags_seen"] |= flags
            if flags & bad_flags:
                raise RuntimeError(f"PicoHarp error/data-loss flags: 0x{flags:x}.")
            api.call("ReadFiFo", index, buffer, ct.byref(actual))
            if not 0 <= actual.value <= ph.TTREADMAX:
                raise RuntimeError("Invalid FIFO record count.")
            if actual.value:
                block = ct.string_at(buffer, 4 * actual.value)
                if stream.write(block) != len(block):
                    raise OSError("Short raw-file write.")
                meta["records"] += actual.value
                empty = 0
            elif ph.scalar(api, index, "CTCStatus"):
                empty += 1
                if empty >= 6:
                    break
            now = time.monotonic()
            if now > origin + cfg["duration_ms"]/1000 + 15:
                raise TimeoutError("Acquisition/FIFO drain exceeded expected duration.")
            if now >= progress:
                print(f"Saved {meta['records']:,} raw records; {now-origin:.1f} s host elapsed.", flush=True)
                progress = now + 2
            if not actual.value:
                stop.wait(.001)
        meta["elapsed_ms"] = ph.scalar(api, index, "GetElapsedMeasTime", ct.c_double)
        if not math.isfinite(meta["elapsed_ms"]) or meta["elapsed_ms"] <= 0:
            raise ValueError("Invalid PicoHarp measurement duration.")
        meta["rates_after"] = ph.rates(api, index, meta["hardware"]["input_count"])
        if cfg["mode"] == 3 and abs(meta["rates_after"]["sync_hz"] / cfg["ph330"]["laser_hz"] - 1) > .05:
            raise ValueError("SYNC rate changed during the recording.")
        meta["flags_seen"] |= ph.scalar(api, index, "GetFlags")
        if meta["flags_seen"] & bad_flags:
            raise RuntimeError("Final flags indicate invalid data.")
        meta.update(status="completed", complete=True)
    except BaseException as exc:
        failure = exc
        meta.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", complete=False, error=str(exc))
    finally:
        def cleanup(name, operation):
            try:
                operation()
                return True
            except Exception as exc:
                meta["cleanup_errors"].append(f"{name}: {exc}")
                return False
        if started:
            cleanup("StopMeas", lambda: api.call("StopMeas", index))
        if clock is not None:
            if clock_started:
                cleanup("stop NI clock", clock.stop)
            cleanup("close NI clock", clock.close)
        if ao is not None:
            if reserved:
                meta["ao_zeroed_on_exit"] = cleanup("zero AO", lambda: ao.write([0., 0.], auto_start=True))
            cleanup("close AO", ao.close)
        if opened:
            cleanup("close PicoHarp", lambda: api.call("CloseDevice", index))
        if stream is not None:
            cleanup("close raw file", stream.close)
        if meta["cleanup_errors"]:
            meta.update(status="cleanup_error", complete=False)
            failure = RuntimeError("; ".join(meta["cleanup_errors"]))
        meta["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(path, meta)
        print(f"Acquisition record: {path.resolve()}", flush=True)
    if failure is not None:
        raise failure
    return folder


def main(values, stop_event=None):
    stop = stop_event if stop_event is not None else threading.Event()
    try:
        opts = g2_analysis.options(values)
        if values["action"] == "Analyze saved run":
            if not values["folder"].strip():
                raise ValueError("Select a saved g2 recording folder.")
            g2_analysis.analyze(values["folder"], opts, stop)
            return 0
        cfg = settings(values)
        if values["action"] == "Check settings":
            print(json.dumps(cfg, indent=2))
            print("Settings checked offline. No hardware accessed.")
            return 0
        if values["action"] != "Record":
            raise ValueError("Unknown action.")
        if cfg["source"] != NI and values["manual-ready"] != "Yes":
            raise ValueError("Set the requested manual laser mode, disconnect NI PFI12, then confirm in the Run tab.")
        folder = record(cfg, stop)
        print("Raw recording saved; starting full-file correlation. Stop can cancel analysis without deleting raw data.", flush=True)
        g2_analysis.analyze(folder, opts, stop)
        return 0
    except KeyboardInterrupt:
        print("Stopped. Incomplete acquisitions remain marked incomplete; completed raw recordings remain reusable.")
        return 130
    except Exception as exc:
        print(f"g2: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch(TOOL)
