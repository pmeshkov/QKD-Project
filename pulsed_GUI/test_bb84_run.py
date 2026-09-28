"""Offline finite NI/T3 orchestration tests. Fakes never load device DLLs."""
import contextlib
import ctypes as ct
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np

import bb84_run as run


def config():
    return dict(mode="ordered", device="Dev1", frequency_hz=500000., delay_ns=1200., high_ns=100.,
                trial_count=16, warmup_count=4, labels_confirmed=False,
                alice_voltages={"H": -152., "V": 50.9, "R": -50.87, "L": 152.69},
                bob_voltages={"HV": 47.52, "RL": -47.44}, dll="fake.dll", output=None,
                ph330={"laser_hz": 500000., "sync_divider": 1, "binning": 6,
                       "device_index": 0, "serial": "test",
                       "sync": {"mode": "edge", "level_mv": -250, "edge": "falling"},
                       "detectors": [{"channel": i, "mode": "edge", "level_mv": 200,
                                      "edge": "rising", "offset_ps": i * 4500} for i in (0, 1)]})


CONSTANTS = NS(AcquisitionType=NS(FINITE="finite"),
               TaskMode=NS(TASK_VERIFY="verify", TASK_COMMIT="commit"),
               RegenerationMode=NS(DONT_ALLOW_REGENERATION="no_regen"),
               Edge=NS(RISING="rising"), Level=NS(LOW="low"))
SYSTEM = NS(devices={"Dev1": NS(product_type="USB-6351", dev_is_simulated=False, dev_serial_num=42)})


class Task:
    def __init__(self, owner):
        self.owner = owner
        self.kind = "unknown"
        self.ao_channels = self.co_channels = self.timing = self.out_stream = self.all = self
        self.triggers = NS(start_trigger=self)
        self.started = False
        self.written = 0
        self.done_checks = 0
        self.total_samp_per_chan_generated = 0

    def add_ao_voltage_chan(self, path, **kwargs):
        self.kind = "ao"
        self.owner.events.append(("ao_channels", path))

    def add_co_pulse_chan_ticks(self, path, **kw):
        self.kind = "counter"
        self.co_ctr_timebase_src = kw["source_terminal"]
        self.co_pulse_ticks_initial_delay = kw["initial_delay"]
        self.co_pulse_high_ticks = kw["high_ticks"]
        self.co_pulse_low_ticks = kw["low_ticks"]
        self.owner.events.append(("counter", path))
        return self

    def cfg_samp_clk_timing(self, rate, **kwargs):
        self.samp_clk_rate = rate
        self.cfg_implicit_timing(**kwargs)

    def cfg_implicit_timing(self, **kwargs):
        self.sample_mode = kwargs["sample_mode"]
        self.samp_quant_samp_per_chan = kwargs["samps_per_chan"]

    def cfg_dig_edge_start_trig(self, source, **kwargs):
        self.dig_edge_src = source

    def control(self, mode):
        self.owner.events.append((mode, self.kind))
        if self.owner.fail == "commit" and self.kind == "counter" and mode == "commit":
            raise RuntimeError("resource reserved")
        if self.owner.fail == "coercion" and self.kind == "ao" and mode == "commit":
            self.samp_clk_timebase_div += 1

    def start(self):
        self.owner.events.append(("start", self.kind))
        self.started = True
        self.owner.start_time = time.monotonic()
        if self.owner.fail == "start" and self.kind == "ao":
            raise RuntimeError("AO start failure")

    def is_task_done(self):
        if self.owner.fail == "underrun" and self.kind == "ao":
            raise RuntimeError("AO buffer underflow")
        self.done_checks += 1
        if self.kind == "counter":
            complete = self.owner.ao.written == self.samp_quant_samp_per_chan
            if self.owner.fail == "rate":
                complete = complete and time.monotonic() - self.owner.start_time > .3
            complete = complete and self.done_checks >= 3
            if complete and not self.owner.counter_done:
                self.owner.events.append(("done", "counter"))
                self.owner.counter_done = True
            return complete
        complete = self.written == self.samp_quant_samp_per_chan
        if complete:
            self.total_samp_per_chan_generated = self.written - (self.owner.fail == "sample_count")
        return complete

    def write(self, values, **kwargs):
        self.owner.events.append(("zero", tuple(values)))
        if self.owner.fail == "zero":
            raise RuntimeError("zero failed")
        return 1

    def stop(self):
        self.owner.events.append(("stop", self.kind))
        self.started = False

    def close(self):
        self.owner.events.append(("close", self.kind))


class DAQ:
    def __init__(self, fail=None):
        self.events = []
        self.fail = fail
        self.counter_done = False

    def Task(self):
        result = Task(self)
        if not hasattr(self, "ao"):
            self.ao = result
        return result


class Writer:
    def __init__(self, stream, auto_start):
        assert not auto_start
        self.stream = stream

    def write_many_sample(self, values, timeout):
        assert values.flags.c_contiguous
        task = self.stream
        task.owner.events.append(("write", values.shape[1]))
        if task.owner.fail == "write" and task.started:
            raise RuntimeError("stream failure")
        count = values.shape[1]
        task.written += count
        if task.owner.fail == "short_write":
            return count - 1
        return count


class API:
    path = Path("fake.dll")
    version = "2.0"

    def __init__(self, daq, stop):
        self.daq = daq
        self.stop = stop
        self.calls = []
        self.reads = 0

    def bind(self, name, signature):
        pass

    def call(self, name, *args):
        self.calls.append((name, *[a for a in args if isinstance(a, (int, str))]))
        if name == "OpenDevice":
            args[1].value = b"test"
        elif name == "StartMeas":
            self.daq.events.append(("ph_start", ""))
        elif name == "StopMeas":
            self.daq.events.append(("ph_stop", ""))
        elif name == "GetFlags":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 2 if self.daq.fail == "flags" else 0
        elif name == "CTCStatus":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 1 if self.daq.fail == "ph_stopped" else 0
        elif name == "ReadFiFo":
            self.reads += 1
            count = 1 if self.reads == 1 or self.reads == 6 else 0
            args[1][0] = 0xFE000001
            ct.cast(args[2], ct.POINTER(ct.c_int))[0] = count
            if self.daq.fail == "stop":
                self.stop.set()
        elif name == "GetSyncPeriod":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 2e-6
        elif name == "GetElapsedMeasTime":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 1000
        return 0


class RunTests(unittest.TestCase):
    def session(self, fail=None, changes=None):
        cfg = config()
        cfg.update(changes or {})
        stop = threading.Event()
        daq = DAQ(fail)
        api = API(daq, stop)
        with tempfile.TemporaryDirectory() as temp:
            cfg["output"] = Path(temp)
            with patch.object(run, "BUFFER_SAMPLES", 6), patch.object(run, "CHUNK_SAMPLES", 2), \
                    patch.object(run, "TAIL_SECONDS", 0), \
                    patch.object(run.photon, "configure", return_value={"input_count": 2}), \
                    patch.object(run.photon, "rates", return_value={"sync_hz": 2000000, "input_hz": [0, 0]}), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = run.run(cfg, stop, daq=daq, system=SYSTEM, constants=CONSTANTS,
                               api_factory=lambda _: api, writer_factory=Writer)
            folder = next(Path(temp).iterdir())
            record = json.loads((folder / "session.json").read_text())
            raw = json.loads((folder / "tttr/metadata.json").read_text()) if (folder / "tttr/metadata.json").exists() else None
            data = (folder / "tttr/events.t3raw").read_bytes() if (folder / "tttr/events.t3raw").exists() else None
        return code, record, raw, data, daq, api

    def test_plan_quantizes_both_clocks_without_loading_hardware(self):
        cfg = config()
        cfg.update(frequency_hz=750000, delay_ns=900)
        plan = run.prepare_plan(cfg)
        self.assertEqual(plan["period_ticks"], 133)
        self.assertEqual(plan["ph330"]["laser_hz"], 100000000 / 133)
        self.assertEqual(cfg["ph330"]["laser_hz"], 500000)
        self.assertEqual(plan["samples_per_channel"], 20)
        self.assertIsNone(plan["sync_origin"])

    def test_invalid_plan_never_creates_output(self):
        for change in ({"trial_count": 0}, {"warmup_count": -1}, {"trial_count": 2**32},
                       {"delay_ns": 1950}, {"mode": "bad"}, {"frequency_hz": 750000000},
                       {"alice_voltages": {"H": 0}}, {"labels_confirmed": "yes"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                cfg = config()
                cfg.update(change)
                run.prepare_plan(cfg)

    def test_full_finite_stream_saved_but_origin_stays_unverified(self):
        code, record, raw, data, daq, api = self.session()
        self.assertEqual(code, 0)
        self.assertTrue(record["complete"])
        self.assertTrue(record["ni_sequence_complete"])
        self.assertEqual(record["ao_samples_generated"], 20)
        self.assertEqual(record["ao_stream"]["samples_written"], 20)
        self.assertEqual(record["alignment_status"], "unverified")
        self.assertIsNone(record["sync_origin"])
        self.assertFalse(record["eligible_for_key_processing"])
        self.assertTrue(raw["complete"])
        self.assertEqual(len(data), raw["records"] * 4)
        self.assertTrue(record["ao_zeroed_on_exit"])
        self.assertEqual(daq.ao.regen_mode, "no_regen")
        self.assertFalse(daq.ao.ao_use_only_on_brd_mem)
        self.assertEqual(daq.ao.sample_mode, "finite")
        self.assertIn(("SetMeasControl", 0, 6, 0, 0), api.calls)
        events = daq.events
        self.assertLess(events.index(("ph_start", "")), events.index(("start", "counter")))
        self.assertLess(events.index(("start", "counter")), events.index(("start", "ao")))
        self.assertLess(events.index(("done", "counter")), events.index(("stop", "ao")))
        self.assertLess(events.index(("stop", "counter")), events.index(("stop", "ao")))
        self.assertLess(events.index(("stop", "ao")), events.index(("zero", (0., 0.))))
        self.assertEqual([e for e in events if e[0] == "counter"], [("counter", "Dev1/ctr0")])

    def test_commit_coercion_and_prefill_errors_prevent_outputs(self):
        for fail in ("commit", "coercion", "short_write"):
            with self.subTest(fail=fail):
                code, record, _, _, daq, _ = self.session(fail)
                self.assertEqual(code, 1)
                self.assertFalse(record["complete"])
                self.assertFalse(any(e[0] == "start" for e in daq.events))

    def test_stop_retains_incomplete_data_and_zeroes_outputs(self):
        code, record, raw, data, daq, _ = self.session("stop")
        self.assertEqual(code, 130)
        self.assertEqual(record["status"], "interrupted")
        self.assertFalse(raw["complete"])
        self.assertGreater(len(data), 0)
        self.assertTrue(record["ao_zeroed_on_exit"])
        self.assertLess(daq.events.index(("stop", "counter")), daq.events.index(("ph_stop", "")))

    def test_loss_writer_failure_start_failure_and_wrong_count_are_incomplete(self):
        for fail in ("flags", "underrun", "write", "start", "sample_count", "zero", "ph_stopped"):
            with self.subTest(fail=fail):
                code, record, raw, _, daq, api = self.session(fail)
                self.assertEqual(code, 1)
                self.assertFalse(record["complete"])
                self.assertFalse(raw["complete"])
                self.assertIn(("StopMeas", 0), api.calls)
                self.assertIn(("CloseDevice", 0), api.calls)
                self.assertIn(("close", "counter"), daq.events)

    def test_mismatch_is_checked_after_counter_started(self):
        code, record, raw, _, daq, _ = self.session("rate")
        self.assertEqual(code, 1)
        self.assertIn("SYNC rate", record["error"])
        self.assertEqual(record["rates_during"]["sync_hz"], 2000000)
        self.assertTrue(record["hardware_started"])
        self.assertFalse(raw["complete"])

    def test_label_confirmation_does_not_claim_sync_alignment(self):
        code, record, _, _, _, _ = self.session(changes={"mode": "random", "labels_confirmed": True})
        self.assertEqual(code, 0)
        self.assertTrue(record["labels_confirmed"])
        self.assertFalse(record["eligible_for_key_processing"])
        self.assertIsNone(record["sync_origin"])


if __name__ == "__main__":
    unittest.main()
