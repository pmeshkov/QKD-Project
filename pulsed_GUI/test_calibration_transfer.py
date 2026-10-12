"""Offline tests for calibration handoff and acquisition context."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

import calibration_transfer as transfer
import figure_options
import gui_app as gui
import pulsed_polarization as pol


def candidate():
    return transfer.payload(dict(alice_v=[-170, -80, 35, 127], bob_v=[45, -54]), "my sweep.csv")


class TransferTests(unittest.TestCase):
    def test_one_time_profile_repair_preserves_custom_values_and_later_edits(self):
        pol_values = dict(gui.defaults("Pulsed polarization"), **transfer.form_updates(candidate(), True))
        stale = dict(gui.defaults(gui.bb84.RANDOM), **{"ch1-edge": "falling", "seconds": "120", "labels-confirmed": "Yes"})
        custom = dict(gui.defaults(gui.bb84.ORDERED), **{"alice-h-v": "-133"})
        saved = {"Pulsed polarization": pol_values, gui.bb84.RANDOM: stale, gui.bb84.ORDERED: custom}
        messages = gui.migrate_experiment_profiles(saved)
        self.assertTrue(any("CH1" in m for m in messages))
        self.assertEqual(saved[gui.bb84.RANDOM]["ch1-edge"], "rising")
        self.assertEqual(saved[gui.bb84.RANDOM]["alice-h-v"], "-170.0")
        self.assertEqual(saved[gui.bb84.RANDOM]["seconds"], "120")
        self.assertEqual(saved[gui.bb84.RANDOM]["labels-confirmed"], "No")
        self.assertEqual(saved[gui.bb84.ORDERED]["alice-h-v"], "-133")
        saved[gui.bb84.RANDOM]["alice-h-v"] = "-140"
        self.assertEqual(gui.migrate_experiment_profiles(saved), [])
        self.assertEqual(saved[gui.bb84.RANDOM]["alice-h-v"], "-140")

    def test_import_updates_both_modes_preserving_unrelated_settings(self):
        data = candidate()
        settings = {gui.bb84.RANDOM: {"seconds": "120", "alice-h-v": "-152", "labels-confirmed": "Yes"}}
        transfer.publish(settings, data)
        self.assertEqual(settings[gui.bb84.RANDOM]["seconds"], "120")
        for tool in (gui.bb84.ORDERED, gui.bb84.RANDOM):
            values = dict(gui.defaults(tool), **settings[tool])
            cfg = gui.bb84.settings(values, gui.bb84.MODES[tool])
            transfer.require_record_calibration(cfg)
            self.assertEqual(cfg["alice_voltages"], dict(H=-170., V=35., R=-80., L=127.))
            self.assertEqual(cfg["bob_voltages"], dict(HV=45., RL=-54.))
            self.assertFalse(cfg["labels_confirmed"])
            values["alice-h-v"] = "-169"
            cfg = gui.bb84.settings(values, gui.bb84.MODES[tool])
            with self.assertRaisesRegex(ValueError, "Manual voltages"):
                transfer.require_record_calibration(cfg)
            cfg["calibration_mode"] = "Manual voltages"
            transfer.require_record_calibration(cfg)

    def test_candidate_handoff_preserves_indices_and_marks_manual_changes(self):
        data = candidate()
        for indexed, tool in [(True, "Pulsed polarization"), (False, gui.bb84.RANDOM)]:
            values = gui.defaults(tool)
            values.update(transfer.form_updates(data, indexed))
            cfg = pol.settings(values) if indexed else gui.bb84.settings(values, "random")
            self.assertTrue(cfg["calibration_transfer"]["current_voltages_match_import"])
            self.assertEqual(list(cfg["alice" if indexed else "alice_voltages"].values()),
                             [-170, -80, 35, 127] if indexed else [-170, 35, -80, 127])
            if indexed:
                self.assertEqual(cfg["mapping"], {"HV": "Unassigned", "RL": "Unassigned"})
            else:
                self.assertFalse(cfg["labels_confirmed"])
            values["bob-hv-v"] = "46"
            self.assertFalse(transfer.context(values)["current_voltages_match_import"])

    def test_invalid_payloads_cannot_partially_edit_form(self):
        for change in [{"units": "DAQ V"}, {"alice_v": [0, 1, 2]}, {"bob_v": [45, 201]},
                       {"bob_v": [45, float("nan")]}, {"alice_labels": ["H", "V", "R", "L"]},
                       {"bob_v": [1, 1]}, {"physical_labels_confirmed": True}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                transfer.parse(json.dumps(dict(candidate(), **change)))
        self.assertEqual(transfer.parse("```json\n" + json.dumps(candidate()) + "\n```"), candidate())

    def test_gui_paste_is_offline_and_atomic(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui, "SETTINGS", Path(tmp)/"gui.json"):
            root = tk.Tk(); root.withdraw()
            app = gui.App(root, "Pulsed polarization")
            try:
                with patch.object(root, "clipboard_get", return_value=json.dumps(candidate())), patch.object(pol, "record") as hardware:
                    app.paste_calibration()
                    hardware.assert_not_called()
                self.assertEqual(app.vars["alice-0-v"].get(), "-170.0")
                self.assertEqual(app.settings[gui.bb84.RANDOM]["alice-v-v"], "35.0")
                self.assertEqual(app.settings[gui.bb84.ORDERED]["alice-r-v"], "-80.0")
                before = app.values()
                with patch.object(root, "clipboard_get", return_value='{"schema":"wrong"}'):
                    app.paste_calibration()
                self.assertEqual(app.values(), before)
                app.build_form()
                self.assertTrue(transfer.context(app.values())["current_voltages_match_import"])
            finally:
                app.close()

    def test_shared_measurement_fields_reach_all_capture_configs(self):
        metadata = {"sample-type": "Bright spot for testing", "bandpass-filter": "650/40 nm",
                    "laser-power": "40 uW before objective", "note": "alignment test"}
        expected = dict(sample_type=metadata["sample-type"], bandpass_filter=metadata["bandpass-filter"],
                        laser_power=metadata["laser-power"])
        tools = {"Pulsed polarization": pol.settings, gui.sweep.TOOL: gui.sweep.settings,
                 gui.g2.TOOL: gui.g2.settings, gui.bb84.ORDERED: lambda v: gui.bb84.settings(v, "ordered")}
        for tool, settings in tools.items():
            values = dict(gui.defaults(tool), **metadata)
            self.assertEqual(settings(values)["measurement"], expected, tool)
        values = dict(gui.defaults("CH1 lifetime"), **metadata)
        values.update(action="Record", **{"sync-edge": "falling", "sync-level-mv": "-60"})
        with patch("ph330_lifetime.PH330"), patch("ph330_lifetime.run", return_value=None) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute("CH1 lifetime", values, threading.Event()), 0)
            self.assertEqual(run.call_args.args[1]["measurement"], expected)
            self.assertEqual(run.call_args.args[1]["notes"], "alignment test")

    def test_invalid_dpi_is_rejected(self):
        for bad in ["0", "1201", "nan", "300.5", "", None, True]:
            with self.subTest(value=bad), self.assertRaises(ValueError):
                figure_options.validated_dpi(bad)
        self.assertEqual(figure_options.validated_dpi("600"), 600)


if __name__ == "__main__":
    unittest.main()
