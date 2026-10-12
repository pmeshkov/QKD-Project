"""Synthetic marker acquisition/overflow/origin tests; no hardware access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

import bb84_markers as markers
import bb84_analysis as analysis
import bb84_sequence as sequence
from ph330_preview import decode_t3_events


class MarkerTests(unittest.TestCase):
    def plan(self, mode=markers.COMMISSION):
        plan = dict(device="Dev1", warmup_count=16, trial_count=64, realized_rate_hz=500000.)
        return markers.make_plan(dict(mode=mode, pulse_sync_minus_marker_sync=1,
                                      verification_note="Synthetic independent reference"), plan)

    def test_marker_samples_are_bounded_and_start_low(self):
        plan = self.plan()
        whole = np.concatenate([markers.samples(plan, n, min(n+7, 80)) for n in range(0, 80, 7)])
        self.assertEqual(np.flatnonzero(whole).tolist(), plan["sample_indices"])
        self.assertEqual(whole[0], 0)
        self.assertEqual(whole.dtype, np.uint32)

    def test_multi_bit_markers_and_compressed_overflow_across_chunks(self):
        first = decode_t3_events(np.array([0x820003FF, 0xFE000003], dtype=np.uint32))
        second = decode_t3_events(np.array([0x86000002, 0x02000403], dtype=np.uint32), first["carry"])
        self.assertEqual(first["marker_cycles"].tolist(), [1023])
        self.assertEqual(second["marker_masks"].tolist(), [3])
        self.assertEqual(second["marker_cycles"].tolist(), [3074])
        self.assertEqual(second["cycles"].tolist(), [3075])

    def fixture(self, folder, mode="random", fault=None):
        alice, bob = dict(H=-159., V=37., R=-69., L=151.), dict(HV=45., RL=-53.)
        sequence.build_sequence(folder, mode, 64, 16, alice, bob)
        schedule = self.plan()
        plan = dict(mode=mode, trial_count=64, warmup_count=16, samples_per_channel=80,
                    realized_rate_hz=500000., alice_voltages=alice, bob_voltages=bob, markers=schedule)
        session = dict(schema="qkd-bb84-acquisition-v1", complete=True, status="completed",
                       ni_sequence_complete=True, marker_samples_generated=80, plan=plan)
        (folder/"session.json").write_text(json.dumps(session))
        origin = 1021
        events = []
        positions = schedule["sample_indices"]
        for i,n in enumerate(positions):
            if fault == "missing" and i == 1:
                continue
            cycle = origin-16-1+n + (1 if fault == "slip" and i > 1 else 0)
            events.append((cycle, 0x82000000))
        choices = np.load(folder/"choices.npy")
        target = analysis.expected(("V", "L"))
        for i,(s,b) in enumerate(choices):
            for ch in ([0,0] if target[s,b] == 1 else [1,1] if target[s,b] == 0 else [0,1]):
                events.append((origin+i, (ch << 25) | (800 << 10)))
        words, wrap = [], 0
        for cycle,word in sorted(events):
            next_wrap = cycle//1024
            if next_wrap > wrap:
                words.append(0xFE000000 | (next_wrap-wrap))
                wrap = next_wrap
            words.append(word | (cycle % 1024))
        raw = folder/"tttr"
        raw.mkdir()
        np.asarray(words, dtype="<u4").tofile(raw/"events.t3raw")
        meta = dict(schema="qkd-ph330-raw-t3-v1", record_type="0x00010307", record_format="GenericT3",
                    complete=True, status="completed", records=len(words), flags_seen=0,
                    hardware=dict(resolution_ps=64), markers=schedule)
        (raw/"metadata.json").write_text(json.dumps(meta))
        return origin

    def test_random_run_marker_origin_and_final_guard_exclusion(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            origin = self.fixture(folder)
            with patch.object(analysis, "CHUNK", 3):
                r = analysis.summarize(folder, marker_offset=1, marker_note="Synthetic loopback reference")
            self.assertEqual(r["origin"], origin)
            self.assertEqual(r["mode"], "marker_referenced")
            self.assertEqual(r["accepted_photons"], 126)
            self.assertEqual(r["sync_range"], [origin, origin+63])
            stats = analysis.agreement(r["assigned"], r["target"])
            populated = r["assigned"].sum(axis=-1) > 0
            np.testing.assert_equal(stats["ch1_fraction"][populated], r["target"][populated])
            commission = analysis.summarize(folder, sync_start=0)
            self.assertIsNone(commission["origin"])
            self.assertIsNone(commission["assigned"])
            self.assertEqual(commission["marker_check"]["status"], "checkpoints_passed_offset_unverified")
            for kwargs in (dict(marker_offset=1), dict(marker_offset=1, marker_note="ref", origin=origin+1, origin_note="other")):
                with self.assertRaises(ValueError):
                    analysis.summarize(folder, **kwargs)

    def test_missing_marker_and_single_sync_slip_are_rejected(self):
        for fault in ("missing", "slip"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                self.fixture(folder, fault=fault)
                with self.assertRaisesRegex(ValueError, "Marker"):
                    analysis.summarize(folder, marker_offset=1, marker_note="ref")

    def test_unverified_configuration_never_uses_old_offset(self):
        self.assertIsNone(self.plan()["pulse_sync_minus_marker_sync"])
        plan = self.plan(markers.CALIBRATED)
        measured = np.array(plan["sample_indices"]) + 100
        r = markers.validate_observed(plan, [1]*len(measured), measured, 16)
        self.assertEqual(r["origin"], 117)
        for masks, cycles in (([1], [100]), ([3]*len(measured), measured), ([1]*len(measured), measured[::-1])):
            with self.assertRaises(ValueError):
                markers.validate_observed(plan, masks, cycles, 16)


if __name__ == "__main__":
    unittest.main()
