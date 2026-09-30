"""Offline statistical/data-contract checks for the measured-grid notebook."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

import calibration_analysis as cal


def fixture(folder):
    alice, bob = np.array([-150., -50., 50., 150.]), np.array([-60., 40.])
    path = Path(folder)/"example_Detector_Traces.csv"
    rows, points, cumulative = [], [], np.zeros(2, dtype=int)
    # Deliberately reverse scan order; voltage columns must define the grid.
    for bv in bob[::-1]:
        for ai in range(3, -1, -1):
            target = cal.TARGET[ai, 0 if bv == 40 else 1]
            p = .02 + .96*target
            counts = np.array([round(100000*p), round(100000*(1-p))])
            cumulative += counts
            rows.append([ai, bv, alice[ai], *cumulative, *counts])
            points.append(dict(step=len(points), counts=counts.tolist(), seconds=1., flags=0))
    df = pd.DataFrame(rows, columns=["Elapsed_Time_s", "EOM1_Target_V", "EOM0_Target_V",
                                    "Raw_Count0", "Raw_Count1", "Rate0_Hz", "Rate1_Hz"])
    df.to_csv(path, index=False)
    path.with_name("example_points.jsonl").write_text("\n".join(json.dumps(v) for v in points))
    path.with_name("example_sweep.json").write_text(json.dumps(dict(
        schema="qkd-eom-picoharp-sweep-v1", complete=True, status="completed", cleanup_errors=[],
        rows=8, expected_rows=8, settings=dict(mode=3, source="NI external"))))
    return path


class CalibrationTests(unittest.TestCase):
    def test_loader_uses_point_counts_and_voltage_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            grid = cal.load_sweep(fixture(temp))
            np.testing.assert_array_equal(grid.counts.sum(axis=2), np.full((2, 4), 100000))
            np.testing.assert_array_equal(grid.alice, [-150, -50, 50, 150])
            self.assertEqual(grid.counts[1, 0, 0], 2000)
            self.assertEqual(grid.counts[0, 1, 0], 2000)

    def test_corrupt_sidecars_and_incomplete_sweeps_refused(self):
        for failure in ("flags", "duration", "complete", "missing_point"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                path = fixture(temp)
                meta = path.with_name("example_sweep.json")
                points = path.with_name("example_points.jsonl")
                records = [json.loads(v) for v in points.read_text().splitlines()]
                if failure == "flags":
                    records[0]["flags"] = 2
                elif failure == "duration":
                    records[0]["seconds"] = 2
                elif failure == "missing_point":
                    records.pop()
                else:
                    m = json.loads(meta.read_text())
                    m["complete"] = False
                    meta.write_text(json.dumps(m))
                points.write_text("\n".join(json.dumps(v) for v in records))
                with self.assertRaises(ValueError):
                    cal.load_sweep(path)

    def test_joint_selection_matches_all_eight_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            grid = cal.load_sweep(fixture(temp))
            train, heldout = cal.split_counts(grid)
            np.testing.assert_array_equal(train+heldout, grid.counts)
            selected = cal.search(grid, train, [-150,-50,50,150], [40,-60], alice_radius=200, bob_radius=200)[0]
            self.assertTrue(selected["training_point_feasible"])
            self.assertEqual(len(set(selected["alice_indices"])), 4)
            checks = cal.evaluate(grid, selected, heldout)
            self.assertTrue(checks.interval_within_tolerance.all())
            before = cal.search(grid, train, [-150,-50,50,150], [40,-60])
            grid.counts[:] = grid.counts[..., ::-1].copy()  # Selection must never consult held-out/full counts.
            self.assertEqual(before, cal.search(grid, train, [-150,-50,50,150], [40,-60]))

    def test_infeasible_measurements_are_not_called_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            grid = cal.load_sweep(fixture(temp))
            flat = np.full_like(grid.counts, 1000)
            selected = cal.search(grid, flat, [-150,-50,50,150], [40,-60])[0]
            self.assertFalse(selected["training_point_feasible"])
            self.assertFalse(cal.evaluate(grid, selected, flat).interval_within_tolerance.all())
            with self.assertRaisesRegex(ValueError, "No feasible"):
                cal.search(grid, np.zeros_like(flat), [-150,-50,50,150], [40,-60])

    def test_legacy_counters_do_not_become_dwell_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = fixture(temp)
            path.with_name("example_sweep.json").unlink()
            with self.assertRaisesRegex(ValueError, "do NOT difference"):
                cal.load_sweep(path)
            grid = cal.load_sweep(path, legacy_dwell_s=.5)
            self.assertFalse(grid.exact_counts)
            self.assertEqual(grid.counts.sum(), 400000)

    def test_confidence_intervals_and_exact_independent_voltages(self):
        p, lo, hi = cal.wilson(0, 0)
        self.assertTrue(np.isnan(p))
        p, lo, hi = cal.wilson(50, 50)
        self.assertLess(lo, .45)
        self.assertGreater(hi, .55)
        with tempfile.TemporaryDirectory() as temp:
            grid = cal.load_sweep(fixture(temp))
            selected = cal.search(grid, grid.counts, [-150,-50,50,150], [40,-60])[0]
            self.assertEqual(cal.match_candidate(grid, selected), selected)
            selected["alice_v"][0] += .1
            with self.assertRaisesRegex(ValueError, "exactly"):
                cal.match_candidate(grid, selected)


if __name__ == "__main__":
    unittest.main()
