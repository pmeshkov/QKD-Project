"""Offline scope-test checks. No nidaqmx device is opened."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np
import eom_timing_scope as scope
import gui_app as gui


def args(**changes):
    result = NS(device="Dev1", frequency_hz=1e6, delay_ns=500., high_ns=100.,
                hold_pulses=20, alice_a_v=-80., alice_b_v=120.,
                bob_a_v=40., bob_b_v=40., seconds=0., note="offline test", output=None)
    vars(result).update(changes)
    return result


class FakeTask:
    def __init__(self, owner):
        self.owner = owner
        self.kind = "unconfigured"
        self.ao_channels = self.co_channels = self.timing = self.out_stream = self
        self.all = self
        self.triggers = NS(start_trigger=self)
        self.started = False

    def add_ao_voltage_chan(self, path, **kw):
        self.kind = "ao"
        self.owner.events.append(("ao_channels", path))

    def add_co_pulse_chan_ticks(self, path, **kw):
        self.kind = "co"
        self.co_ctr_timebase_src = kw["source_terminal"]
        self.co_pulse_ticks_initial_delay = kw["initial_delay"]
        self.co_pulse_high_ticks = kw["high_ticks"]
        self.co_pulse_low_ticks = kw["low_ticks"]
        self.owner.events.append(("counter", path))
        return self

    def cfg_samp_clk_timing(self, rate, **kw):
        self.samp_clk_rate = rate
        self.sample_mode = kw["sample_mode"]

    def cfg_implicit_timing(self, **kw):
        self.sample_mode = kw["sample_mode"]

    def cfg_dig_edge_start_trig(self, source, **kw):
        self.dig_edge_src = source
        self.owner.events.append(("start_source", source))

    def control(self, mode):
        self.owner.events.append((mode, self.kind))
        if self.owner.fail == "commit" and self.kind == "co" and mode == "commit":
            raise RuntimeError("route or resource unavailable")
        if self.owner.fail == "coerced" and self.kind == "ao" and mode == "commit":
            self.samp_clk_timebase_div += 1

    def write(self, data, **kw):
        array = np.asarray(data)
        if array.ndim == 2:
            assert not kw["auto_start"]
            self.owner.events.append(("load", array.shape))
            self.owner.waveform = array.copy()
            return array.shape[1] - (self.owner.fail == "short_write")
        self.owner.events.append(("zero", list(data)))
        if self.owner.fail == "zero":
            raise RuntimeError("zero failed")
        return 1

    def start(self):
        self.owner.events.append(("start", self.kind))
        if self.owner.fail == "ao_start" and self.kind == "ao":
            raise RuntimeError("AO start failed")
        self.started = True
        if self.kind == "ao":
            self.owner.stop.set()

    def is_task_done(self):
        return not self.started

    def stop(self):
        self.owner.events.append(("stop", self.kind))
        self.started = False

    def close(self):
        self.owner.events.append(("close", self.kind))


class FakeDAQ:
    def __init__(self, stop, fail=None):
        self.stop, self.fail, self.events = stop, fail, []

    def Task(self):
        return FakeTask(self)


class ScopeTests(unittest.TestCase):
    def test_quantization_prevents_clock_drift(self):
        p = scope.make_plan(args(frequency_hz=750000, delay_ns=503, high_ns=103))
        self.assertEqual(p["period_ticks"], 133)
        self.assertAlmostEqual(p["realized_rate_hz"], 100_000_000/133)
        self.assertEqual(p["high_ticks"] + p["low_ticks"], p["period_ticks"])
        self.assertEqual(p["nominal_delay_ns"], 500)
        self.assertEqual(p["high_ns"], 100)

    def test_waveform_order_gain_and_dwell(self):
        p = scope.make_plan(args(hold_pulses=3))
        np.testing.assert_array_equal(scope.waveform(p),
                                      [[4, 4, 4, -6, -6, -6], [-2]*6])
        self.assertEqual(p["samples_per_channel"], 6)
        self.assertAlmostEqual(p["ab_pattern_hz"], 1e6/6)

    def test_invalid_timing_voltage_or_buffer_rejected(self):
        for change in ({"frequency_hz": float("nan")}, {"frequency_hz": 2e6},
                       {"delay_ns": 900}, {"high_ns": 310}, {"delay_ns": 1},
                       {"alice_b_v": 201}, {"bob_a_v": float("inf")},
                       {"hold_pulses": 2001}, {"seconds": -1}, {"device": "Dev1/ctr1"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                scope.make_plan(args(**change))

    def test_no_step_or_invalid_settings_never_open_hardware(self):
        with patch.object(scope, "run_session") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(scope.main([]), 0)
            self.assertEqual(scope.main(["--run"]), 1)
            self.assertEqual(scope.main(["--run", "--delay-ns=99999"]), 1)
            run.assert_not_called()

    def session(self, fail=None):
        stop = threading.Event()
        daq = FakeDAQ(stop, fail)
        constants = NS(AcquisitionType=NS(CONTINUOUS="continuous"),
                       TaskMode=NS(TASK_VERIFY="verify", TASK_COMMIT="commit"),
                       RegenerationMode=NS(ALLOW_REGENERATION="regen"),
                       Edge=NS(RISING="rising"), Level=NS(LOW="low"))
        system = NS(devices={"Dev1": NS(product_type="USB-6351", dev_is_simulated=False,
                                       dev_serial_num=42)})
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(scope, "save_preview"), contextlib.redirect_stdout(io.StringIO()):
            settings = args(output=Path(temp))
            code = scope.run_session(settings, scope.make_plan(settings), stop,
                                     daq=daq, system=system, constants=constants)
            record = json.loads(next(Path(temp).glob("*/metadata.json")).read_text())
            csv = next(Path(temp).glob("*/command_cycle.csv")).read_text()
        return code, record, daq.events, csv

    def test_armed_before_first_ao_clock_cleanup_stops_laser_first(self):
        code, record, events, csv = self.session()
        self.assertEqual(code, 0)
        self.assertEqual(record["status"], "stopped")
        self.assertTrue(record["ao_zeroed_on_exit"])
        self.assertLess(events.index(("commit", "co")), events.index(("load", (2, 40))))
        self.assertLess(events.index(("start", "co")), events.index(("start", "ao")))
        self.assertLess(events.index(("stop", "co")), events.index(("stop", "ao")))
        self.assertLess(events.index(("stop", "ao")), events.index(("zero", [0., 0.])))
        self.assertIn(("start_source", "/Dev1/ao/SampleClock"), events)
        self.assertEqual([e for e in events if e[0] == "counter"], [("counter", "Dev1/ctr0")])
        self.assertIn("ni_trigger_time_ns", csv)
        self.assertEqual(len(csv.splitlines()), 41)

    def test_reservation_coercion_and_short_write_prevent_start(self):
        for failure in ("commit", "coerced", "short_write"):
            with self.subTest(failure=failure):
                code, record, events, _ = self.session(failure)
                self.assertEqual(code, 1)
                self.assertFalse(record["hardware_started"])
                self.assertFalse(any(e[0] in ("start", "zero") for e in events))
                self.assertIn(("close", "co"), events)
                self.assertIn(("close", "ao"), events)

    def test_failed_ao_start_disarms_counter_and_zeroes(self):
        code, record, events, _ = self.session("ao_start")
        self.assertEqual(code, 1)
        self.assertTrue(record["ao_zeroed_on_exit"])
        self.assertIn(("stop", "co"), events)

    def test_failed_cleanup_is_not_reported_as_success(self):
        code, record, _, _ = self.session("zero")
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "cleanup_error")
        self.assertFalse(record["ao_zeroed_on_exit"])

    def test_gui_dispatch_dry_run_and_signed_values(self):
        values = gui.defaults("EOM timing scope")
        values.update({"alice-a-v": "-80", "alice-b-v": "120"})
        with patch.object(scope, "run_session") as hardware, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute("EOM timing scope", values, threading.Event()), 0)
            hardware.assert_not_called()
        values["action"] = "Output timing pattern"
        self.assertIn("--run", gui.arguments("EOM timing scope", values))
        self.assertIn("--alice-a-v=-80", gui.arguments("EOM timing scope", values))

    def test_preview_file(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(temp)
            scope.save_preview(folder, scope.make_plan(args()))
            self.assertGreater((folder / "command_preview.png").stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
