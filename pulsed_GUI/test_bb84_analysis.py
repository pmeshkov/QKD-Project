"""Synthetic TTTR/sequence integration tests; never open hardware."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

import bb84_analysis as analysis
import bb84_controls as controls
import bb84_sequence as sequence
import gui_app as gui
import recent_data


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)/"run"

    def fixture(self, mode="ordered"):
        alice = dict(H=-150., V=50., R=-50., L=150.)
        bob = dict(HV=45., RL=-55.)
        sequence.build_sequence(self.folder, mode, 64, 3, alice, bob)
        choices = np.load(self.folder/"choices.npy")
        plan = dict(mode=mode, trial_count=64, warmup_count=3, realized_rate_hz=500000., alice_voltages=alice, bob_voltages=bob)
        session = dict(schema="qkd-bb84-acquisition-v1", complete=True, status="completed", ni_sequence_complete=True, plan=plan)
        (self.folder/"session.json").write_text(json.dumps(session))
        self.origin = 1021
        target = analysis.expected(("V", "L"))
        events = [(self.origin-1, 0, 800)]
        for i,(s,b) in enumerate(choices):
            detectors = [0,0] if target[s,b] == 1 else [1,1] if target[s,b] == 0 else [0,1]
            events.extend((self.origin+i, ch, 800) for ch in detectors)
            events.append((self.origin+i, 0, 100))  # Out-of-gate background.
        events.append((self.origin+64, 1, 800))  # Post-sequence event.
        words, wrap = [], 0
        for cycle,ch,micro in events:
            next_wrap = cycle//1024
            if next_wrap > wrap:
                words.append(0xFE000000 | (next_wrap-wrap))
                wrap = next_wrap
            words.append((ch << 25) | (micro << 10) | (cycle % 1024))
        raw = self.folder/"tttr"
        raw.mkdir()
        np.asarray(words,dtype="<u4").tofile(raw/"events.t3raw")
        meta = dict(schema="qkd-ph330-raw-t3-v1", record_type="0x00010307", record_format="GenericT3", complete=True, status="completed", records=len(words), flags_seen=4,
                    flags_by_phase=dict(startup=4,active=0,tail=0,stopped=0), startup_sync_clear_observed=True, hardware=dict(resolution_ps=64))
        (raw/"metadata.json").write_text(json.dumps(meta))

    def test_ordered_all_phases_and_chunked_overflow_without_origin_claim(self):
        self.fixture()
        with patch.object(analysis,"CHUNK",3):
            r = analysis.summarize(self.folder, sync_start=self.origin, sync_stop=self.origin+64)
        self.assertEqual(r["accepted_photons"],128)
        self.assertEqual(r["range_photons"],192)
        self.assertEqual(r["hypotheses"].shape,(8,4,2,2))
        self.assertIsNone(r["assigned"])
        self.assertEqual(analysis.agreement(r["hypotheses"][self.origin%8],r["target"])["mean_absolute_error"],0)
        self.assertNotIn("best_phase",r)

    def test_supplied_origin_indexes_saved_random_choices_and_excludes_boundaries(self):
        self.fixture("random")
        r = analysis.summarize(self.folder,origin=self.origin,origin_note="Synthetic reference")
        self.assertEqual(r["accepted_photons"],128)
        self.assertEqual(r["assigned"].sum(),128)
        stats = analysis.agreement(r["assigned"],r["target"])
        populated = r["assigned"].sum(axis=-1)>0
        np.testing.assert_equal(stats["ch1_fraction"][populated],r["target"][populated])
        self.assertIsNone(r["hypotheses"])
        r = analysis.summarize(self.folder,sync_start=0)
        self.assertIsNone(r["assigned"])
        self.assertIsNone(r["hypotheses"])

    def test_invalid_origin_range_gate_and_no_counts_are_rejected(self):
        self.fixture()
        for kwargs in (dict(origin=1021),dict(origin=1.5),dict(sync_start=-1),dict(sync_start=4,sync_stop=3),dict(gate=(60,50)),dict(gate=(0,3000)),dict(mapping=("Q","L")),dict(sync_start=10000000)):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                analysis.summarize(self.folder,**kwargs)

    def test_missing_settings_not_reported_as_zero_error_and_wilson_limits(self):
        stats = analysis.agreement(np.zeros((4,2,2),dtype=int),analysis.expected(("V","L")))
        self.assertIsNone(stats["mean_absolute_error"])
        self.assertIsNone(stats["matched_fraction"])
        p,lo,hi = analysis.wilson(np.array([0,10]),np.array([10,10]))
        self.assertGreater(hi[0],0)
        self.assertLess(lo[1],1)

    def test_corruption_bad_flags_and_incomplete_data_are_rejected(self):
        self.fixture()
        path = self.folder/"tttr/metadata.json"
        original = json.loads(path.read_text())
        for change in (dict(flags_seen=2),dict(flags_seen=4,flags_by_phase={}),dict(flags_by_phase=dict(active=4)),dict(complete=False),dict(records=1)):
            path.write_text(json.dumps({**original,**change}))
            with self.subTest(change=change),self.assertRaises(ValueError):
                analysis.summarize(self.folder,sync_start=0)
        path.write_text(json.dumps(original))
        with (self.folder/"choices.npy").open("ab") as f:
            f.write(b"changed")
        with self.assertRaisesRegex(ValueError,"checksum"):
            analysis.summarize(self.folder,origin=self.origin,origin_note="Synthetic")

    def test_exports_preserve_source_and_prior_analyses_and_analysis_is_offline(self):
        self.fixture()
        before = (self.folder/"tttr/events.t3raw").read_bytes()
        values = gui.defaults(controls.ORDERED)
        values.update(action="Analyze saved run",folder=str(self.folder),**{"analysis-start":str(self.origin),"analysis-stop":str(self.origin+64),"plot-dpi":"72","frequency-hz":"invalid unused acquisition setting"})
        with patch("bb84_run.run") as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute(controls.ORDERED,values,threading.Event()),0)
            out = analysis.analyze(self.folder,sync_start=self.origin,sync_stop=self.origin+64,dpi=72)
        run.assert_not_called()
        self.assertEqual(len(list((self.folder/"analysis").iterdir())),2)
        self.assertEqual(before,(self.folder/"tttr/events.t3raw").read_bytes())
        report = json.loads((out/"analysis.json").read_text())
        self.assertEqual(report["status"],"completed")
        self.assertEqual(len(report["phase_hypotheses"]),8)
        self.assertFalse(report["eligible_for_key_processing"])
        for ext in ("png","pdf","svg"):
            self.assertTrue((out/f"bb84_diagnostic.{ext}").is_file())

    def test_renamed_profiles_and_recent_run_discovery(self):
        self.fixture()
        for new,old in controls.LEGACY_NAMES.items():
            p = gui.initial_settings(new,{old:{"seconds":"17","action":"Prepare sequence only"}})
            self.assertEqual(p["seconds"],"17")
            self.assertEqual(p["action"],controls.PREPARE)
        settings = {controls.ORDERED:{"output":self.temp.name}}
        self.assertEqual(recent_data.latest("bb84_ordered",settings,Path(self.temp.name)),self.folder.resolve())
        self.assertFalse(recent_data.valid("bb84_random",self.folder))
        self.assertNotIn("Preview saved run",gui.TOOLS)


if __name__ == "__main__":
    unittest.main()
