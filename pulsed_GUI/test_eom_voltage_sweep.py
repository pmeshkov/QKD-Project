"""Offline acquisition, exact CSV contract and notebook grid compatibility."""
import ast
import contextlib
import csv
import ctypes as ct
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

import eom_voltage_sweep as sweep
import gui_app as gui
from test_laser_clock_eom import FakeDAQ
from test_ph330_acquire import FakeAPI
from test_pulsed_polarization import dependencies


class FastStop:
    def __init__(self):
        self.stopped = False
    def is_set(self):
        return self.stopped
    def wait(self, seconds):
        return self.stopped
    def set(self):
        self.stopped = True


class SweepAPI(FakeAPI):
    def __init__(self, stop, fail=None, sync_hz=0):
        super().__init__()
        self.stop, self.fail, self.sync_hz = stop, fail, sync_hz
        self.starts = 0
        self.initialized_mode = None
    def call(self, name, *args):
        if name == "Initialize":
            self.initialized_mode = args[1]
        if name == "StartMeas":
            self.starts += 1
            # 2 CH1, 1 CH2, overflow, SYNC/marker specials: count only photons.
            self.blocks = iter([[1, 2, (1 << 25) | 3, 0xfe000001, 0x80000002]])
            if self.fail == "fifo" and self.starts == 2:
                self.flags = 2
        result = super().call(name, *args)
        if name == "GetAllCountRates":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = self.sync_hz
            args[2][0], args[2][1] = 200, 300
        if name == "GetElapsedMeasTime":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 100
        if name == "ReadFiFo" and self.fail == "stop" and self.starts == 2:
            self.stop.set()
        return result


def values():
    result = gui.defaults(sweep.TOOL)
    result.update(serial="test", **{"manual-ready": "Yes"})
    return result


class SweepTests(unittest.TestCase):
    def test_header_and_grid_match_original_script_and_notebook(self):
        source = Path(__file__).resolve().parents[1] / "EOM_scripts/EOM_V_sweep.py"
        tree = ast.parse(source.read_text())
        headers = [ast.literal_eval(n.args[0]) for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "writerow" and n.args and isinstance(n.args[0], ast.List)]
        self.assertIn(sweep.HEADER, headers)
        cfg = sweep.settings(values())
        actual = np.array(list(sweep.grid(cfg)))
        self.assertEqual(actual.shape, (3600, 2))
        np.testing.assert_array_equal(actual[:, 0], np.tile(np.linspace(-200, 200, 60), 60))
        np.testing.assert_array_equal(actual[:, 1], np.repeat(np.linspace(-200, 200, 60), 60))
        self.assertEqual(cfg["mode"], 2)
        self.assertIsNone(cfg["clock"])
        custom = sweep.settings(dict(values(), points="7"))
        actual = np.array(list(sweep.grid(custom)))
        np.testing.assert_array_equal(actual[:, 0], np.tile(np.linspace(-200, 200, 7), 7))
        np.testing.assert_array_equal(actual[:, 1], np.repeat(np.linspace(-200, 200, 7), 7))
        self.assertAlmostEqual(custom["minimum_scan_seconds"], 49 * .11)

    def run_fake(self, folder, fail=None, source=sweep.CW, sync_hz=0):
        stop = FastStop()
        api, daq = SweepAPI(stop, fail, sync_hz), FakeDAQ(fail_write=fail == "ao")
        constants, system = dependencies()
        cfg = sweep.settings(dict(values(), output=str(folder), source=source, points="2"))
        with contextlib.redirect_stdout(io.StringIO()):
            code = sweep.run(cfg, stop, daq=daq, system=system, constants=constants, api=api)
        meta = json.loads(next(Path(folder).glob("*_sweep.json")).read_text())
        return code, meta, api, daq

    def test_cw_counts_and_cumulative_csv_have_exact_numeric_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            code, meta, api, daq = self.run_fake(temp)
            self.assertEqual(code, 0)
            self.assertTrue(meta["complete"])
            with (Path(temp)/meta["csv"]).open(newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, sweep.HEADER)
                rows = list(reader)
            self.assertEqual(len(rows), 4)
            self.assertEqual([int(r["Raw_Count0"]) for r in rows], [2, 4, 6, 8])
            self.assertEqual([float(r["Rate0_Hz"]) for r in rows], [20.] * 4)
            self.assertEqual([float(r["Rate1_Hz"]) for r in rows], [10.] * 4)
            self.assertEqual(api.initialized_mode, 2)
            self.assertNotIn("SetBinning", api.calls)
            self.assertNotIn("GetResolution", api.calls)
            self.assertFalse(any(e[0] == "clock_route" for e in daq.events))
            self.assertIn(("write", [10., 10.]), daq.events)
            self.assertEqual(daq.events[-2:], [("write", [0., 0.]), ("close", "ao")])
            self.assertEqual(api.calls[-1], "CloseDevice")

    def test_failures_and_stop_keep_partial_rows_and_zero_outputs(self):
        for failure, expected in (("stop", 130), ("fifo", 1), ("ao", 1)):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                code, meta, api, daq = self.run_fake(temp, failure)
                self.assertEqual(code, expected)
                self.assertFalse(meta["complete"])
                self.assertTrue(meta["csv"].endswith(".partial.csv"))
                self.assertFalse(list(Path(temp).glob("*_Detector_Traces.csv")))
                self.assertTrue(meta["ao_zeroed_on_exit"])
                self.assertEqual(meta["rows"], 0 if failure == "ao" else 1)

    def test_internal_pulsed_uses_t3_without_counter_and_external_owns_ctr0(self):
        for source, hz in (("Laser internal 80 MHz", 80_000_000), (sweep.NI, 500_000)):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                code, meta, api, daq = self.run_fake(temp, source=source, sync_hz=hz)
                self.assertEqual(code, 0)
                self.assertEqual(api.initialized_mode, 3)
                routes = [e[1] for e in daq.events if e[0] == "clock_route"]
                self.assertEqual(routes, ["Dev1/ctr0"] if source == sweep.NI else [])

    def test_wrong_sync_prevents_any_measurement_or_nonzero_ao(self):
        for source, hz in ((sweep.CW, 80_000_000), ("Laser internal 20 MHz", 500_000)):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temp:
                code, meta, api, daq = self.run_fake(temp, source=source, sync_hz=hz)
                self.assertEqual(code, 1)
                self.assertEqual(api.starts, 0)
                self.assertFalse(any(e[0] == "write" and e[1] != [0., 0.] for e in daq.events))

    def test_gui_dry_run_and_manual_confirmation_do_not_open_hardware(self):
        with patch.object(sweep, "PH330") as hardware, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute(sweep.TOOL, values(), threading.Event()), 0)
            self.assertEqual(gui.execute(sweep.TOOL, dict(values(), action="Run sweep", **{"manual-ready": "No"}), threading.Event()), 1)
            hardware.assert_not_called()

    def test_validation_rejects_invalid_dwell_and_volts_are_fixed(self):
        for key, val in (("seconds", "0"), ("seconds", "nan"), ("seconds", "0.0009"),
                         ("seconds", "0.1005"), ("settle-ms", "0"), ("output", ""),
                         ("points", "1"), ("points", "1001"), ("points", "2.5"), ("points", "nan")):
            with self.subTest(key=key, val=val), self.assertRaises(ValueError):
                sweep.settings(dict(values(), **{key: val}))


if __name__ == "__main__":
    unittest.main()
