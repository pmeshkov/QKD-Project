"""Offline checks for CH1 isolation, full-file histograms and tail fitting."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import ph330_acquire as acq
import ph330_lifetime as life
from test_ph330_acquire import FakeAPI, config


def fixture(folder, tau_ns=18):
    rng = np.random.default_rng(53)
    delay = np.concatenate((30+rng.exponential(tau_ns, 100000), rng.uniform(0, 500, 10000)))
    delay = delay[delay < 500]
    micro = np.rint(delay*1000/64).astype(np.uint32)
    words = micro << 10
    # Include CH2 and an overflow to ensure neither is counted as CH1.
    words = np.concatenate((words, np.array([(1 << 25) | (100 << 10), 0xFE000001], dtype=np.uint32)))
    folder.mkdir(parents=True, exist_ok=True)
    words.astype("<u4").tofile(folder / "events.t3raw")
    meta = {"schema": "qkd-ph330-raw-t3-v1", "record_type": "0x00010307",
            "complete": True, "status": "completed", "records": len(words),
            "hardware": {"resolution_ps": 64}, "measured_sync_period_s": 500e-9,
            "synthetic_test_data": True}
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return len(micro)


class LifetimeTests(unittest.TestCase):
    def test_one_channel_validation_keeps_g2_default(self):
        cfg = config()
        cfg["detectors"] = cfg["detectors"][:1]
        acq.validate(cfg, detector_count=1)
        with self.assertRaises(ValueError):
            acq.validate(cfg)

    def test_ch2_disabled_in_hardware_configuration(self):
        api = FakeAPI()
        cfg = config()
        cfg["detectors"] = cfg["detectors"][:1]
        acq.bind_acquisition(api)
        with patch.object(api, "call", wraps=api.call) as calls:
            acq.configure(api, cfg)
            calls.assert_any_call("SetInputChannelEnable", 0, 0, 1)
            calls.assert_any_call("SetInputChannelEnable", 0, 1, 0)

    def test_zero_ch2_rate_does_not_block_ch1(self):
        cfg = config()
        cfg["detectors"] = cfg["detectors"][:1]
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(acq, "configure", return_value={"input_count": 2}), \
                patch.object(acq, "rates", return_value={"sync_hz": 2000000, "input_hz": [100, 0]}), \
                patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            folder = acq.run(FakeAPI([[1]]), cfg, 1, Path(tmp), True)
            self.assertTrue(json.loads((folder / "metadata.json").read_text())["complete"])

    def test_dry_run_does_not_load_dll(self):
        with patch.object(life, "PH330") as api, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(life.main(["--sync-edge", "rising", "--sync-level-mv", "200"]), 0)
            api.assert_not_called()

    def test_missing_sync_and_inverted_threshold_rejected(self):
        for args in (["--acquire"], ["--rates", "--sync-edge", "rising", "--sync-level-mv", "-200"]):
            with patch.object(life, "PH330") as api, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(life.main(args), 1)
                api.assert_not_called()

    def test_known_lifetime_recovered_and_counts_preserved(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            photons = fixture(folder)
            counts, edges, _, info = life.ch1_histogram(folder)
            self.assertEqual(int(counts.sum()), photons)
            self.assertEqual(info["other_channel_photons_ignored"], 1)
            self.assertEqual(info["overflow_records"], 1)
            rebinned, new_edges = life.rebin_histogram(counts, edges, 8)
            self.assertEqual(int(rebinned.sum()), photons)
            report = life.analyze(folder, fit_window=(40, 180))
            self.assertEqual(report["fit"]["status"], "preliminary_tail_fit")
            self.assertAlmostEqual(report["fit"]["tau_ns"], 18, delta=0.8)
            self.assertGreater((folder / "lifetime_ch1.png").stat().st_size, 1000)

    def test_flat_trace_does_not_get_lifetime(self):
        result, curve = life.tail_fit(np.full(500, 100), np.arange(501), 20, 400)
        self.assertEqual(result["status"], "not_identifiable")
        self.assertNotIn("tau_ns", result)
        self.assertIsNone(curve)

    def test_sparse_trace_is_not_fitted(self):
        result, _ = life.tail_fit(np.ones(50), np.arange(51), 0, 50)
        self.assertEqual(result["status"], "not_identifiable")

    def test_incomplete_and_truncated_files_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            fixture(folder)
            with (folder / "events.t3raw").open("ab") as stream:
                stream.write(b"x")
            with self.assertRaisesRegex(ValueError, "file size"):
                life.ch1_histogram(folder)
            meta = json.loads((folder / "metadata.json").read_text())
            meta["complete"] = False
            (folder / "metadata.json").write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                life.ch1_histogram(folder)


if __name__ == "__main__":
    unittest.main()
