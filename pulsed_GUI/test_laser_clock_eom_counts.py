"""Offline live-alignment tests; no physical hardware is accessed."""
import contextlib
import ctypes as ct
import csv
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import gui_app as gui
import laser_clock_eom_counts as live
from live_counts_window import LiveCountsWindow
from test_laser_clock_eom import FakeDAQ, FakeTask
from test_ph330_acquire import FakeAPI
from test_pulsed_polarization import dependencies


def values():
    v = gui.defaults(live.TOOL)
    v.update({"sync-edge": "falling", "ch1-edge": "rising", "ch2-edge": "falling", "serial": "test", "poll-ms": "100"})
    return v


class CountAPI(FakeAPI):
    def __init__(self, stop, on_read=None, fail=False, zero=False):
        super().__init__()
        self.stop, self.on_read, self.fail, self.zero = stop, on_read, fail, zero
        self.reads = 0
        self.arguments = []

    def call(self, name, *args):
        self.arguments.append((name, args))
        result = super().call(name, *args)
        if name == "GetAllCountRates":
            if self.fail:
                raise RuntimeError("USB read failed")
            self.reads += 1
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 0 if self.zero else 500000
            args[2][0], args[2][1] = (0, 0) if self.zero else (100, 200)
            if self.on_read:
                self.on_read(self.reads)
            else:
                self.stop.set()
        return result


class AlignmentTests(unittest.TestCase):
    def test_offline_validation_gain_and_limits(self):
        v = values()
        v.update({"eom1-v": "100", "eom2-v": "-40", "frequency-hz": "1000"})
        cfg = live.settings(v)
        self.assertEqual(cfg["eom"]["daq_v"], [-5, 2])
        self.assertEqual(cfg["ph330"]["detectors"][1]["edge"], "falling")
        with patch.object(live, "PH330") as factory, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute(live.TOOL, v, threading.Event()), 0)
            factory.assert_not_called()
        for key, value in (("eom1-v", "201"), ("frequency-hz", "1000001"), ("frequency-hz", "nan"),
                           ("high-ns", "900"), ("poll-ms", "50"), ("average-s", "inf")):
            bad = values()
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                live.settings(bad)

    def test_rate_meter_configuration_does_not_require_t3_period_range(self):
        cfg = live.settings(dict(values(), **{"frequency-hz": "1000"}))["ph330"]
        api = FakeAPI()
        with self.assertRaisesRegex(ValueError, "microtime"):
            live.ph.configure(api, cfg)
        live.ph.configure(api, cfg, rate_meters_only=True)
        self.assertNotIn("StartMeas", api.calls)

    def run_fake(self, temp, daq=None, api=None, stop=None, commands=None, emit=None, serial="test"):
        stop = stop if stop is not None else threading.Event()
        daq = daq if daq is not None else FakeDAQ()
        api = api if api is not None else CountAPI(stop)
        commands = commands if commands is not None else queue.Queue(maxsize=1)
        samples = queue.Queue(maxsize=1)
        events = []
        cfg = live.settings(dict(values(), output=temp, serial=serial))
        constants, system = dependencies()
        with contextlib.redirect_stdout(io.StringIO()):
            code = live.run_session(cfg, stop, commands, samples, emit or events.append,
                                    daq=daq, constants=constants, system=system, api=api)
        path = next(Path(temp).glob("*/session.json"))
        return code, json.loads(path.read_text()), daq, api, samples, events, path.parent

    def test_reads_and_cleanup_without_any_tttr_or_ni_detector_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, meta, daq, api, samples, events, folder = self.run_fake(tmp)
            self.assertEqual(code, 130)
            self.assertEqual(samples.get()["sum_hz"], 300)
            self.assertEqual(meta["samples"], 1)
            self.assertTrue(meta["ao_zeroed_on_exit"])
            self.assertEqual(api.calls[-1], "CloseDevice")
            self.assertNotIn("StartMeas", api.calls)
            self.assertNotIn("ReadFiFo", api.calls)
            self.assertEqual([e[1] for e in daq.events if e[0] == "clock_route"], ["Dev1/ctr0"])
            self.assertEqual(daq.events[-4:], [("stop", "clock"), ("close", "clock"), ("write", [0, 0]), ("close", "ao")])
            self.assertTrue((folder / "rates.csv").is_file())

    def test_live_eom_keeps_clock_running_rate_change_restarts_and_logs(self):
        stop, commands, daq = threading.Event(), queue.Queue(maxsize=1), FakeDAQ()
        def on_read(n):
            if n == 1:
                commands.put(("eom", live.eom_plan(40, -60)))
            elif n == 2:
                commands.put(("clock", live.live_clock(750000, 100)))
            else:
                stop.set()
        api = CountAPI(stop, on_read=on_read)
        with tempfile.TemporaryDirectory() as tmp:
            code, meta, daq, api, samples, _, folder = self.run_fake(tmp, daq, api, stop, commands)
            self.assertEqual(code, 130)
            writes = [e for e in daq.events if e[0] == "write"]
            self.assertIn(("write", [-2, 3]), writes)
            self.assertEqual([e for e in daq.events if e[0] == "start"], [("start", "clock")] * 2)
            self.assertLess(daq.events.index(("write", [-2, 3])), daq.events.index(("stop", "clock")))
            with (folder / "rates.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([row["epoch"] for row in rows], ["0", "1", "2"])
            self.assertEqual(float(rows[1]["alice_v"]), 40)
            self.assertAlmostEqual(float(rows[2]["commanded_hz"]), 100e6 / 133)
            self.assertEqual(samples.qsize(), 1)
            self.assertEqual(meta["samples"], 3)

    def test_invalid_adjustment_preserves_outputs_and_zero_rates_keep_running(self):
        stop, commands = threading.Event(), queue.Queue(maxsize=1)
        def on_read(n):
            if n == 1:
                commands.put(("eom", {"target_eom_v": [999, 0]}))
            elif n >= 2:
                stop.set()
        with tempfile.TemporaryDirectory() as tmp:
            code, meta, daq, _, samples, events, _ = self.run_fake(tmp, api=CountAPI(stop, on_read, zero=True), stop=stop, commands=commands)
            self.assertEqual(code, 130)
            self.assertEqual(meta["samples"], 2)
            self.assertTrue(any(e["kind"] == "rejected" for e in events))
            self.assertEqual([e for e in daq.events if e[0] == "write"], [("write", [0, 0])] * 2)
            sample = samples.get()
            self.assertEqual(sample["sum_hz"], 0)
            self.assertFalse(sample["sync_matches"])

    def test_restart_failure_stops_and_zeroes_outputs(self):
        stop, commands, daq = threading.Event(), queue.Queue(maxsize=1), FakeDAQ()
        def on_read(n):
            daq.fail_reserve = True
            commands.put(("clock", live.live_clock(750000, 100)))
        with tempfile.TemporaryDirectory() as tmp:
            code, meta, daq, api, *_ = self.run_fake(tmp, daq, CountAPI(stop, on_read), stop, commands)
            self.assertEqual(code, 1)
            self.assertTrue(meta["ao_zeroed_on_exit"])
            self.assertEqual(api.calls[-1], "CloseDevice")
            self.assertEqual([e for e in daq.events if e[0] == "start"], [("start", "clock")])

    def test_serial_and_reservation_failures_never_start_output(self):
        for serial, conflict in (("wrong", False), ("test", True)):
            with self.subTest(serial=serial), tempfile.TemporaryDirectory() as tmp:
                code, meta, daq, api, *_ = self.run_fake(tmp, daq=FakeDAQ(fail_reserve=conflict), serial=serial)
                self.assertEqual(code, 1)
                self.assertFalse(any(e[0] == "start" for e in daq.events))
                self.assertEqual(api.calls[-1], "CloseDevice")

    def test_usb_failure_cleans_all_owned_resources(self):
        stop = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            code, meta, daq, api, *_ = self.run_fake(tmp, api=CountAPI(stop, fail=True), stop=stop)
            self.assertEqual(code, 1)
            self.assertIn("USB", meta["error"])
            self.assertTrue(meta["ao_zeroed_on_exit"])
            self.assertEqual(api.calls[-1], "CloseDevice")

    def test_zeroing_failure_is_reported_and_other_resources_still_close(self):
        class Task(FakeTask):
            def write(self, values, **kwargs):
                result = super().write(values, **kwargs)
                if self.owner.events.count(("write", [0, 0])) > 1:
                    raise RuntimeError("zeroing failed")
                return result
        daq = FakeDAQ()
        daq.Task = lambda: Task(daq)
        with tempfile.TemporaryDirectory() as tmp:
            code, meta, daq, api, *_ = self.run_fake(tmp, daq=daq)
            self.assertEqual(code, 1)
            self.assertFalse(meta["ao_zeroed_on_exit"])
            self.assertIn("zeroing failed", meta["cleanup_errors"][0])
            self.assertEqual(api.calls[-1], "CloseDevice")
            self.assertEqual(daq.events[-1], ("close", "ao"))

    def test_stop_before_start_accesses_nothing(self):
        stop = threading.Event()
        stop.set()
        with patch.object(live, "PH330") as hardware:
            self.assertEqual(live.run_session(live.settings(values()), stop, queue.Queue(), queue.Queue(), lambda v: None), 130)
            hardware.assert_not_called()


def pump(root, predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        root.update()
        time.sleep(.01)
    if not predicate():
        raise AssertionError("GUI condition timed out")


class AlignmentGuiTests(unittest.TestCase):
    def test_closing_launcher_requests_worker_stop_before_destroy(self):
        ended = threading.Event()
        def worker(cfg, stop, commands, samples, emit):
            emit(dict(kind="applied", epoch=0, clock=cfg["clock"], eom=cfg["eom"]))
            stop.wait(5)
            ended.set()
            emit(dict(kind="cleanup", zeroed=True, errors=[]))
            return 130
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui, "SETTINGS", Path(tmp) / "settings.json"), \
                patch.object(live, "run_session", side_effect=worker):
            root = tk.Tk()
            root.withdraw()
            app = gui.App(root, live.TOOL)
            for key, value in values().items():
                app.vars[key].set(value)
            app.vars["action"].set("Start live controls")
            app.start()
            pump(root, lambda: app.live_counts.epoch == 0)
            app.close()
            pump(root, lambda: not app.running)
            self.assertTrue(ended.is_set())
            self.assertTrue(app.stop_event.is_set())
            self.assertIsNone(app.poll_id)

    def test_seeds_saved_inputs_without_copying_wrong_laser_rate(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui, "SETTINGS", Path(tmp) / "settings.json"):
            gui.SETTINGS.write_text(json.dumps({"G2 acquisition": {"sync-mode": "cfd", "sync-level-mv": "-60",
                                    "sync-zero": "-10", "ch2-edge": "falling", "laser-hz": "2000000", "action": "Record"}}))
            root = tk.Tk()
            root.withdraw()
            app = gui.App(root, live.TOOL)
            try:
                self.assertEqual(app.vars["sync-mode"].get(), "cfd")
                self.assertEqual(app.vars["ch2-edge"].get(), "falling")
                self.assertEqual(app.vars["frequency-hz"].get(), "500000")
                self.assertEqual(app.vars["action"].get(), "Check settings")
            finally:
                app.close()

    def test_live_window_adjustments_plot_and_close_stop_worker(self):
        received = []
        def worker(cfg, stop, commands, samples, emit):
            epoch = 0
            clock, eom = cfg["clock"], cfg["eom"]
            def acknowledge():
                emit(dict(kind="applied", epoch=epoch, clock=clock, eom=eom))
                live.latest(samples, dict(epoch=epoch, elapsed_s=epoch + 1, ch1_hz=100, ch2_hz=0,
                                          sync_hz=500000, sync_matches=True, warnings=0, warnings_text=""))
            acknowledge()
            while not stop.wait(.005):
                try:
                    kind, plan = commands.get_nowait()
                except queue.Empty:
                    continue
                received.append((kind, plan))
                if kind == "eom":
                    eom = plan
                else:
                    clock = plan
                epoch += 1
                acknowledge()
            emit(dict(kind="cleanup", zeroed=True, errors=[]))
            return 130

        with tempfile.TemporaryDirectory() as tmp, patch.object(gui, "SETTINGS", Path(tmp) / "settings.json"), \
                patch.object(live, "run_session", side_effect=worker):
            root = tk.Tk()
            root.withdraw()
            app = gui.App(root, live.TOOL)
            try:
                for key, value in values().items():
                    app.vars[key].set(value)
                app.vars["action"].set("Start live controls")
                app.start()
                window = app.live_counts
                pump(root, lambda: window.epoch == 0)
                self.assertEqual(window.cards["CH2"].get(), "0.0")
                window.vars["eom1-v"].set("80")
                window.eom_button.invoke()
                pump(root, lambda: window.epoch == 1)
                self.assertEqual(received[0][1]["daq_v"], [-4, 0])
                self.assertEqual(app.vars["eom1-v"].get(), "80")
                self.assertEqual(len(window.average), 1)
                window.vars["frequency-hz"].set("750000")
                window.clock_button.invoke()
                pump(root, lambda: window.epoch == 2)
                self.assertAlmostEqual(received[1][1]["realized_nominal_frequency_hz"], 100e6 / 133)
                window.show_sum.set(True)
                window.log_scale.set(True)
                window.draw()
                root.update()
                self.assertTrue(window.lines[2].get_visible())
                self.assertEqual(window.ax.get_yscale(), "log")
                window.request_close()
                pump(root, lambda: not app.running)
                self.assertTrue(app.stop_event.is_set())
                self.assertFalse(window.exists())
                self.assertEqual(app.status.get(), "Stopped")
            finally:
                if app.running:
                    app.stop()
                    pump(root, lambda: not app.running)
                app.close()


if __name__ == "__main__":
    unittest.main()
