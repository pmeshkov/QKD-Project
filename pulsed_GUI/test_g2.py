"""Synthetic full-file correlations and fake-device acquisition; never instruments."""
import contextlib
import ctypes as ct
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

import g2_analysis as analysis
import g2_measurement as g2
import gui_app as gui
from test_ph330_acquire import FakeAPI
from test_laser_clock_eom import FakeDAQ
from test_pulsed_polarization import dependencies
from test_eom_voltage_sweep import FastStop


def options(**changes):
    v = gui.defaults(g2.TOOL)
    v.update(changes)
    return analysis.options(v)


def fixture(folder, mode=3):
    rng = np.random.default_rng(44)
    if mode == 3:
        words = []
        for cycle in range(4000):
            if cycle and cycle % 1024 == 0:
                words.append(0xfe000001)
            for ch in (0, 1):
                if rng.random() < .4:
                    words.append((ch << 25) | (500 << 10) | (cycle % 1024))
        hardware = dict(resolution_ps=64, base_resolution_ps=1)
        duration_ms = 8.
    else:
        duration_ms = 1.
        events = sorted([(int(t), ch) for ch in (0, 1) for t in rng.integers(0, 1_000_000_000, 4000)])
        words, wrap = [], 0
        for t, ch in events:
            newwrap = t // 33554432
            if newwrap > wrap:
                words.append(0xfe000000 | (newwrap-wrap))
                wrap = newwrap
            words.append((ch << 25) | (t % 33554432))
        hardware = dict(resolution_ps=None, base_resolution_ps=1)
    np.array(words, dtype="<u4").tofile(folder / f"events.t{mode}raw")
    meta = dict(schema="qkd-g2-tttr-v1", complete=True, status="completed", mode=mode,
                flags_seen=0, records=len(words), hardware=hardware, elapsed_ms=duration_ms,
                measured_sync_period_s=2e-6, settings={"geometry": "QKD PBS outputs"})
    (folder / "metadata.json").write_text(json.dumps(meta))
    return meta


class CorrelationTests(unittest.TestCase):
    def test_t2_wraps_compression_sync_markers_and_chunks(self):
        ch, t, carry = analysis.decode_t2([1, 0xfe000002, 0x80000000 | 12, 0x82000003, (1 << 25) | 7])
        np.testing.assert_array_equal(ch, [0, 1])
        np.testing.assert_array_equal(t, [1, 2*33554432+7])
        _, t, carry = analysis.decode_t2([0xfe000000, 5], carry)
        self.assertEqual(t[0], 3*33554432+5)
        with self.assertRaises(ValueError):
            analysis.decode_t2([0xe0000001])

    def test_all_pairs_matches_bruteforce_and_boundaries(self):
        a = np.array([0, 4, 4, 10, 30])
        b = np.array([0, 2, 5, 10, 20, 31])
        hist = np.zeros(10, dtype=np.int64)
        pairs = analysis.add_pairs(hist, a, b, 10, 2, 1000, None)
        lag = [y-x for x in a for y in b if -10 <= y-x < 10]
        expected = np.histogram(lag, bins=np.arange(-10, 11, 2))[0]
        np.testing.assert_array_equal(hist, expected)
        self.assertEqual(pairs, len(lag))
        with self.assertRaisesRegex(ValueError, "limit"):
            analysis.add_pairs(hist, a, b, 10, 2, 1, None)

    def test_dense_pair_fallback_is_exact(self):
        a, b = np.zeros(512, dtype=np.int64), np.zeros(600, dtype=np.int64)
        hist = np.zeros(2, dtype=np.int64)
        self.assertEqual(analysis.add_pairs(hist, a, b, 1, 1, 400000, None), 307200)
        np.testing.assert_array_equal(hist, [0, 307200])

    def test_cw_normalization_includes_bin_exposure_and_zero_count_case(self):
        edges = np.array([-10, 0, 10])
        np.testing.assert_allclose(analysis.normalized(np.array([95, 95]), edges, [100, 100], 1000), [95/99.5]*2)
        self.assertTrue(np.all(np.isnan(analysis.normalized([0, 0], edges, [0, 100], 1000))))

    def test_pulsed_area_ratio_and_reference_validation(self):
        edges = np.arange(-12000, 12001, 2, dtype=np.int64)
        hist = np.zeros(len(edges)-1, dtype=np.int64)
        for k in range(-5, 6):
            hist[(k*2000+12000)//2] = 10 if k == 0 else 100
        opts = options(**{"corr-bin-ns": ".002"})
        rows, ratio = analysis.peak_areas(hist, edges, 2000, 1e12, opts)
        self.assertAlmostEqual(ratio, .1, places=7)
        self.assertEqual(sum(r["reference"] for r in rows), 8)
        with self.assertRaisesRegex(ValueError, "half a laser period"):
            analysis.peak_areas(hist, edges, 2000, 1e12, dict(opts, peak_halfwidth_ps=1000))
        _, ratio = analysis.peak_areas(np.zeros_like(hist), edges, 2000, 1e12, opts)
        self.assertIsNone(ratio)

    def test_full_file_t3_chunk_invariance_gate_and_saved_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            fixture(folder)
            with patch.object(analysis, "READ_CHUNK", 7):
                first = analysis.analyze(folder, options())
            second = analysis.analyze(folder, options())
            self.assertEqual(first["pairs"], second["pairs"])
            self.assertEqual(first["photons"], second["photons"])
            self.assertAlmostEqual(first["pulsed_central_to_side_area_ratio"], 1., delta=.12)
            self.assertEqual(first["interpretation"], "PBS-output cross-correlation")
            self.assertEqual(first["half_range_ns"], 12000)
            gate = analysis.analyze(folder, options(gate="Yes", **{"gate-start-ns": "40", "gate-stop-ns": "100"}))
            self.assertEqual(gate["photons"], [0, 0])
            self.assertIsNone(gate["pulsed_central_to_side_area_ratio"])
            self.assertEqual(len(list((folder / "analysis").glob("*/g2.png"))), 3)
            self.assertFalse(list((folder / "analysis").glob("*/decoded_*")))

    def test_full_file_t2_normalization_near_one_and_invalid_metadata(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            meta = fixture(folder, 2)
            report = analysis.analyze(folder, options(**{"corr-bin-ns": "20"}))
            curve = np.loadtxt(next((folder / "analysis").glob("*/correlation.csv")), delimiter=",", skiprows=1)
            self.assertAlmostEqual(float(np.mean(curve[:, 3])), 1, delta=.03)
            self.assertEqual(report["photons"], [4000, 4000])
            with self.assertRaisesRegex(ValueError, "Arrival gating"):
                analysis.analyze(folder, options(gate="Yes"))
            meta["complete"] = False
            (folder / "metadata.json").write_text(json.dumps(meta))
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                analysis.analyze(folder, options())


class API(FakeAPI):
    def __init__(self, stop, mode=3, fail=None):
        super().__init__([[10, (1 << 25) | 20, 0xfe000001]])
        self.stop, self.mode, self.fail = stop, mode, fail
        self.init_mode = None
    def call(self, name, *args):
        if name == "Initialize":
            self.init_mode = args[1]
        result = super().call(name, *args)
        if name == "GetAllCountRates":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 500000 if self.mode == 3 or self.fail == "sync" else 0
            args[2][0], args[2][1] = 100, 200
        if name == "GetSyncPeriod":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 2e-6
        if name == "ReadFiFo" and self.fail == "stop":
            self.stop.set()
        if name == "GetFlags" and self.fail == "flags":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 2
        return result


class AcquisitionTests(unittest.TestCase):
    def run_fake(self, temp, source=g2.NI, eom="No", fail=None):
        v = gui.defaults(g2.TOOL)
        v.update(output=temp, source=source, serial="test", **{"control-eoms": eom, "eom1-v": "100"})
        cfg = g2.settings(v)
        stop, daq = FastStop(), FakeDAQ(fail_write=fail == "ao")
        api = API(stop, cfg["mode"], fail)
        constants, system = dependencies()
        with contextlib.redirect_stdout(io.StringIO()):
            folder = g2.record(cfg, stop, daq=daq, system=system, constants=constants, api=api)
        return folder, json.loads((folder / "metadata.json").read_text()), daq, api

    def test_pulsed_records_both_detectors_stops_clock_and_zeros_optional_ao(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, meta, daq, api = self.run_fake(temp, eom="Yes")
            self.assertTrue(meta["complete"])
            self.assertEqual((folder / "events.t3raw").stat().st_size, 4*meta["records"])
            self.assertEqual(api.init_mode, 3)
            self.assertEqual([e[1] for e in daq.events if e[0] == "clock_route"], ["Dev1/ctr0"])
            self.assertTrue(meta["ao_zeroed_on_exit"])
            self.assertEqual(api.calls[-1], "CloseDevice")

    def test_cw_does_not_access_daq_and_uses_t2(self):
        with tempfile.TemporaryDirectory() as temp:
            folder, meta, daq, api = self.run_fake(temp, source=g2.CW)
            self.assertEqual(daq.events, [])
            self.assertEqual(api.init_mode, 2)
            self.assertNotIn("GetResolution", api.calls)
            self.assertTrue((folder / "events.t2raw").exists())

    def test_interrupted_loss_and_wrong_cw_sync_preserve_failure_state(self):
        for fail, error in (("stop", KeyboardInterrupt), ("flags", RuntimeError), ("sync", ValueError), ("ao", RuntimeError)):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(error):
                    self.run_fake(temp, source=g2.CW if fail == "sync" else g2.NI, eom="Yes", fail=fail)
                meta = json.loads(next(Path(temp).glob("*/metadata.json")).read_text())
                self.assertFalse(meta["complete"])
                self.assertTrue(meta["ao_zeroed_on_exit"])

    def test_offline_actions_and_rate_validation(self):
        v = gui.defaults(g2.TOOL)
        with patch.object(g2, "PH330") as device, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute(g2.TOOL, v, threading.Event()), 0)
            self.assertEqual(gui.execute(g2.TOOL, dict(v, source=g2.CW, action="Record"), threading.Event()), 1)
            device.assert_not_called()
        for hz in (500000, 750000, 1000000):
            self.assertIsNotNone(g2.settings(dict(v, **{"laser-hz": str(hz)}))["clock"])
        for hz in (500000, 2000000, 80000000):
            cfg = g2.settings(dict(v, source=g2.MANUAL, **{"laser-hz": str(hz)}))
            self.assertIsNone(cfg["clock"])
        with self.assertRaises(ValueError):
            g2.settings(dict(v, **{"laser-hz": "80000000"}))


if __name__ == "__main__":
    unittest.main()
