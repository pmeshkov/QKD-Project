"""Standalone USB-6351 Ctr0/PFI12 characterization; dry run unless --run.

This is a continuous hardware clock, not a finite, indexed QKD acquisition.
Python controls only the approximate run duration, never individual pulses.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time


TIMEBASE_HZ = 1_000_000_000


def clock_plan(frequency_hz, high_ns):
    """Round to 10 ns ticks and validate the realized waveform."""
    if not math.isfinite(frequency_hz) or not 1_000 <= frequency_hz <= 10_000_000:
        raise ValueError("Characterization frequency must be 1 kHz through 1 MHz.")
    if not math.isfinite(high_ns) or high_ns <= 0:
        raise ValueError("High time must be finite and positive.")
    period_ticks = round(TIMEBASE_HZ / frequency_hz)
    high_ticks = round(high_ns / 10)
    low_ticks = period_ticks - high_ticks
    if min(high_ticks, low_ticks) < 2:
        raise ValueError("Each pulse phase must have at least two timebase ticks.")
    if high_ticks / period_ticks > 0.30:
        raise ValueError("Realized duty cycle exceeds the BDL limit of 30%.")
    return {
        "requested_frequency_hz": frequency_hz,
        "requested_high_ns": high_ns,
        "nominal_timebase_hz": TIMEBASE_HZ,
        "high_ticks": high_ticks,
        "low_ticks": low_ticks,
        "realized_nominal_frequency_hz": TIMEBASE_HZ / period_ticks,
        "realized_high_ns": high_ticks * 10,
        "realized_low_ns": low_ticks * 10,
        "realized_duty_cycle": high_ticks / period_ticks,
    }


def run_clock(args, plan):
    # Delay the import so offline planning does not require NI software.
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, Level, TaskMode
    from nidaqmx.system import System

    args.log_dir.mkdir(parents=True, exist_ok=True)
    path = args.log_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ") + "_clock.json")
    record = dict(plan, device=args.device, counter=f"{args.device}/ctr0",
                  terminal=f"/{args.device}/PFI12", requested_duration_s=args.seconds,
                  status="preparing", note=args.note,
                  excitation_count=None, optical_lock_verified=False)

    def save():
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    # Fail before reserving hardware if the run record cannot be written.
    save()
    try:
        system = System.local()
        device = system.devices[args.device]
        record.update(product_type=device.product_type, serial_number=device.dev_serial_num,
                      simulated=device.dev_is_simulated,
                      driver_version=str(system.driver_version))
        if record["product_type"] != "USB-6351" or record["simulated"]:
            raise RuntimeError("--run requires a physical USB-6351.")
        with nidaqmx.Task() as task:
            channel = task.co_channels.add_co_pulse_chan_ticks(
                record["counter"], source_terminal=f"/{args.device}/100MHzTimebase",
                idle_state=Level.LOW, initial_delay=plan["low_ticks"],
                low_ticks=plan["low_ticks"], high_ticks=plan["high_ticks"],
            )
            channel.co_pulse_term = record["terminal"]
            task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.CONTINUOUS)
            # Resolve the requested route/resources before starting. Do not reset
            # the device or steal a counter from an existing detector task.
            task.control(TaskMode.TASK_VERIFY)
            task.control(TaskMode.TASK_COMMIT)
            readback = {
                "terminal": channel.co_pulse_term,
                "timebase_source": channel.co_ctr_timebase_src,
                "high_ticks": channel.co_pulse_high_ticks,
                "low_ticks": channel.co_pulse_low_ticks,
            }
            record["daqmx_readback"] = readback
            if (readback["high_ticks"] != plan["high_ticks"] or
                    readback["low_ticks"] != plan["low_ticks"]):
                raise RuntimeError("DAQmx changed the requested pulse ticks.")
            record["status"] = "committed"
            save()
            task.start()
            try:
                record["status"] = "running"
                record["host_start_utc"] = datetime.now(timezone.utc).isoformat()
                save()
                print(f"Clock running on {record['terminal']}; Ctrl+C stops it.")
                deadline = time.monotonic() + args.seconds
                while time.monotonic() < deadline:
                    if task.is_task_done():
                        raise RuntimeError("Continuous counter task stopped unexpectedly.")
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                record["status"] = "completed"
            finally:
                task.stop()
    except KeyboardInterrupt:
        record["status"] = "interrupted"
        return_code = 130
    except Exception as exc:
        record.update(status="error", error=str(exc))
        raise
    else:
        return_code = 0
    finally:
        record["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        print(f"Run record: {path.resolve()}")
    return return_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="Dev1")
    parser.add_argument("--frequency-hz", type=float, default=1_000_000)
    parser.add_argument("--high-ns", type=float, default=200)
    parser.add_argument("--seconds", type=float, default=60,
                        help="Approximate host-controlled duration, not an exact pulse count")
    parser.add_argument("--run", action="store_true", help="Actually start hardware output")
    parser.add_argument("--log-dir", type=Path, default=Path("pulsed/runs"))
    parser.add_argument("--note", default="", help="Scope/BDL connection and measurement context")
    args = parser.parse_args(argv)
    try:
        plan = clock_plan(args.frequency_hz, args.high_ns)
        if not math.isfinite(args.seconds) or args.seconds <= 0:
            raise ValueError("Duration must be finite and positive.")
        if not args.device or any(c in args.device for c in "/\\,:"):
            raise ValueError("Specify a device name such as Dev1, not a terminal path.")
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(plan, indent=2))
    if not args.run:
        print("DRY RUN: no NI device accessed. Add --run for the scope test.")
        return 0
    print("Use high-impedance scope loading on raw PFI12. Verify the BDL interface first.")
    try:
        return run_clock(args, plan)
    except Exception as exc:
        print(f"Clock test failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
