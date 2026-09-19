"""Manual EOM DC bias adjustment alongside a hardware-generated PFI12 clock.

Not an indexed BB84 sequencer. Clock changes deliberately stop/restart Ctr0;
manual AO changes are asynchronous to laser pulses. Only the worker owns tasks.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import threading
import time

from laser_clock import clock_plan

HV_GAIN = -20.0  # Existing bench convention in EOM_scripts/EOM_V_sequence.py.
AO_LIMIT = 10.0


def eom_plan(eom1_v, eom2_v):
    targets = [float(eom1_v), float(eom2_v)]
    if any(not math.isfinite(v) or abs(v) > abs(HV_GAIN)*AO_LIMIT for v in targets):
        raise ValueError("EOM targets must be finite and within -200 to +200 V (DAQ ±10 V).")
    return {"target_eom_v": targets, "daq_v": [v/HV_GAIN for v in targets],
            "assumed_hv_gain": HV_GAIN}


def prepared_clock(daq, constants, device, plan):
    task = daq.Task()
    try:
        channel = task.co_channels.add_co_pulse_chan_ticks(
            f"{device}/ctr0", source_terminal=f"/{device}/100MHzTimebase",
            idle_state=constants.Level.LOW, initial_delay=plan["low_ticks"],
            high_ticks=plan["high_ticks"], low_ticks=plan["low_ticks"])
        channel.co_pulse_term = f"/{device}/PFI12"
        task.timing.cfg_implicit_timing(sample_mode=constants.AcquisitionType.CONTINUOUS)
        task.control(constants.TaskMode.TASK_VERIFY)
        task.control(constants.TaskMode.TASK_COMMIT)
        if (channel.co_pulse_high_ticks != plan["high_ticks"] or
                channel.co_pulse_low_ticks != plan["low_ticks"]):
            raise RuntimeError("DAQmx changed the requested pulse ticks.")
        return task
    except BaseException:
        task.close()
        raise


def run_session(args, stop_event, commands, daq=None, system=None, constants=None):
    """Accept ('eom', plan) and ('clock', plan) commands; stop/zero on exit.

    Injection parameters are for offline fake-device tests only.
    """
    initial_clock = clock_plan(args.frequency_hz, args.high_ns)
    initial_eom = eom_plan(args.eom1_v, args.eom2_v)
    if daq is None:
        import nidaqmx as daq
        from nidaqmx import constants
        from nidaqmx.system import System
        system = System.local()
    device = system.devices[args.device]
    if device.product_type != "USB-6351" or device.dev_is_simulated:
        raise RuntimeError("Output requires a physical USB-6351.")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    path = args.log_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_clock_eom.json")
    record = {"schema": "qkd-manual-clock-eom-v1", "device": args.device,
              "serial_number": device.dev_serial_num, "counter": "ctr0", "terminal": "PFI12",
              "ao_channels": ["ao0", "ao1"], "requested_duration_s": args.seconds,
              "note": args.note, "status": "preparing", "events": [],
              "initial_clock": initial_clock, "initial_eom": initial_eom,
              "ao_zeroed_on_exit": False, "cleanup_errors": [],
              "timing_note": "Host timestamps only; AO changes asynchronous, clock changes interrupt pulse train."}

    def save():
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(record, indent=2)+"\n", encoding="utf-8")
        temp.replace(path)

    def event(kind, **details):
        record["events"].append(dict(kind=kind, host_utc=datetime.now(timezone.utc).isoformat(), **details))
        save()

    save()  # Confirm writable logging before touching outputs.
    ao = clock = None
    ao_reserved = False
    code = 0
    try:
        if stop_event.is_set():
            raise KeyboardInterrupt
        ao = daq.Task()
        ao.ao_channels.add_ao_voltage_chan(f"{args.device}/ao0:1", min_val=-AO_LIMIT, max_val=AO_LIMIT)
        ao.control(constants.TaskMode.TASK_VERIFY)
        ao.control(constants.TaskMode.TASK_COMMIT)
        ao_reserved = True
        clock = prepared_clock(daq, constants, args.device, initial_clock)
        if stop_event.is_set():
            raise KeyboardInterrupt
        ao.write(initial_eom["daq_v"], auto_start=True)
        event("eom_applied", **initial_eom)
        # A conservative startup pause, not a calibrated pulse-to-state delay.
        if stop_event.wait(0.01):
            raise KeyboardInterrupt
        clock.start()
        event("clock_started", **initial_clock)
        record["status"] = "running"
        save()
        print(f"Running PFI12 at {initial_clock['realized_nominal_frequency_hz']:g} Hz; EOM targets {initial_eom['target_eom_v']} V, DAQ {initial_eom['daq_v']} V.", flush=True)
        deadline = time.monotonic() + args.seconds if args.seconds else math.inf
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise KeyboardInterrupt
            if clock.is_task_done():
                raise RuntimeError("Counter stopped unexpectedly.")
            try:
                kind, requested = commands.get(timeout=min(0.05, max(0, deadline-time.monotonic())))
            except queue.Empty:
                continue
            if stop_event.is_set():
                raise KeyboardInterrupt
            # Validate again in the hardware owner before changing any output.
            try:
                if kind == "eom":
                    plan = eom_plan(*requested["target_eom_v"])
                elif kind == "clock":
                    plan = clock_plan(requested["requested_frequency_hz"], requested["requested_high_ns"])
                else:
                    raise ValueError("Unknown adjustment command.")
            except (ValueError, KeyError, TypeError) as exc:
                print(f"Adjustment rejected; outputs unchanged: {exc}", flush=True)
                continue
            event("adjustment_requested", adjustment=kind, plan=plan)
            if kind == "eom":
                ao.write(plan["daq_v"], auto_start=True)
                event("eom_applied", **plan)
                print(f"Applied EOM1/AO0, EOM2/AO1 targets {plan['target_eom_v']} V; DAQ outputs {plan['daq_v']} V. Clock continues.", flush=True)
            else:
                clock.stop()
                event("clock_stopped_for_update")
                clock.close()
                clock = None
                clock = prepared_clock(daq, constants, args.device, plan)
                if stop_event.is_set():
                    raise KeyboardInterrupt
                clock.start()
                event("clock_restarted", **plan)
                print(f"Clock restarted at {plan['realized_nominal_frequency_hz']:g} Hz, high {plan['realized_high_ns']:g} ns, duty {plan['realized_duty_cycle']:.1%}. EOM biases held; pulse train has a gap.", flush=True)
        record["status"] = "completed"
    except KeyboardInterrupt:
        record["status"] = "stopped"
        code = 130
    except Exception as exc:
        record.update(status="error", error=str(exc))
        print(f"Clock/EOM error: {exc}", flush=True)
        code = 1
    finally:
        # Cleanup attempts are independent so one driver error cannot skip the others.
        def cleanup(label, operation):
            try:
                operation()
                return True
            except Exception as exc:
                record["cleanup_errors"].append(f"{label}: {exc}")
                print(f"Cleanup failed — {label}: {exc}", flush=True)
                return False
        if clock is not None:
            cleanup("stop counter", clock.stop)
            cleanup("close counter", clock.close)
        if ao is not None:
            if ao_reserved:
                record["ao_zeroed_on_exit"] = cleanup("zero AO0/AO1", lambda: ao.write([0.0, 0.0], auto_start=True))
            cleanup("close analog output", ao.close)
        if record["cleanup_errors"]:
            record["status"] = "error"
            code = 1
        record["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        if record["ao_zeroed_on_exit"]:
            print("AO0/AO1 set to 0 V on exit.", flush=True)
        print(f"Run record: {path.resolve()}", flush=True)
    return code


def main(argv=None, stop_event=None, commands=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="Dev1")
    parser.add_argument("--frequency-hz", type=float, default=1_000_000)
    parser.add_argument("--high-ns", type=float, default=200)
    parser.add_argument("--eom1-v", type=float, default=0)
    parser.add_argument("--eom2-v", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=0, help="0 means until Stop")
    parser.add_argument("--log-dir", type=Path, default=Path(__file__).resolve().parent/"runs"/"clock_eom")
    parser.add_argument("--note", default="")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = {"clock": clock_plan(args.frequency_hz, args.high_ns),
                "eom": eom_plan(args.eom1_v, args.eom2_v)}
        if not math.isfinite(args.seconds) or args.seconds < 0:
            raise ValueError("Duration must be finite and nonnegative; 0 means until Stop.")
        if not args.device or any(c in args.device for c in "/\\,:"):
            raise ValueError("Specify a device name such as Dev1.")
        print(json.dumps(plan, indent=2))
        if not args.run:
            print("DRY RUN: no hardware accessed.")
            return 0
        return run_session(args, stop_event if stop_event is not None else threading.Event(),
                           commands if commands is not None else queue.Queue())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Clock/EOM: {exc}")
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch("Laser clock + EOM")
