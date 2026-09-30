"""Offline checks for the consolidated forms and BB84 preparation actions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

import bb84_controls as bb84
import gui_app as gui
import pulsed_polarization as pol


class WorkflowTests(unittest.TestCase):
    def test_internal_rate_choices_ignore_stale_external_rate(self):
        for name, hz in pol.INTERNAL_SOURCES.items():
            values = gui.defaults("Pulsed polarization")
            values.update(source=name, **{"laser-hz": "500000"})
            cfg = pol.settings(values)
            self.assertIsNone(cfg["clock"])
            self.assertEqual(cfg["ph330"]["laser_hz"], hz)

    def test_new_bb84_profiles_preserve_opposite_pairs_and_do_not_guess_labels(self):
        previous = {f"alice-{i}-v": str(v) for i, v in enumerate((-40, 60, 160, -140))}
        previous.update({"alice-labels": "Voltage index only", "alice-0-state": "L",
                         "alice-1-state": "V", "alice-2-state": "L", "alice-3-state": "R",
                         "ch2-offset-ns": "4.5", "seconds": "100"})
        saved = gui.initial_settings(bb84.RANDOM, {"Pulsed polarization": previous})
        self.assertEqual([saved[f"alice-{s}-v"] for s in "hvrl"], ["-40", "160", "60", "-140"])
        self.assertEqual(saved["labels-confirmed"], "No")
        self.assertEqual(saved["ch2-offset-ns"], "4.5")
        self.assertNotIn("seconds", saved)
        explicit = {"alice-h-v": "12", "seconds": "3"}
        self.assertEqual(gui.initial_settings(bb84.RANDOM, {bb84.RANDOM: explicit}), explicit)

    def test_gui_source_preset_tabs_and_saved_settings(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gui, "SETTINGS", Path(temp)/"settings.json"):
            root = tk.Tk()
            root.withdraw()
            app = gui.App(root, "Pulsed polarization")
            try:
                self.assertEqual(set(app.pages), {"Run", "Voltages", "PicoHarp", "Analysis", "Files"})
                app.vars["laser-hz"].set("750000")
                app.vars["source"].set("Laser internal 20 MHz")
                self.assertEqual(app.vars["laser-hz"].get(), "20000000")
                self.assertEqual(str(app.fields["laser-hz"][0].cget("state")), "disabled")
                app.vars["internal-ready"].set("Yes")
                app.vars["source"].set("Laser internal 80 MHz")
                self.assertEqual(app.vars["internal-ready"].get(), "No")
                app.vars["source"].set("NI external")
                self.assertEqual(app.vars["laser-hz"].get(), "750000")
                app.tool.set(bb84.RANDOM)
                app.change_tool()
                app.vars["timing-profile"].set("750 kHz / 900 ns")
                self.assertEqual(app.vars["delay-ns"].get(), "900")
                app.vars["timing-profile"].set("Custom")
                self.assertEqual(str(app.fields["delay-ns"][0].cget("state")), "normal")
                app.vars["ch2-mode"].set("cfd")
                self.assertEqual(str(app.fields["ch2-edge"][0].cget("state")), "disabled")
                self.assertEqual(str(app.fields["ch2-zero"][0].cget("state")), "normal")
                app.vars["action"].set("Analyze saved run")
                self.assertEqual(str(app.fields["seconds"][0].cget("state")), "disabled")
                self.assertEqual(str(app.fields["analysis-origin"][0].cget("state")), "normal")
                app.vars["action"].set("Record")
                self.assertEqual(str(app.fields["seconds"][0].cget("state")), "normal")
                self.assertEqual(str(app.fields["analysis-origin"][0].cget("state")), "disabled")
                self.assertEqual(str(app.fields["ch2-edge"][0].cget("state")), "disabled")
                app.tool.set("Polarization matrix")
                app.change_tool()
                self.assertEqual(app.current_tool, "Pulsed polarization")
                self.assertEqual(app.vars["laser-hz"].get(), "750000")
            finally:
                app.close()

    def test_prepare_action_saves_complete_sequence_without_hardware(self):
        with tempfile.TemporaryDirectory() as temp, patch("bb84_run.PH330") as hardware, \
                patch("bb84_run.run") as run, contextlib.redirect_stdout(io.StringIO()):
            values = gui.defaults(bb84.ORDERED)
            values.update(action="Prepare sequence only", output=temp, seconds=".001", **{"warmup-s": "0"})
            self.assertEqual(gui.execute(bb84.ORDERED, values, threading.Event()), 0)
            manifest = json.loads(next(Path(temp).glob("*/sequence.json")).read_text())
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["trial_count"], 500)
            hardware.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
