"""Repeating two-level EOM scope test. No PicoHarp acquisition or BB84 indexing.

AO SampleClock starts Ctr0 through its gate/start-trigger route (X Series manual,
ch. 7). Both clocks divide the same 100 MHz timebase by exactly the same integer.
Initial delay is relative to the first AO sample-clock edge, not AO StartTrigger.
Route propagation, DAC response and BDL TTL/TRG offsets must be measured on scope.
"""
import argparse
from datetime import datetime, timezone
import csv
import json
import math
from pathlib import Path
import threading
import time

import numpy as np

TIMEBASE_HZ = 100_000_000
TICK_NS = 10
HV_GAIN = -20.0
DATA = Path(__file__).resolve().parents[1] / "data" / "timing"


def make_plan(args):
    if not args.device or any(c in args.device for c in "/\\,:"):
        raise ValueError("Use a device name such as Dev1.")
    if not math.isfinite(args.frequency_hz) or not 1_000 <= args.frequency_hz <= 1_000_000:
        raise ValueError("Scope-test laser rate must be 1 kHz through 1 MHz.")
    if not math.isfinite(args.seconds) or args.seconds < 0:
        raise ValueError("Duration must be nonnegative; 0 means until Stop.")
    if not 1 <= args.hold_pulses <= 2000:
        raise ValueError("Pulses per voltage level must be 1 through 2000.")
    if any(not math.isfinite(x) or x <= 0 for x in (args.delay_ns, args.high_ns)):
        raise ValueError("Delay and pulse width must be finite and positive.")
    period = round(TIMEBASE_HZ / args.frequency_hz)
    delay, high = round(args.delay_ns / TICK_NS), round(args.high_ns / TICK_NS)
    if min(delay, high, period - high) < 2:
        raise ValueError("Delay and each counter pulse phase need at least 20 ns.")
    if high / period > 0.30:
        raise ValueError("Realized positive laser-trigger duty cycle exceeds 30%.")
    if delay + high > period - 2:
        raise ValueError("Delay + pulse width must leave at least 20 ns before the next AO clock.")
    levels = [[args.alice_a_v, args.alice_b_v], [args.bob_a_v, args.bob_b_v]]
    if any(not math.isfinite(v) or abs(v) > 200 for row in levels for v in row):
        raise ValueError("Each EOM target must be within -200 to +200 V (DAQ within +/-10 V).")
    rate = TIMEBASE_HZ / period
    return dict(requested_rate_hz=args.frequency_hz, realized_rate_hz=rate,
                period_ticks=period, period_ns=period*TICK_NS,
                requested_delay_ns=args.delay_ns, delay_ticks=delay,
                nominal_delay_ns=delay*TICK_NS, requested_high_ns=args.high_ns,
                high_ticks=high, low_ticks=period-high, high_ns=high*TICK_NS,
                duty_cycle=high/period, hold_pulses=args.hold_pulses,
                level_dwell_us=args.hold_pulses/rate*1e6,
                ab_pattern_hz=rate/(2*args.hold_pulses), target_hv=levels,
                daq_levels=[[v/HV_GAIN for v in row] for row in levels],
                assumed_hv_gain=HV_GAIN, samples_per_channel=2*args.hold_pulses,
                timebase_hz=TIMEBASE_HZ, device=args.device,
                start_trigger=f"/{args.device}/ao/SampleClock",
                counter=f"{args.device}/ctr0", terminal=f"/{args.device}/PFI12",
                mode="continuous onboard regeneration; approximate host-controlled duration",
                delay_note="Nominal delay from AO sample clock to NI trigger; scope verification required.")


def waveform(plan):
    return np.ascontiguousarray(np.repeat(np.array(plan["daq_levels"], dtype=float),
                                         plan["hold_pulses"], axis=1))


def save_preview(folder, plan):
    # Object-based Agg rendering also works in the GUI worker thread.
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    period = plan["period_ns"] / 1000
    hold = plan["hold_pulses"]
    fig = Figure(figsize=(9, 6), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(3, 1, sharex=True)
    for ax, label, levels in zip(axes[:2], ("Alice target (V)", "Bob target (V)"), plan["target_hv"]):
        ax.step(np.arange(5)*hold*period, [*levels, *levels, levels[0]], where="post")
        ax.set_ylabel(label)
        ax.grid(alpha=.25)
    if hold <= 50:
        edges = np.arange(4*hold)*period + plan["nominal_delay_ns"]/1000
        x, y = [0.], [0]
        for edge in edges:
            x.extend([edge, edge, edge+plan["high_ns"]/1000, edge+plan["high_ns"]/1000])
            y.extend([0, 1, 1, 0])
        x.append(4*hold*period)
        y.append(0)
        axes[2].plot(x, y)
    else:
        axes[2].text(.03, .5, f"One trigger each {period:g} us; {hold} triggers per level",
                     transform=axes[2].transAxes)
    axes[2].set(xlabel="Time from first AO sample clock (us)", ylabel="NI trigger\nlogic level",
                xlim=(0, 4*hold*period), ylim=(-.1, 1.2))
    fig.suptitle("Requested commands only - measure actual settling and laser timing on the scope")
    path = folder / "command_preview.png"
    fig.savefig(path, dpi=130)
    print(f"Preview image: {path.resolve()}", flush=True)


def run_session(args, plan, stop_event, daq=None, system=None, constants=None):
    """Injectable hardware boundary; tests never open a physical device."""
    folder = args.output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_scope")
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / "metadata.json"
    record = dict(schema="qkd-eom-scope-v1", plan=plan, note=args.note,
                  requested_seconds=args.seconds, status="preparing", cleanup_errors=[],
                  ao_zeroed_on_exit=False, hardware_started=False,
                  scope_reference="User observes BDL TTL OUT; offset to TRG OUT/optical pulse unmeasured.")

    def save():
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2)+"\n", encoding="utf-8")
        tmp.replace(path)

    save()  # Fail on unwritable storage before accessing hardware.
    samples = waveform(plan)
    with (folder / "command_cycle.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["sample_index", "ao_clock_time_ns", "ni_trigger_time_ns",
                         "alice_target_hv_v", "bob_target_hv_v", "ao0_v", "ao1_v"])
        for i, pair in enumerate(samples.T):
            writer.writerow([i, i*plan["period_ns"], i*plan["period_ns"]+plan["nominal_delay_ns"],
                             pair[0]*HV_GAIN, pair[1]*HV_GAIN, *pair])
    save_preview(folder, plan)
    ao = co = None
    ao_started = co_started = False
    code = 0
    try:
        if stop_event.is_set():
            raise KeyboardInterrupt
        if daq is None:
            import nidaqmx as daq
            from nidaqmx import constants
            from nidaqmx.system import System
            system = System.local()
        device = system.devices[args.device]
        if device.product_type != "USB-6351" or device.dev_is_simulated:
            raise RuntimeError("Output requires a physical NI USB-6351.")
        record.update(product_type=device.product_type, serial_number=device.dev_serial_num)
        record["stage"] = "configure AO and counter"
        ao = daq.Task()
        co = daq.Task()
        ao.ao_channels.add_ao_voltage_chan(f"{args.device}/ao0:1", min_val=-10, max_val=10)
        ao.ao_channels.all.ao_use_only_on_brd_mem = True
        ao.timing.cfg_samp_clk_timing(plan["realized_rate_hz"],
                                     sample_mode=constants.AcquisitionType.CONTINUOUS,
                                     samps_per_chan=plan["samples_per_channel"])
        ao.timing.samp_clk_timebase_src = f"/{args.device}/100MHzTimebase"
        ao.timing.samp_clk_timebase_rate = TIMEBASE_HZ
        ao.timing.samp_clk_timebase_div = plan["period_ticks"]
        ao.out_stream.output_buf_size = plan["samples_per_channel"]
        ao.out_stream.regen_mode = constants.RegenerationMode.ALLOW_REGENERATION
        channel = co.co_channels.add_co_pulse_chan_ticks(
            plan["counter"], source_terminal=f"/{args.device}/100MHzTimebase",
            idle_state=constants.Level.LOW, initial_delay=plan["delay_ticks"],
            low_ticks=plan["low_ticks"], high_ticks=plan["high_ticks"])
        channel.co_pulse_term = plan["terminal"]
        co.timing.cfg_implicit_timing(sample_mode=constants.AcquisitionType.CONTINUOUS)
        co.triggers.start_trigger.cfg_dig_edge_start_trig(plan["start_trigger"],
                                                        trigger_edge=constants.Edge.RISING)
        record["stage"] = "verify and commit routes (no outputs started)"
        for task in (ao, co):
            task.control(constants.TaskMode.TASK_VERIFY)
        for task in (ao, co):
            task.control(constants.TaskMode.TASK_COMMIT)
        readback = dict(ao_rate_hz=ao.timing.samp_clk_rate,
                        ao_timebase_hz=ao.timing.samp_clk_timebase_rate,
                        ao_divisor=ao.timing.samp_clk_timebase_div,
                        ao_timebase_source=ao.timing.samp_clk_timebase_src,
                        onboard_only=ao.ao_channels.all.ao_use_only_on_brd_mem,
                        counter_timebase_source=channel.co_ctr_timebase_src,
                        counter_initial_delay=channel.co_pulse_ticks_initial_delay,
                        counter_high_ticks=channel.co_pulse_high_ticks,
                        counter_low_ticks=channel.co_pulse_low_ticks,
                        counter_terminal=channel.co_pulse_term,
                        counter_start_source=co.triggers.start_trigger.dig_edge_src)
        record["daqmx_readback"] = readback
        if (not math.isclose(readback["ao_rate_hz"], plan["realized_rate_hz"], rel_tol=1e-9)
                or readback["ao_timebase_hz"] != TIMEBASE_HZ
                or readback["ao_divisor"] != plan["period_ticks"]
                or not readback["onboard_only"]
                or readback["counter_initial_delay"] != plan["delay_ticks"]
                or readback["counter_high_ticks"] != plan["high_ticks"]
                or readback["counter_low_ticks"] != plan["low_ticks"]):
            raise RuntimeError("DAQmx timing readback differs from the plan; outputs were not started.")
        record["stage"] = "load complete repeating waveform"
        if ao.write(samples, auto_start=False, timeout=10) != samples.shape[1]:
            raise RuntimeError("Incomplete AO waveform write.")
        save()
        if stop_event.is_set():
            raise KeyboardInterrupt
        record["stage"] = "arm counter then start AO sample clock"
        co_started = True  # Also clean up if a start call fails partway through.
        co.start()  # Waits for the first AO SampleClock rising edge.
        ao_started = True
        ao.start()
        record.update(status="running", hardware_started=True,
                      host_start_utc=datetime.now(timezone.utc).isoformat())
        save()
        print(f"Running: {plan['realized_rate_hz']:g} Hz; nominal trigger delay "
              f"{plan['nominal_delay_ns']:g} ns; level dwell {plan['level_dwell_us']:g} us.", flush=True)
        print("Trigger the scope on a monitor step to distinguish A and B. Stop before editing settings.", flush=True)
        started = time.monotonic()
        while not stop_event.wait(.1):
            if ao.is_task_done() or co.is_task_done():
                raise RuntimeError("A continuous NI task stopped unexpectedly.")
            if args.seconds and time.monotonic() - started >= args.seconds:
                break
        record["host_elapsed_seconds"] = time.monotonic() - started
        record["status"] = "stopped" if stop_event.is_set() else "completed"
    except KeyboardInterrupt:
        code, record["status"] = 130, "cancelled"
    except Exception as exc:
        code, record["status"] = 1, "error"
        record["error"] = str(exc)
        print(f"Scope test failed at {record.get('stage', 'initialization')}: {exc}", flush=True)
    finally:
        # Stop laser triggers BEFORE stopping/changing voltages. Never reset the device.
        for name, task, was_started in (("counter", co, co_started), ("AO", ao, ao_started)):
            if task is not None:
                if was_started:
                    try:
                        task.stop()
                    except Exception as exc:
                        record["cleanup_errors"].append(f"Stop {name}: {exc}")
                try:
                    task.close()
                except Exception as exc:
                    record["cleanup_errors"].append(f"Close {name}: {exc}")
        if ao_started:
            zero = None
            try:
                zero = daq.Task()
                zero.ao_channels.add_ao_voltage_chan(f"{args.device}/ao0:1", min_val=-10, max_val=10)
                zero.write([0., 0.], auto_start=True)
                record["ao_zeroed_on_exit"] = True
            except Exception as exc:
                record["cleanup_errors"].append(f"Zero AO: {exc}")
            finally:
                if zero is not None:
                    try:
                        zero.close()
                    except Exception as exc:
                        record["cleanup_errors"].append(f"Close zero task: {exc}")
        if record["cleanup_errors"]:
            code, record["status"] = 1, "cleanup_error"
            print("Cleanup errors: " + "; ".join(record["cleanup_errors"]), flush=True)
        record["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        save()
        print("AO0/AO1 returned to 0 V." if record["ao_zeroed_on_exit"] else
              "No successful AO zero operation recorded; see run metadata.", flush=True)
        print(f"Run record: {path.resolve()}", flush=True)
    return code


def main(argv=None, stop_event=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="Dev1")
    parser.add_argument("--frequency-hz", type=float, default=1_000_000)
    parser.add_argument("--delay-ns", type=float, default=500)
    parser.add_argument("--high-ns", type=float, default=100)
    parser.add_argument("--hold-pulses", type=int, default=20)
    for key in ("alice-a-v", "alice-b-v", "bob-a-v", "bob-b-v"):
        parser.add_argument("--"+key, type=float, default=0)
    parser.add_argument("--seconds", type=float, default=0)
    parser.add_argument("--output", type=Path, default=DATA)
    parser.add_argument("--note", default="")
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        plan = make_plan(args)
        print(json.dumps(plan, indent=2))
        if not args.run:
            print("DRY RUN: no hardware accessed. Enter your calibrated voltage pair before output.")
            return 0
        if all(a == b for a, b in plan["target_hv"]):
            raise ValueError("Set different A/B voltages for at least one EOM to test a step.")
        return run_session(args, plan, stop_event if stop_event is not None else threading.Event())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"EOM scope test: {exc}")
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch("EOM timing scope")
