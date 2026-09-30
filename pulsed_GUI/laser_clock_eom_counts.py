"""Live PicoHarp alignment with NI Ctr0/PFI12 and static AO0/AO1 biases.

Run this file to open its launcher form. GUI callbacks only enqueue commands;
one worker owns every hardware call. No TTTR measurement is started.
"""
import ctypes as ct
import json
import math
from pathlib import Path
import queue
import threading
import time

from laser_clock import clock_plan
from laser_clock_eom import eom_plan, prepared_clock
from ph330 import PH330, DEFAULT_DLL
import ph330_acquire as ph
from pulsed_polarization import INTERNAL_SOURCES

TOOL = "Clock + EOM + live counts"
ROOT = Path(__file__).resolve().parent
NI = "NI external"
CW = "CW (manual laser)"
HELP = ("Live alignment: optional NI Ctr0/PFI12 trigger + AO0 Alice/AO1 Bob + PicoHarp CH1/CH2. "
        "For internal/CW operation, disconnect PFI12 and select the laser mode manually; no counter is reserved. "
        "Close UniHarp and other AO/clock tools. Start live controls opens a second window with plots and editable outputs. "
        "EOM targets use DAQ = -target/20. Clock changes have a gap; Stop zeroes AOs. Display only; no measurement files are saved.")
FORM = [
    ("action", "Action", "Check settings", ["Check settings", "Start live controls"]),
    ("device", "NI device (USB-6351)", "Dev1", None),
    ("source", "Laser source", NI, [NI, *INTERNAL_SOURCES, CW]),
    ("manual-ready", "Manual mode selected on laser; NI PFI12 disconnected", "No", ["No", "Yes"]),
    ("frequency-hz", "Initial external laser rate (Hz; bench range 1000–1000000)", "500000", None),
    ("high-ns", "Initial positive trigger width (ns)", "100", None),
    ("eom1-v", "Initial Alice / AO0 EOM target (V)", "0", None),
    ("eom2-v", "Initial Bob / AO1 EOM target (V)", "0", None),
    ("poll-ms", "Count-rate refresh (ms; minimum 100)", "200", None),
    ("average-s", "Display smoothing window (s; 0 = latest meter reading)", "0.5", None),
    ("history-s", "Visible count-rate history (s)", "30", None),
    ("dll", "PicoHarp DLL", str(DEFAULT_DLL), "file"),
    ("device-index", "PicoHarp device index", "0", None),
    ("serial", "PicoHarp serial", "1050578", None),
    *[item for prefix, label, level, offset in (("sync", "SYNC", "-250", "0"),
                                              ("ch1", "CH1", "200", "0"),
                                              ("ch2", "CH2", "200", "4.5"))
      for item in ((f"{prefix}-mode", f"{label} trigger mode", "edge", ["edge", "cfd"]),
                   (f"{prefix}-edge", f"{label} edge (edge mode only)", "falling" if prefix == "sync" else "rising", ["rising", "falling"]),
                   (f"{prefix}-level-mv", f"{label} threshold (signed mV)", level, None),
                   (f"{prefix}-zero", f"{label} CFD zero crossing (mV; CFD only)", "-10", None),
                   (f"{prefix}-offset-ns", f"{label} channel offset (ns)", offset, None))],
]


def number(values, key, low, high):
    value = float(values[key])
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key}: enter a finite number from {low:g} to {high:g}.")
    return value


def live_clock(frequency, high_ns):
    frequency, high_ns = float(frequency), float(high_ns)
    if not math.isfinite(frequency) or not 1000 <= frequency <= 1_000_000:
        raise ValueError("This bench tool supports external triggering from 1 kHz to 1 MHz.")
    return clock_plan(frequency, high_ns)


def settings(values):
    source = values.get("source", NI)
    if source not in (NI, CW, *INTERNAL_SOURCES):
        raise ValueError("Select NI external, an internal laser rate, or CW.")
    clock = live_clock(values["frequency-hz"], values["high-ns"]) if source == NI else None
    expected_sync_hz = clock["realized_nominal_frequency_hz"] if clock else INTERNAL_SOURCES.get(source)
    eom = eom_plan(values["eom1-v"], values["eom2-v"])
    device = values["device"].strip()
    if not device or any(c in device for c in "/\\,:"):
        raise ValueError("Enter a NI device name such as Dev1.")

    def trigger(prefix):
        mode = values[prefix + "-mode"]
        result = dict(mode=mode, edge=values[prefix + "-edge"],
                      level_mv=int(values[prefix + "-level-mv"]),
                      offset_ps=round(number(values, prefix + "-offset-ns", -99, 99) * 1000))
        if mode == "cfd":
            result["zero_cross_mv"] = int(values[prefix + "-zero"])
        return result

    config = dict(device_index=int(values["device-index"]), serial=values["serial"].strip(),
                  laser_hz=expected_sync_hz or 500000, sync_divider=1, binning=6,
                  sync=trigger("sync"), detectors=[dict(trigger("ch1"), channel=0), dict(trigger("ch2"), channel=1)])
    ph.validate(config, expected_laser_hz=None)
    # CW has no expected excitation rate; laser_hz above only satisfies shared
    # input validation. No measurement or clock uses that placeholder.
    return dict(device=device, source=source, expected_sync_hz=expected_sync_hz,
                clock=clock, eom=eom, ph330=config, dll=values["dll"],
                poll_s=number(values, "poll-ms", 100, 10000) / 1000,
                average_s=number(values, "average-s", 0, 60), history_s=number(values, "history-s", 2, 600))


def latest(samples, value):
    """Keep only the latest reading for the bounded live display."""
    try:
        samples.put_nowait(value)
    except queue.Full:
        try:
            samples.get_nowait()
        except queue.Empty:
            pass
        samples.put_nowait(value)


def run_session(cfg, stop, commands, samples, emit, *, daq=None, constants=None, system=None, api=None):
    """Hardware owner. Injection parameters are for offline fake-device tests."""
    if stop.is_set():
        return 130
    if daq is None:
        import nidaqmx as daq
        from nidaqmx import constants
        from nidaqmx.system import System
        system = System.local()
    device = system.devices[cfg["device"]]
    if device.product_type != "USB-6351" or device.dev_is_simulated:
        raise RuntimeError("Output requires a physical USB-6351.")
    api = api if api is not None else PH330(Path(cfg["dll"]))
    ph.bind_acquisition(api)
    # Cleanup status is transient; this display-only tool creates no run files.
    record = dict(status="preparing", ao_zeroed_on_exit=False, cleanup_errors=[], samples=0)
    ao = clock = None
    opened = reserved = clock_started = False
    index = cfg["ph330"]["device_index"]
    current_clock, current_eom = cfg["clock"], cfg["eom"]
    epoch, origin, code = 0, time.monotonic(), 0
    try:
        def applied():
            record.update(last_clock=current_clock, last_eom=current_eom)
            emit(dict(kind="applied", epoch=epoch, clock=current_clock, eom=current_eom))

        serial = ct.create_string_buffer(8)
        api.call("OpenDevice", index, serial)
        opened = True
        if serial.value.decode() != cfg["ph330"]["serial"]:
            raise RuntimeError("PicoHarp serial does not match. Use Connection test to verify the device.")
        info = ph.configure(api, cfg["ph330"], rate_meters_only=True, tttr_mode=2 if cfg["source"] == CW else 3)
        if stop.is_set():
            raise KeyboardInterrupt
        ao = daq.Task()
        ao.ao_channels.add_ao_voltage_chan(f"{cfg['device']}/ao0:1", min_val=-10, max_val=10)
        ao.control(constants.TaskMode.TASK_VERIFY)
        ao.control(constants.TaskMode.TASK_COMMIT)
        reserved = True
        if current_clock is not None:
            clock = prepared_clock(daq, constants, cfg["device"], current_clock)
        if stop.is_set():
            raise KeyboardInterrupt
        ao.write(current_eom["daq_v"], auto_start=True)
        if stop.wait(0.01):
            raise KeyboardInterrupt
        if clock is not None:
            clock.start()
            clock_started = True
        record["status"] = "running"
        applied()
        print(f"{cfg['source']}: EOM outputs started; reading PicoHarp rate meters (no TTTR). "
              + ("NI laser clock running." if clock else "PFI12/Ctr0 unused; laser controlled manually."), flush=True)
        # Rate-meter gate is 100 ms. Wait 200 ms after startup/output changes.
        next_read = time.monotonic() + max(.2, cfg["poll_s"])
        previous_warning = None
        while not stop.is_set():
            if clock is not None and clock.is_task_done():
                raise RuntimeError("NI laser counter stopped unexpectedly.")
            try:
                kind, requested = commands.get_nowait()
            except queue.Empty:
                kind = None
            if kind is not None:
                try:
                    if kind == "eom":
                        plan = eom_plan(*requested["target_eom_v"])
                    elif kind == "clock":
                        if cfg["source"] != NI:
                            raise ValueError("NI clock control is disabled in manual laser modes. Stop and select NI external to change source.")
                        plan = live_clock(requested["requested_frequency_hz"], requested["requested_high_ns"])
                    else:
                        raise ValueError("Unknown live adjustment.")
                except (ValueError, KeyError, TypeError, OverflowError) as exc:
                    emit(dict(kind="rejected", message=str(exc)))
                    continue
                if stop.is_set():
                    break
                if kind == "eom":
                    ao.write(plan["daq_v"], auto_start=True)
                    current_eom = plan
                else:
                    clock.stop()
                    clock_started = False
                    clock.close()
                    clock = None
                    clock = prepared_clock(daq, constants, cfg["device"], plan)
                    if stop.is_set():
                        raise KeyboardInterrupt
                    clock.start()
                    clock_started = True
                    current_clock = plan
                epoch += 1
                applied()
                print(f"Applied {kind}: Alice/Bob {current_eom['target_eom_v']} V; "
                      + (f"NI clock {current_clock['realized_nominal_frequency_hz']:g} Hz."
                         if current_clock else f"{cfg['source']}; laser controlled manually."), flush=True)
                next_read = time.monotonic() + max(.2, cfg["poll_s"])
            if time.monotonic() >= next_read:
                reading = ph.rates(api, index, info["input_count"])
                sync = reading["sync_hz"]
                ch1, ch2 = reading["input_hz"][:2]
                commanded = current_clock["realized_nominal_frequency_hz"] if current_clock else None
                expected = commanded if current_clock else cfg["expected_sync_hz"]
                value = dict(host_utc=reading["host_utc"], elapsed_s=time.monotonic() - origin, epoch=epoch,
                             sync_hz=sync, ch1_hz=ch1, ch2_hz=ch2, sum_hz=ch1 + ch2,
                             commanded_hz=commanded, source=cfg["source"], expected_sync_hz=expected,
                             alice_v=current_eom["target_eom_v"][0],
                             bob_v=current_eom["target_eom_v"][1], sync_matches=None if expected is None else abs(sync / expected - 1) <= .05,
                             warnings=reading["warnings"], warnings_text=reading["warnings_text"].strip())
                record["samples"] += 1
                latest(samples, value)
                warning = (value["sync_matches"], value["warnings"], value["warnings_text"])
                if warning != previous_warning:
                    status = ("CW: periodic SYNC is not required." if expected is None else
                              "within 5% of selected rate." if value["sync_matches"] else "MISMATCH: check laser mode/SYNC.")
                    print(f"SYNC {sync:g} Hz; " + status +
                          (f" PicoHarp: {value['warnings_text']}" if value["warnings"] else ""), flush=True)
                    previous_warning = warning
                next_read = time.monotonic() + cfg["poll_s"]
            stop.wait(min(.05, max(0, next_read - time.monotonic())))
        record["status"] = "stopped"
        code = 130
    except KeyboardInterrupt:
        record["status"] = "stopped"
        code = 130
    except Exception as exc:
        record.update(status="error", error=str(exc))
        emit(dict(kind="error", message=str(exc)))
        print(f"Live alignment error: {exc}", flush=True)
        code = 1
    finally:
        def cleanup(label, operation):
            try:
                operation()
                return True
            except Exception as exc:
                record["cleanup_errors"].append(f"{label}: {exc}")
                print(f"Cleanup failed: {label}: {exc}", flush=True)
                return False
        if clock is not None:
            if clock_started:
                cleanup("stop clock", clock.stop)
            cleanup("close clock", clock.close)
        if ao is not None:
            if reserved:
                record["ao_zeroed_on_exit"] = cleanup("zero AO0/AO1", lambda: ao.write([0., 0.], auto_start=True))
            cleanup("close AO", ao.close)
        if opened:
            cleanup("close PicoHarp", lambda: api.call("CloseDevice", index))
        if record["cleanup_errors"]:
            record["status"] = "error"
            code = 1
        emit(dict(kind="cleanup", zeroed=record["ao_zeroed_on_exit"], errors=record["cleanup_errors"], summary=record))
        print("Live alignment stopped. Display data discarded; no run files saved.", flush=True)
    return code


def main(values, stop_event=None, commands=None, samples=None, emit=None):
    try:
        cfg = settings(values)
        if values["action"] == "Check settings":
            print(json.dumps(cfg, indent=2))
            print("Settings valid. No hardware accessed. Select Start live controls to run.")
            return 0
        if values["action"] != "Start live controls":
            raise ValueError("Unknown action.")
        if cfg["source"] != NI and values.get("manual-ready") != "Yes":
            raise ValueError("Disconnect PFI12, select the chosen mode on the laser, then confirm in the Run tab.")
        return run_session(cfg, stop_event if stop_event is not None else threading.Event(),
                           commands if commands is not None else queue.Queue(maxsize=1),
                           samples if samples is not None else queue.Queue(maxsize=1), emit or (lambda v: None))
    except Exception as exc:
        print(f"Live alignment: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch(TOOL)
