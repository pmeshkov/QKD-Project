"""Fake-device tests: independent AO/clock adjustments and failure cleanup."""
import contextlib
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import gui_app as gui
import laser_clock_eom as combined
from laser_clock import clock_plan


class FakeTask:
    def __init__(self, owner):
        self.owner = owner
        self.kind = None
        self.ao_channels = self.co_channels = self.timing = self
        self.started = False

    def add_ao_voltage_chan(self, path, **kwargs):
        self.kind = "ao"
        self.owner.events.append(("ao_route", path))

    def add_co_pulse_chan_ticks(self, path, **kwargs):
        self.kind = "clock"
        self.owner.events.append(("clock_route", path, kwargs))
        self.co_pulse_high_ticks = kwargs["high_ticks"]
        self.co_pulse_low_ticks = kwargs["low_ticks"]
        return self

    def cfg_implicit_timing(self, **kwargs):
        pass

    def control(self, mode):
        if self.owner.fail_reserve and self.kind == "clock":
            raise RuntimeError("counter already reserved")

    def write(self, values, **kwargs):
        self.owner.events.append(("write", list(values)))
        if self.owner.fail_write and values != [0, 0]:
            raise RuntimeError("write failed")
        return 1

    def start(self):
        self.started = True
        self.owner.events.append(("start", self.kind))

    def stop(self):
        self.started = False
        self.owner.events.append(("stop", self.kind))

    def close(self):
        self.owner.events.append(("close", self.kind))

    def is_task_done(self):
        return not self.started


class FakeDAQ:
    def __init__(self, fail_write=False, fail_reserve=False):
        self.events = []
        self.fail_write = fail_write
        self.fail_reserve = fail_reserve

    def Task(self):
        return FakeTask(self)


class Commands:
    def __init__(self, items, stop):
        self.items = iter(items)
        self.stop = stop

    def get(self, timeout):
        try:
            return next(self.items)
        except StopIteration:
            self.stop.set()
            raise queue.Empty


class CombinedTests(unittest.TestCase):
    def session(self, items=(), **fake_options):
        daq = FakeDAQ(**fake_options)
        stop = threading.Event()
        system = NS(devices={"Dev1": NS(product_type="USB-6351", dev_is_simulated=False, dev_serial_num=1)})
        constants = NS(Level=NS(LOW="LOW"), TaskMode=NS(TASK_VERIFY="verify", TASK_COMMIT="commit"),
                       AcquisitionType=NS(CONTINUOUS="continuous"))
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            args = NS(device="Dev1", frequency_hz=1e6, high_ns=200, seconds=0,
                      eom1_v=20, eom2_v=-40, note="fake", log_dir=Path(temp))
            code = combined.run_session(args, stop, Commands(items, stop), daq, system, constants)
            record = json.loads(next(Path(temp).glob("*.json")).read_text())
        return code, record, daq.events

    def test_voltage_mapping_and_validation(self):
        self.assertEqual(combined.eom_plan(200, -100)["daq_v"], [-10, 5])
        for value in (201, -201, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                combined.eom_plan(value, 0)

    def test_dry_run_never_opens_hardware(self):
        with patch.object(combined, "run_session") as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(combined.main([]), 0)
            self.assertEqual(combined.main(["--run", "--eom1-v=nan"]), 1)
            self.assertEqual(combined.main(["--run", "--seconds=-1"]), 1)
            run.assert_not_called()

    def test_eom_update_keeps_clock_running_clock_update_holds_ao(self):
        code, record, events = self.session([
            ("eom", combined.eom_plan(60, -80)), ("clock", clock_plan(500000, 200))])
        self.assertEqual(code, 130)
        self.assertEqual([e for e in events if e[0] == "write"],
                         [("write", [-1, 2]), ("write", [-3, 4]), ("write", [0, 0])])
        eom_update = events.index(("write", [-3, 4]))
        self.assertLess(events.index(("start", "clock")), eom_update)
        self.assertGreater(events.index(("stop", "clock")), eom_update)
        self.assertEqual([e for e in events if e[0] == "start"], [("start", "clock")]*2)
        self.assertEqual(events[-4:], [("stop", "clock"), ("close", "clock"),
                                      ("write", [0, 0]), ("close", "ao")])
        self.assertTrue(record["ao_zeroed_on_exit"])
        self.assertIn("clock_restarted", [e["kind"] for e in record["events"]])
        self.assertTrue(all(e[1] == "Dev1/ctr0" for e in events if e[0] == "clock_route"))

    def test_failure_zeroes_ao_and_closes_both_tasks(self):
        code, record, events = self.session(fail_write=True)
        self.assertEqual(code, 1)
        self.assertEqual(record["status"], "error")
        self.assertTrue(record["ao_zeroed_on_exit"])
        self.assertNotIn(("start", "clock"), events)
        self.assertIn(("close", "clock"), events)
        self.assertEqual(events[-1], ("close", "ao"))

    def test_conflicting_counter_does_not_start_or_apply_target(self):
        code, record, events = self.session(fail_reserve=True)
        self.assertEqual(code, 1)
        self.assertNotIn(("write", [-1, 2]), events)
        self.assertNotIn(("start", "clock"), events)
        self.assertIn("reserved", record["error"])

    def test_invalid_queued_adjustment_does_not_change_outputs(self):
        code, _, events = self.session([("eom", {"target_eom_v": [999, 0]})])
        self.assertEqual(code, 130)
        self.assertEqual([e for e in events if e[0] == "write"],
                         [("write", [-1, 2]), ("write", [0, 0])])

    def test_gui_live_controls_enqueue_and_stop(self):
        ready = threading.Event()
        received = []

        def fake_execute(tool, values, stop_event, commands=None):
            ready.set()
            while not stop_event.wait(0.005):
                try:
                    received.append(commands.get_nowait())
                except queue.Empty:
                    pass
            return 130

        with tempfile.TemporaryDirectory() as temp, patch.object(gui, "SETTINGS", Path(temp)/"settings.json"), \
                patch.object(gui, "execute", side_effect=fake_execute):
            root = tk.Tk()
            root.withdraw()
            app = gui.App(root, "Laser clock + EOM")
            try:
                app.vars["action"].set("Output clock + EOM")
                app.run_button.invoke()
                self.assertTrue(ready.wait(2))
                self.assertTrue(all(str(field["state"]) == "normal" for field in app.live_inputs))
                app.vars["eom1-v"].set("60")
                app.vars["eom2-v"].set("-80")
                app.apply_eom_button.invoke()
                end = time.monotonic()+2
                while not received and time.monotonic() < end:
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(received[0], ("eom", combined.eom_plan(60, -80)))
                app.vars["frequency-hz"].set("500000")
                app.apply_clock_button.invoke()
                end = time.monotonic()+2
                while len(received) < 2 and time.monotonic() < end:
                    root.update()
                    time.sleep(0.01)
                self.assertEqual(received[1][0], "clock")
                app.stop_button.invoke()
                end = time.monotonic()+2
                while app.running and time.monotonic() < end:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app.running)
                self.assertEqual(str(app.apply_eom_button["state"]), "disabled")
                self.assertEqual(app.status.get(), "Stopped")
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
