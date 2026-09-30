"""Finite, hardware-timed Alice/Bob generation with raw PicoHarp T3 recording.

This is an acquisition/commissioning backend, not key reconciliation. Without a
hardware reference between NI and PicoHarp, every run has unverified SYNC origin.
Host timestamps, the first photon, and discarded warmup pulses do not resolve it.

NI X-Series manual, chapters 5 and 7: AO SampleClock can start a counter; an
embedded counter ends a finite pulse train. Both engines divide the same 100 MHz
timebase. The final delayed pulse must finish before changing the held AO values.
"""
from __future__ import annotations

import copy
import ctypes as ct
from datetime import datetime, timezone
import math
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import numpy as np

import bb84_sequence as sequence
import eom_timing_scope as timing
import ph330_acquire as photon
from ph330 import PH330, DEFAULT_DLL
from record_io import save_json as _save


BUFFER_SAMPLES = 1_000_000
CHUNK_SAMPLES = 100_000
WRITE_TIMEOUT_S = 1.0
TAIL_SECONDS = 0.02
SYNC_LOST = 0x0004
SYNC_STARTUP_SECONDS = 0.5
# A driver call that ignores its timeout must retain its task/mapping instead of
# racing close()/zero() against the still-running writer. Restart the process
# after resolving that exceptional driver failure; ordinary Stop never uses this.
_RETAINED_RESOURCES = []


def prepare_plan(cfg):
    """Validate without loading a DLL, creating files, or accessing hardware."""
    if cfg.get("mode") not in ("ordered", "random"):
        raise ValueError("mode must be ordered or random.")
    trials = sequence._integer(cfg.get("trial_count"), "trial_count", 1)
    warmup = sequence._integer(cfg.get("warmup_count"), "warmup_count", 0)
    total = trials + warmup
    if not 2 <= total <= 2**32 - 1:
        raise ValueError("Total warmup plus trial samples must be 2 through 4294967295.")
    alice = sequence._voltages(cfg.get("alice_voltages"), sequence.STATES, "alice_voltages")
    bob = sequence._voltages(cfg.get("bob_voltages"), sequence.BASES, "bob_voltages")
    if type(cfg.get("labels_confirmed", False)) is not bool:
        raise ValueError("labels_confirmed must be true or false.")
    args = SimpleNamespace(device=cfg.get("device", "Dev1"),
                           frequency_hz=float(cfg["frequency_hz"]),
                           delay_ns=float(cfg["delay_ns"]), high_ns=float(cfg["high_ns"]),
                           seconds=0, hold_pulses=1,
                           alice_a_v=alice["H"], alice_b_v=alice["V"],
                           bob_a_v=bob["HV"], bob_b_v=bob["RL"])
    plan = timing.make_plan(args)
    ph = copy.deepcopy(cfg["ph330"])
    ph["laser_hz"] = plan["realized_rate_hz"]
    photon.validate(ph, expected_laser_hz=None)
    plan.update(mode=cfg["mode"], trial_count=trials, warmup_count=warmup,
                trigger_chain=copy.deepcopy(cfg.get("trigger_chain")),
                samples_per_channel=total, requested_seconds=trials / plan["realized_rate_hz"],
                warmup_seconds=warmup / plan["realized_rate_hz"],
                output_seconds=total / plan["realized_rate_hz"],
                alice_voltages=alice, bob_voltages=bob, ph330=ph,
                labels_confirmed=cfg.get("labels_confirmed", False),
                buffer_samples=min(total, BUFFER_SAMPLES, max(2048, round(plan["realized_rate_hz"] * 2))),
                chunk_samples=min(total, CHUNK_SAMPLES, max(1, round(plan["realized_rate_hz"] * .1))),
                mode_note="Finite nonregenerating AO and finite delayed Ctr0 pulse train",
                alignment_status="unverified", sync_origin=None,
                estimated_sequence_bytes=16 * total + 2 * trials,
                random_choices_note="OS cryptographic randomness; no repeated random waveform"
                                    if cfg["mode"] == "random" else "Ordered commissioning pattern")
    return plan


def _configure_ni(daq, system, constants, plan, tasks):
    """Return verified tasks; append immediately so partial setup can be closed."""
    device = system.devices[plan["device"]]
    if device.product_type != "USB-6351" or device.dev_is_simulated:
        raise RuntimeError("Output requires a physical NI USB-6351.")
    ao = daq.Task()
    tasks["ao"] = ao
    co = daq.Task()
    tasks["counter"] = co
    ao.ao_channels.add_ao_voltage_chan(f"{plan['device']}/ao0:1", min_val=-10, max_val=10)
    ao.ao_channels.all.ao_use_only_on_brd_mem = False
    ao.timing.cfg_samp_clk_timing(plan["realized_rate_hz"],
                                 sample_mode=constants.AcquisitionType.FINITE,
                                 samps_per_chan=plan["samples_per_channel"])
    ao.timing.samp_clk_timebase_src = f"/{plan['device']}/100MHzTimebase"
    ao.timing.samp_clk_timebase_rate = timing.TIMEBASE_HZ
    ao.timing.samp_clk_timebase_div = plan["period_ticks"]
    ao.out_stream.output_buf_size = plan["buffer_samples"]
    ao.out_stream.regen_mode = constants.RegenerationMode.DONT_ALLOW_REGENERATION
    channel = co.co_channels.add_co_pulse_chan_ticks(
        plan["counter"], source_terminal=f"/{plan['device']}/100MHzTimebase",
        idle_state=constants.Level.LOW, initial_delay=plan["delay_ticks"],
        low_ticks=plan["low_ticks"], high_ticks=plan["high_ticks"])
    channel.co_pulse_term = plan["terminal"]
    co.timing.cfg_implicit_timing(sample_mode=constants.AcquisitionType.FINITE,
                                 samps_per_chan=plan["samples_per_channel"])
    co.triggers.start_trigger.cfg_dig_edge_start_trig(plan["start_trigger"],
                                                    trigger_edge=constants.Edge.RISING)
    for task in (ao, co):
        task.control(constants.TaskMode.TASK_VERIFY)
    for task in (ao, co):
        task.control(constants.TaskMode.TASK_COMMIT)
    readback = dict(product_type=device.product_type, serial_number=device.dev_serial_num,
                    ao_rate_hz=ao.timing.samp_clk_rate,
                    ao_timebase_hz=ao.timing.samp_clk_timebase_rate,
                    ao_divisor=ao.timing.samp_clk_timebase_div,
                    ao_timebase_source=ao.timing.samp_clk_timebase_src,
                    ao_onboard_only=ao.ao_channels.all.ao_use_only_on_brd_mem,
                    ao_regen_mode=str(ao.out_stream.regen_mode),
                    ao_samples=ao.timing.samp_quant_samp_per_chan,
                    counter_samples=co.timing.samp_quant_samp_per_chan,
                    counter_timebase_source=channel.co_ctr_timebase_src,
                    counter_initial_delay=channel.co_pulse_ticks_initial_delay,
                    counter_high_ticks=channel.co_pulse_high_ticks,
                    counter_low_ticks=channel.co_pulse_low_ticks,
                    counter_terminal=channel.co_pulse_term,
                    counter_start_source=co.triggers.start_trigger.dig_edge_src)
    if (not math.isclose(readback["ao_rate_hz"], plan["realized_rate_hz"], rel_tol=1e-9)
            or readback["ao_timebase_hz"] != timing.TIMEBASE_HZ
            or readback["ao_divisor"] != plan["period_ticks"]
            or readback["ao_onboard_only"]
            or ao.out_stream.regen_mode != constants.RegenerationMode.DONT_ALLOW_REGENERATION
            or readback["ao_samples"] != plan["samples_per_channel"]
            or readback["counter_samples"] != plan["samples_per_channel"]
            or readback["counter_initial_delay"] != plan["delay_ticks"]
            or readback["counter_high_ticks"] != plan["high_ticks"]
            or readback["counter_low_ticks"] != plan["low_ticks"]):
        raise RuntimeError("DAQmx readback differs from planned finite timing; outputs not started.")
    return ao, co, readback


def _write_chunk(writer, waveform, start, stop):
    # Slice copies are bounded; never turn a multi-million-point array into a Python list.
    chunk = np.ascontiguousarray(waveform[:, start:stop], dtype=np.float64)
    count = writer.write_many_sample(chunk, timeout=WRITE_TIMEOUT_S)
    if count != stop - start:
        raise RuntimeError(f"Short AO write: {count} of {stop - start} samples.")


def _fill_remaining(writer, waveform, start, chunk_samples, abort, result):
    try:
        total = waveform.shape[1]
        while start < total and not abort.is_set():
            end = min(total, start + chunk_samples)
            _write_chunk(writer, waveform, start, end)
            start = end
            result["samples_written"] = start
        result["complete"] = start == total
    except BaseException as exc:
        result["error"] = str(exc)
        result["complete"] = False


def _check_flags(raw, flags, phase="active", elapsed=0., startup_allowance=0.):
    """Tolerate absent SYNC only at bounded, explicitly recorded boundaries.

    GetFlags has no event timestamp. These host observations do not establish
    which photons are valid or the NI-to-T3 index origin.
    """
    raw["flags_seen"] |= flags
    by_phase = raw.setdefault("flags_by_phase", {})
    by_phase[phase] = by_phase.get(phase, 0) | flags
    bad = flags & photon.BAD_FLAGS
    if phase in ("tail", "stopped"):
        bad &= ~SYNC_LOST
    elif phase == "startup":
        if flags & SYNC_LOST:
            if elapsed < startup_allowance:
                bad &= ~SYNC_LOST
                raw["startup_sync_loss_observed"] = True
            elif not bad & ~SYNC_LOST:
                raise RuntimeError("PicoHarp SYNC_LOST did not clear within the startup allowance "
                                   f"({startup_allowance:g} s, bounded by warm-up). Check BDL TRG OUT "
                                   "-> PicoHarp SYNC, input edge/threshold, and laser triggering.")
        else:
            raw["startup_sync_clear_observed"] = True
            raw["startup_sync_clear_host_seconds_after_ni_start"] = elapsed
            raw["startup_sync_clear_raw_record_offset"] = raw["records"]
    if bad:
        raise RuntimeError("Invalid TTTR flags during " + phase + ": " + ", ".join(
            name for bit, name in photon.FLAG_NAMES.items() if bad & bit))


def _fifo_once(api, index, stream, raw, buffer, actual, *, phase="active",
               elapsed=0., startup_allowance=0.):
    flags = photon.scalar(api, index, "GetFlags")
    _check_flags(raw, flags, phase, elapsed, startup_allowance)
    api.call("ReadFiFo", index, buffer, ct.byref(actual))
    if not 0 <= actual.value <= photon.TTREADMAX:
        raise RuntimeError("Invalid PH330 FIFO record count.")
    if actual.value:
        data = ct.string_at(buffer, actual.value * 4)
        if stream.write(data) != len(data):
            raise OSError("Short write while saving raw T3 data.")
        raw["records"] += actual.value
    return actual.value


def _drain_stopped(api, index, stream, raw, buffer, actual):
    empty = 0
    deadline = time.monotonic() + 5
    while empty < 6:
        count = _fifo_once(api, index, stream, raw, buffer, actual, phase="stopped")
        empty = 0 if count else empty + 1
        if time.monotonic() > deadline:
            raise TimeoutError("Stopped PicoHarp FIFO did not drain within five seconds.")
        if not count:
            time.sleep(.001)


def run(cfg, stop_event=None, *, daq=None, system=None, constants=None,
        api_factory=None, writer_factory=None):
    """Record an experimental run; hardware injection is for offline tests only.

    Return 0 on completed acquisition, 130 on Stop, 1 on failure. Completion
    describes delivery/recording, never validation of NI-to-T3 pulse indexing.
    """
    stop_event = stop_event if stop_event is not None else threading.Event()
    if _RETAINED_RESOURCES:
        raise RuntimeError("A previous AO writer ignored its timeout. Resolve the driver/device state "
                           "and restart this program before another acquisition.")
    plan = prepare_plan(cfg)
    folder = Path(cfg["output"]) / datetime.now(timezone.utc).strftime(
        "%Y%m%dT%H%M%S_%fZ_" + plan["mode"])
    folder.mkdir(parents=True, exist_ok=False)
    session_path = folder / "session.json"
    record = dict(schema="qkd-bb84-acquisition-v1", status="preparing", complete=False,
                  plan=plan, note=str(cfg.get("note", "")), measurement=cfg.get("measurement", {}),
                  calibration_transfer=cfg.get("calibration_transfer"), alignment_status="unverified",
                  sync_origin=None, eligible_for_key_processing=False,
                  labels_confirmed=plan["labels_confirmed"],
                  alignment_note="NI sample indices are known, but their PicoHarp SYNC offset is not. "
                                 "Warmup and the first photon do not establish trial zero.",
                  cleanup_errors=[], ao_zeroed_on_exit=False, hardware_started=False,
                  rates_validation="not_yet_checked", ni_sequence_complete=False,
                  host_start_utc=datetime.now(timezone.utc).isoformat())
    raw = None
    raw_path = None
    stream = None
    api = None
    opened = measuring = ao_started = co_started = False
    tasks = {}
    waveform = None
    writer_thread = None
    writer_abort = threading.Event()
    writer_result = {"complete": False, "samples_written": 0}
    code = 0
    buffer, actual = (ct.c_uint32 * photon.TTREADMAX)(), ct.c_int()
    _save(session_path, record)
    try:
        print("Preparing and saving the entire Alice/Bob sequence before enabling outputs.", flush=True)
        manifest = sequence.build_sequence(folder, plan["mode"], plan["trial_count"],
                                           plan["warmup_count"], plan["alice_voltages"],
                                           plan["bob_voltages"], stop_event)
        record["sequence"] = "sequence.json"
        record["sequence_files"] = manifest["files"]
        waveform = np.load(folder / "ao_waveform.npy", mmap_mode="r")
        if stop_event.is_set():
            raise KeyboardInterrupt
        if daq is None:
            import nidaqmx as daq
            from nidaqmx import constants
            from nidaqmx.system import System
            system = System.local()
        if writer_factory is None:
            from nidaqmx.stream_writers import AnalogMultiChannelWriter
            writer_factory = AnalogMultiChannelWriter
        record["stage"] = "verify finite NI resources"
        ao, co, readback = _configure_ni(daq, system, constants, plan, tasks)
        record["daqmx_readback"] = readback
        writer = writer_factory(ao.out_stream, auto_start=False)
        preload = plan["buffer_samples"]
        for start in range(0, preload, plan["chunk_samples"]):
            if stop_event.is_set():
                raise KeyboardInterrupt
            _write_chunk(writer, waveform, start, min(preload, start + plan["chunk_samples"]))
        writer_result.update(samples_written=preload, complete=preload == plan["samples_per_channel"])
        record["stage"] = "configure PicoHarp T3"
        api = (api_factory or PH330)(cfg.get("dll", DEFAULT_DLL))
        photon.bind_acquisition(api)
        index = plan["ph330"]["device_index"]
        serial = ct.create_string_buffer(8)
        api.call("OpenDevice", index, serial)
        opened = True
        if serial.value.decode() != plan["ph330"]["serial"]:
            raise RuntimeError("PicoHarp serial differs from configured device.")
        info = photon.configure(api, plan["ph330"])
        # PH330Lib supports software-controlled duration. NI defines the exact
        # finite pulse train; PH additionally records leading/trailing photons.
        api.call("SetMeasControl", index, 6, 0, 0)
        raw_folder = folder / "tttr"
        raw_folder.mkdir()
        raw_path = raw_folder / "metadata.json"
        record["tttr"] = "tttr/metadata.json"
        raw = dict(schema="qkd-ph330-raw-t3-v1", status="prepared", complete=False,
                   config=plan["ph330"], hardware=info, records=0, flags_seen=0,
                   dll=str(api.path), library_version=api.version,
                   raw_file="events.t3raw", record_format="GenericT3", record_type="0x00010307",
                   dtype="little-endian uint32", measurement_control="software_start_stop",
                   alignment_status="unverified", sync_origin=None,
                   startup_sync_allowance_s=min(SYNC_STARTUP_SECONDS, plan["warmup_seconds"]),
                   sync_boundary_note="SYNC_LOST may occur before laser startup or after finite clock stop. "
                                      "All flags and raw records are retained. Host phase/record offsets "
                                      "are diagnostic only, not valid-event boundaries or pulse alignment.",
                   note="Includes pre-NI and post-NI events. No pulse-index correspondence established.")
        _save(raw_path, raw)
        _save(session_path, record)
        stream = (raw_folder / "events.t3raw").open("xb")
        if stop_event.is_set():
            raise KeyboardInterrupt
        api.call("StartMeas", index, 1)  # tacq is ignored in control mode 6.
        measuring = True
        raw.update(status="running", host_start_utc=datetime.now(timezone.utc).isoformat())
        record["stage"] = "arm counter then start finite AO"
        co_started = True
        co.start()
        ao_started = True
        ao.start()
        started = time.monotonic()
        record.update(status="running", hardware_started=True,
                      ni_host_start_utc=datetime.now(timezone.utc).isoformat())
        if preload < plan["samples_per_channel"]:
            writer_thread = threading.Thread(target=_fill_remaining, daemon=True,
                args=(writer, waveform, preload, plan["chunk_samples"], writer_abort, writer_result),
                name="BB84-AO-buffer-writer")
            writer_thread.start()
        _save(session_path, record)
        print(f"Running {plan['trial_count']:,} {plan['mode']} trials after "
              f"{plan['warmup_count']:,} warmup pulses at {plan['realized_rate_hz']:g} Hz.", flush=True)
        print("T3 origin is UNVERIFIED; this run cannot yet be accepted as indexed key data.", flush=True)
        print(f"Allowing startup SYNC_LOST for up to {raw['startup_sync_allowance_s']:g} s "
              "within warm-up; after the first clear flag, SYNC loss is fatal until NI completes.", flush=True)
        finished_at = None
        progress_at = started
        deadline = started + plan["output_seconds"] + 15
        while True:
            if stop_event.is_set():
                raise KeyboardInterrupt
            if "error" in writer_result:
                raise RuntimeError("AO streaming failed: " + writer_result["error"])
            if photon.scalar(api, index, "CTCStatus"):
                raise RuntimeError("PicoHarp acquisition ended before software requested Stop.")
            now = time.monotonic()
            ao_done, co_done = ao.is_task_done(), co.is_task_done()
            record["stage"] = "record finite NI sequence"
            if (record["rates_validation"] == "not_yet_checked" and now - started >= .2
                    and not co_done and raw.get("startup_sync_clear_observed")):
                measured = photon.rates(api, index, info["input_count"])
                record["rates_during"] = measured
                period = photon.scalar(api, index, "GetSyncPeriod", ct.c_double)
                raw["measured_sync_period_s"] = period
                if (abs(measured["sync_hz"] / plan["realized_rate_hz"] - 1) > .05
                        or not math.isfinite(period) or period <= 0
                        or abs(period * plan["realized_rate_hz"] - 1) > .05):
                    raise RuntimeError("PicoHarp SYNC rate/period does not match the active NI clock.")
                record["rates_validation"] = "passed_rate_only_not_index_alignment"
            if ao_done and co_done and finished_at is None:
                # Hardware completion can beat publication of the final Python
                # writer status by one scheduling interval. Let it publish first.
                if not writer_result["complete"] and writer_thread is not None:
                    writer_thread.join(.01)
                    if writer_thread.is_alive() and "error" not in writer_result:
                        if now > deadline:
                            raise TimeoutError("AO writer did not finish after finite NI generation.")
                        continue
                if not writer_result["complete"]:
                    raise RuntimeError("NI tasks ended before all waveform samples were supplied.")
                generated = ao.out_stream.total_samp_per_chan_generated
                record["ao_samples_generated"] = generated
                if generated != plan["samples_per_channel"]:
                    raise RuntimeError("AO generated sample count differs from planned finite sequence.")
                record["ni_sequence_complete"] = True
                finished_at = now
            if finished_at is not None:
                if not raw.get("startup_sync_clear_observed"):
                    raise RuntimeError("Finite NI sequence ended before a clear PicoHarp SYNC flag was observed. "
                                       "Increase warm-up/run duration and verify TRG OUT -> SYNC.")
                phase = "tail"
                record["stage"] = "record tail after finite NI completion"
            else:
                phase = "active" if raw.get("startup_sync_clear_observed") else "startup"
            _fifo_once(api, index, stream, raw, buffer, actual, phase=phase,
                       elapsed=now-started, startup_allowance=raw["startup_sync_allowance_s"])
            if finished_at is not None and now - finished_at >= TAIL_SECONDS:
                break
            if now > deadline:
                raise TimeoutError("Finite NI generation exceeded expected duration plus 15 seconds.")
            if now >= progress_at:
                print(f"Saved {raw['records']:,} raw records; supplied "
                      f"{writer_result['samples_written']:,}/{plan['samples_per_channel']:,} AO samples.",
                      flush=True)
                progress_at = now + 2
            if not actual.value:
                time.sleep(.001)
        if record["rates_validation"] == "not_yet_checked":
            record["rates_validation"] = "not_checked_run_shorter_than_rate_meter_settling"
        record.update(status="completed", complete=True)
    except (KeyboardInterrupt, sequence.SequenceCancelled) as exc:
        code = 130
        record.update(status="interrupted", complete=False, error=str(exc) or "Stopped by user.")
    except Exception as exc:
        code = 1
        record.update(status="error", complete=False, error=str(exc))
        print(f"BB84 acquisition failed at {record.get('stage', 'sequence preparation')}: {exc}", flush=True)
    finally:
        writer_abort.set()
        # A host abort can leave an uncertain tail. Stop the laser-trigger
        # counter FIRST, retain partial files, and never call reset_device().
        for name, was_started in (("counter", co_started), ("ao", ao_started)):
            task = tasks.get(name)
            if task is not None and was_started:
                try:
                    task.stop()
                except Exception as exc:
                    record["cleanup_errors"].append(f"Stop {name}: {exc}")
        if writer_thread is not None:
            writer_thread.join(WRITE_TIMEOUT_S + 2)
            if writer_thread.is_alive():
                record["cleanup_errors"].append("AO writer did not stop within its bounded timeout.")
        writer_alive = writer_thread is not None and writer_thread.is_alive()
        record["ao_stream"] = dict(writer_result)
        if measuring:
            try:
                api.call("StopMeas", index)
                _drain_stopped(api, index, stream, raw, buffer, actual)
                raw["elapsed_ms"] = photon.scalar(api, index, "GetElapsedMeasTime", ct.c_double)
            except Exception as exc:
                record["cleanup_errors"].append(f"Stop/drain PicoHarp: {exc}")
        if stream is not None:
            try:
                stream.close()
            except Exception as exc:
                record["cleanup_errors"].append(f"Close raw data file: {exc}")
        if opened:
            try:
                api.call("CloseDevice", index)
            except Exception as exc:
                record["cleanup_errors"].append(f"Close PicoHarp: {exc}")
        for name in ("counter", "ao"):
            task = tasks.get(name)
            if name == "ao" and writer_alive:
                _RETAINED_RESOURCES.append((task, waveform))
                record["cleanup_errors"].append("AO resources retained because the writer is still using them. "
                                               "Resolve the driver/device state and restart this program.")
                continue
            if task is not None:
                try:
                    task.close()
                except Exception as exc:
                    record["cleanup_errors"].append(f"Close {name}: {exc}")
        if ao_started and not writer_alive:
            zero = None
            try:
                zero = daq.Task()
                zero.ao_channels.add_ao_voltage_chan(f"{plan['device']}/ao0:1", min_val=-10, max_val=10)
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
        if waveform is not None:
            # Release the mmap explicitly so Windows permits later archive moves.
            del waveform
        if record["cleanup_errors"]:
            code = 1
            record.update(status="cleanup_error", complete=False)
        record["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        if raw is not None:
            raw.update(status=record["status"], complete=record["complete"],
                       host_end_utc=record["host_end_utc"], cleanup_errors=record["cleanup_errors"])
            if "error" in record:
                raw["error"] = record["error"]
            _save(raw_path, raw)
            print(f"Acquisition record: {raw_path.resolve()}", flush=True)
        _save(session_path, record)
        print(f"Run record: {session_path.resolve()}", flush=True)
    return code
