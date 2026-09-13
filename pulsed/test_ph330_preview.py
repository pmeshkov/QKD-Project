"""Synthetic records only; decoder and all-pairs correlation checks."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from ph330_preview import decode_t3, cross_histogram, preview


def photon(channel, sync, micro=0):
    return (channel << 25) | (micro << 10) | sync


def make_fixture(folder):
    """Independent periodic detections with all peaks, including zero delay."""
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(18)
    words = []
    for cycle in range(20000):
        if cycle and cycle % 1024 == 0:
            words.append(0xFE000001)
        for channel in (0, 1):
            if rng.random() < 0.2:
                delay_ns = 30 + channel*8 + rng.exponential(8)
                words.append(photon(channel, cycle % 1024, round(delay_ns*1000/64)))
    np.asarray(words, dtype="<u4").tofile(folder / "events.t3raw")
    meta = {"schema": "qkd-ph330-raw-t3-v1", "record_type": "0x00010307",
            "status": "completed", "complete": True, "flags_seen": 32,
            "records": len(words), "hardware": {"resolution_ps": 64},
            "measured_sync_period_s": 500e-9, "synthetic_test_data": True,
            "config": {"detectors": [{"channel": 0, "label": "SYNTHETIC CH1"},
                                     {"channel": 1, "label": "SYNTHETIC CH2"}]}}
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return meta


class PreviewTests(unittest.TestCase):
    def test_wraps_markers_and_channels(self):
        words = [photon(0, 1023, 27), 0xFE000002,
                 (1 << 31) | (3 << 25) | 4, photon(1, 5, 99),
                 0xFE000000, photon(0, 0, 31)]
        ch, cycle, micro, carry, stats = decode_t3(words)
        np.testing.assert_array_equal(ch, [0, 1, 0])
        np.testing.assert_array_equal(cycle, [1023, 2053, 3072])
        np.testing.assert_array_equal(micro, [27, 99, 31])
        self.assertEqual(carry, 3072)
        self.assertEqual(stats["marker_records"], 1)
        self.assertEqual(stats["overflow_wraps"], 3)

    def test_overflow_across_chunks(self):
        _, _, _, carry, _ = decode_t3([0xFE000003])
        _, cycle, _, _, _ = decode_t3([photon(1, 12)], carry)
        self.assertEqual(cycle[0], 3084)

    def test_unknown_special_rejected(self):
        with self.assertRaises(ValueError):
            decode_t3([0x80000000])

    def test_all_pairs_matches_brute_force_including_boundaries(self):
        a, b = np.array([40, 0, 20, 20]), np.array([0, 5, 10, 15, 25, 55])
        edges, counts = cross_histogram(a, b, halfwidth_ps=10, bin_ps=2)
        pairs = [int(y-x) for x in a for y in b if -10 <= y-x < 10]
        expected, _ = np.histogram(pairs, edges)
        np.testing.assert_array_equal(counts, expected)

    def test_pair_limit(self):
        with self.assertRaisesRegex(ValueError, "pair limit"):
            cross_histogram([0, 0], [0, 0], max_pairs=3)

    def test_end_to_end_and_truncation_detection(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            meta = make_fixture(folder)
            report = preview(folder)
            self.assertEqual(report["preview_records"], meta["records"])
            self.assertTrue(all(report["preview_photons_by_sdk_channel"].values()))
            self.assertGreater(report["correlation_pairs"], 0)
            self.assertGreater((folder / "preview.png").stat().st_size, 1000)
            with (folder / "events.t3raw").open("ab") as f:
                f.write(b"\x00")
            with self.assertRaisesRegex(ValueError, "byte count"):
                preview(folder)

    def test_incomplete_run_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            meta = make_fixture(folder)
            meta["complete"] = False
            (folder / "metadata.json").write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "not complete"):
                preview(folder)


if __name__ == "__main__":
    unittest.main()
