"""Offline calibration workflow, recent paths, and direct GUI handoff checks."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import eom_calibration as workflow
import eom_calibration_core as core
import gui_app as gui
import recent_data as recent
from calibration_transfer import parse


def fixture(folder, stem="test", total=100000):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    csv = folder / f"{stem}_Detector_Traces.csv"
    rows, points, cumulative = [], [], np.zeros(2, dtype=int)
    for bi, bob in enumerate([40., -60.]):
        for ai, alice in enumerate([-150., -50., 50., 150.]):
            c1 = round(total*(.02+.96*core.TARGET[ai, bi]))
            counts = np.array([c1, total-c1]); cumulative += counts
            rows.append([alice, bob, *cumulative, *counts])
            points.append(dict(step=len(points), counts=counts.tolist(), seconds=1., flags=0))
    pd.DataFrame(rows, columns=["EOM0_Target_V", "EOM1_Target_V", "Raw_Count0", "Raw_Count1", "Rate0_Hz", "Rate1_Hz"]).to_csv(csv, index=False)
    csv.with_name(f"{stem}_points.jsonl").write_text("\n".join(json.dumps(p) for p in points))
    csv.with_name(f"{stem}_sweep.json").write_text(json.dumps(dict(schema="qkd-eom-picoharp-sweep-v1",
        status="completed", complete=True, cleanup_errors=[], rows=8, expected_rows=8,
        settings=dict(source="NI external", mode=3))))
    return csv


class CalibrationWorkflowTests(unittest.TestCase):
    def test_default_analysis_stays_with_run_and_legacy_sources_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            csv = fixture(root/"custom-sweeps"/"run1")
            manifest = csv.with_name("test_sweep.json")
            meta = json.loads(manifest.read_text())
            meta["layout"] = "one-folder-per-sweep-v1"
            manifest.write_text(json.dumps(meta))
            self.assertEqual(workflow.analysis_directory(csv), csv.parent/"analysis")
            with contextlib.redirect_stdout(io.StringIO()), patch("eom_calibration_figures.render"):
                values = dict(gui.defaults(workflow.TOOL), csv=str(csv))
                output = workflow.analyze(workflow.settings(values))
            self.assertEqual(output.parent, csv.parent/"analysis")
            settings = {gui.sweep.TOOL:{"output":str(root/"custom-sweeps")}}
            self.assertEqual(recent.latest("calibration", settings, root), output/"calibration_for_app.json")
            flat = fixture(root/"old-flat", "old")
            self.assertEqual(workflow.analysis_directory(flat), flat.parent/"analysis"/"old")
            self.assertEqual(workflow.analysis_directory(flat, str(root/"override")), root/"override")
            saved = {workflow.TOOL:{"output":str(gui.ROOT.parent/"data/eoms/calibrations")}}
            self.assertEqual(gui.initial_settings(workflow.TOOL,saved)["output"], "")
            saved[workflow.TOOL]["output"] = str(root/"override")
            self.assertEqual(gui.initial_settings(workflow.TOOL,saved)["output"],str(root/"override"))

    def test_workflow_matches_notebook_selection_and_exports(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            path = fixture(temp)
            values = dict(gui.defaults(workflow.TOOL), csv=str(path), output=str(Path(temp)/"analysis"), **{"plot-dpi": "72"})
            cfg = workflow.settings(values)
            grid = core.load_sweep(path)
            train, _ = core.split_counts(grid, cfg["training_fraction"], cfg["seed"])
            expected = core.search(grid, train, cfg["alice_seeds"], cfg["bob_seeds"])[0]
            output = workflow.analyze(cfg)
            report = json.loads((output/"analysis.json").read_text())
            transfer = parse((output/"calibration_for_app.json").read_text())
            self.assertEqual(report["selected"], expected)
            self.assertEqual(transfer["alice_v"], expected["alice_v"])
            self.assertTrue(report["heldout_count_check_passed"])
            self.assertFalse(report["physical_state_labels_confirmed"])
            self.assertEqual(len(list(output.glob("*.png"))), 5)
            self.assertEqual(len(list(output.glob("*.pdf"))), 5)
            self.assertTrue(recent.valid("calibration", output/"calibration_for_app.json"))
            # Same-data independent validation must fail before writing a new result.
            with self.assertRaisesRegex(ValueError, "same CSV"):
                workflow.analyze(dict(cfg, independent_csv=str(path)))

    def test_stop_during_search_never_produces_a_transfer(self):
        with tempfile.TemporaryDirectory() as temp:
            grid = core.load_sweep(fixture(temp))
            train, _ = core.split_counts(grid)
            stop = threading.Event(); stop.set()
            with self.assertRaises(KeyboardInterrupt):
                core.search(grid, train, [-150,-50,50,150], [40,-60], stop_event=stop)
            cfg = workflow.settings(dict(gui.defaults(workflow.TOOL), csv=str(grid.path), output=str(Path(temp)/"out")))
            with self.assertRaises(KeyboardInterrupt):
                workflow.analyze(cfg, stop)
            self.assertFalse((Path(temp)/"out").exists())

    def test_recent_sweep_skips_partial_bad_manifest_and_deleted_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = fixture(root/"custom", "good")
            bad = fixture(root/"custom", "bad")
            meta = bad.with_name("bad_sweep.json")
            m=json.loads(meta.read_text());m["complete"]=False;meta.write_text(json.dumps(m))
            os.utime(bad, (time.time()+10,)*2)
            settings={gui.sweep.TOOL: {"output": str(root/"custom")}, recent.REGISTRY: {"sweep": str(root/"deleted.csv")}}
            self.assertEqual(recent.latest("sweep", settings, root), good)
            options=recent.dialog_options("", "file", "csv", settings, root, workflow.TOOL)
            self.assertEqual(Path(options["initialdir"]), good.parent)
            self.assertEqual(options["initialfile"], good.name)
            other=fixture(root/"selected", "explicit")
            options=recent.dialog_options(str(other), "file", "csv", settings, root, workflow.TOOL)
            self.assertEqual(Path(options["initialdir"]), other.parent)

    def test_recent_folders_are_separated_by_recording_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            meta=folder/"metadata.json"
            meta.write_text(json.dumps(dict(schema="qkd-g2-tttr-v1", status="completed", complete=True)))
            self.assertTrue(recent.valid("g2",folder))
            self.assertFalse(recent.valid("lifetime",folder))
            self.assertFalse(recent.valid("preview",folder))
            meta.write_text(json.dumps(dict(schema="qkd-ph330-raw-t3-v1", status="completed", complete=True,
                                           config={"detectors":[{"channel":0}]})))
            self.assertTrue(recent.valid("lifetime",folder))
            self.assertTrue(recent.valid("preview",folder))
            self.assertFalse(recent.valid("g2",folder))

    def test_sweep_to_analysis_to_measurement_form_without_hardware(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gui, "SETTINGS", Path(temp)/"gui.json"):
            source=fixture(temp)
            root=tk.Tk();root.withdraw()
            app=gui.App(root, gui.sweep.TOOL)
            try:
                app.track_artifact("CSV: ",source)
                app.events.put(("done",0))
                root.after_cancel(app.poll_id)
                app.poll()
                app.open_calibration_analysis()
                self.assertEqual(app.current_tool,workflow.TOOL)
                self.assertEqual(Path(app.vars["csv"].get()),source)
                app.vars["output"].set(str(Path(temp)/"analysis"))
                app.vars["plot-dpi"].set("72")
                with patch.object(app,"open_plots"):
                    app.start()
                    deadline=time.monotonic()+30
                    while app.running and time.monotonic()<deadline:
                        root.update();time.sleep(.01)
                self.assertFalse(app.running)
                self.assertEqual(app.status.get(),"Complete",app.log.get("1.0","end"))
                self.assertIn("calibration",app.settings[recent.REGISTRY])
                app.load_calibration("Pulsed polarization")
                self.assertEqual(app.vars["alice-0-v"].get(),"-150.0")
                self.assertEqual(app.vars["alice-labels"].get(),"Voltage index only")
                self.assertEqual(app.vars["action"].get(),"Check settings")
                app.load_calibration(gui.bb84.RANDOM)
                self.assertEqual(app.vars["alice-h-v"].get(),"-150.0")
                self.assertEqual(app.vars["labels-confirmed"].get(),"No")
                self.assertFalse(app.running)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
