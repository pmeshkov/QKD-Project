"""Offline GUI dispatch, window and cooperative hardware-cleanup checks."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import gui_app as gui
import ph330_acquire as acq
from test_ph330_acquire import FakeAPI, config
from test_ph330_lifetime import fixture


class GuiTests(unittest.TestCase):
    def test_analyze_button_saves_and_displays_plot(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gui, "SETTINGS", Path(temp)/"settings.json"):
            folder = Path(temp)/"synthetic_run"
            fixture(folder)
            root = tk.Tk()
            root.withdraw()
            try:
                app = gui.App(root, "CH1 lifetime")
                app.vars["action"].set("Analyze saved run")
                app.vars["folder"].set(str(folder))
                app.run_button.invoke()
                end = time.monotonic() + 20
                while app.running and time.monotonic() < end:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app.running)
                self.assertEqual(app.status.get(), "Complete", app.log.get("1.0", "end"))
                self.assertEqual(app.last_plot, folder/"lifetime_ch1.png")
                self.assertTrue(any(isinstance(child, tk.Toplevel) for child in root.winfo_children()))
                self.assertTrue((folder/"lifetime_ch1.csv").exists())
                saved = json.loads(gui.SETTINGS.read_text())
                self.assertEqual(saved["CH1 lifetime"]["folder"], str(folder))
            finally:
                app.close()

    def test_opening_forms_and_offline_run_never_access_hardware(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gui, "SETTINGS", Path(temp)/"settings.json"), \
                patch.object(acq, "PH330") as hardware:
            root = tk.Tk()
            root.withdraw()
            try:
                app = gui.App(root)
                for tool in gui.TOOLS:
                    app.tool.set(tool)
                    app.change_tool()
                    root.update()
                    self.assertEqual(set(app.values()), set(gui.defaults(tool)))
                app.tool.set("Laser clock")
                app.change_tool()
                app.run_button.invoke()
                end = time.monotonic() + 10
                while app.running and time.monotonic() < end:
                    root.update()
                    time.sleep(0.01)
                self.assertFalse(app.running)
                self.assertEqual(app.status.get(), "Complete")
                self.assertIn("DRY RUN", app.log.get("1.0", "end"))
                hardware.assert_not_called()
            finally:
                app.close()

    def test_lifetime_analyze_does_not_require_hardware_fields(self):
        values = gui.defaults("CH1 lifetime")
        values.update(action="Analyze saved run", folder="a folder with spaces", fit="Yes",
                      **{"fit-start": "40", "fit-stop": "150"})
        args = gui.arguments("CH1 lifetime", values)
        self.assertIn("a folder with spaces", args)
        self.assertNotIn("--dll", args)
        self.assertIn("--fit-window", args)

    def test_unset_sync_requires_input(self):
        for tool in ("G2 acquisition", "CH1 lifetime"):
            with self.subTest(tool=tool), self.assertRaises(ValueError):
                values = gui.defaults(tool)
                gui.arguments(tool, values)
                if tool == "G2 acquisition":
                    gui.acquisition_config(values)

    def test_lifetime_fields_reach_existing_validator_without_dll(self):
        values = gui.defaults("CH1 lifetime")
        values.update({"sync-edge": "rising", "sync-level-mv": "300"})
        with patch("ph330_lifetime.PH330") as hardware, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute("CH1 lifetime", values, threading.Event()), 0)
            hardware.assert_not_called()

    def test_g2_configuration_round_trip(self):
        values = gui.defaults("G2 acquisition")
        values.update({"sync-edge": "falling", "sync-level-mv": "-300"})
        acq.validate(gui.acquisition_config(values))
        with patch.object(acq, "PH330") as hardware, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute("G2 acquisition", values, threading.Event()), 0)
            hardware.assert_not_called()

    def test_stop_during_fifo_retains_records_and_closes(self):
        stop = threading.Event()

        class StopAPI(FakeAPI):
            def call(self, name, *args):
                result = super().call(name, *args)
                if name == "ReadFiFo":
                    stop.set()
                return result

        api = StopAPI([[123]])
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(acq, "configure", return_value={"input_count": 2}), \
                patch.object(acq, "rates", return_value={"sync_hz": 2_000_000, "input_hz": [100, 100]}), \
                patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                acq.run(api, config(), 1, Path(temp), True, stop_event=stop)
            path = next(Path(temp).glob("*/metadata.json"))
            meta = json.loads(path.read_text())
            self.assertEqual(meta["status"], "interrupted")
            self.assertEqual(meta["records"], 1)
            self.assertFalse(meta["complete"])
            self.assertEqual((path.parent/"events.t3raw").stat().st_size, 4)
        self.assertEqual(api.calls[-2:], ["StopMeas", "CloseDevice"])

    def test_stop_before_start_never_starts_device(self):
        stop = threading.Event()
        stop.set()
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(acq, "configure", return_value={"input_count": 2}), \
                patch.object(acq, "rates", return_value={"sync_hz": 2_000_000, "input_hz": [100, 100]}), \
                patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                acq.run(api, config(), 1, Path(temp), True, stop_event=stop)
        self.assertNotIn("StartMeas", api.calls)
        self.assertEqual(api.calls[-1], "CloseDevice")


if __name__ == "__main__":
    unittest.main()
