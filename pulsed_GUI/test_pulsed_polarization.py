"""Offline static polarization tests. All hardware interfaces are fakes."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np

import gui_app
import ph330_acquire as acq
import polarization_analysis as analysis
import pulsed_polarization as experiment
from test_laser_clock_eom import FakeDAQ
from test_ph330_acquire import FakeAPI, config as ph_config


def form():
    values = gui_app.defaults("Pulsed polarization")
    values.update({"sync-edge": "falling", "ch1-edge": "rising", "ch2-edge": "rising"})
    return values


def raw_fixture(folder, counts=(3, 1), seconds=1, resolution=1000):
    folder.mkdir(parents=True, exist_ok=True)
    words = [((ch << 25) | ((10 + i) << 10) | 1) for ch in (0, 1) for i in range(counts[ch])]
    # Overflow, then another photon, exercises carry across tiny read chunks.
    words += [(1 << 31) | (63 << 25) | 1]
    np.asarray(words, dtype="<u4").tofile(folder / "events.t3raw")
    meta = dict(schema="qkd-ph330-raw-t3-v1", status="completed", complete=True,
                record_format="GenericT3", record_type="0x00010307", records=len(words),
                flags_seen=0, elapsed_ms=seconds * 1000,
                hardware={"resolution_ps": resolution}, measured_sync_period_s=2e-6)
    (folder / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    return folder


def session_fixture(folder, total=8, dark=False):
    cfg = experiment.settings(form())
    cfg["mapping"] = {"HV": "V", "RL": "Unassigned"}
    session = dict(schema="qkd-static-polarization-v1", status="completed" if total == 8 else "interrupted",
                   complete=total == 8, settings=cfg, blocks=[])
    for i, (state, basis) in enumerate((s, b) for s in cfg["alice"] for b in experiment.BASES):
        if i >= total:
            break
        name = f"raw_{i}"
        raw_fixture(folder / name, counts=(0, 0) if dark else (3, 1), seconds=i + 1)
        session["blocks"].append(dict(alice=state, bob=basis, status="completed", raw_folder=name,
                                      target_eom_v=[cfg["alice"][state], cfg["bob"][basis]]))
    experiment.save_json(folder / "session.json", session)
    return session


class PolarizationTests(unittest.TestCase):
    def test_gui_check_settings_and_analyze_do_not_open_hardware(self):
        with patch.object(experiment, "record") as record, patch.object(experiment, "PH330") as hardware, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui_app.execute("Pulsed polarization", form(), threading.Event()), 0)
            values = gui_app.defaults("Pulsed polarization")
            values.update(action="Analyze saved run", folder="example")
            with patch.object(analysis, "analyze") as analyze:
                self.assertEqual(experiment.main(values), 0)
                analyze.assert_called_once()
            record.assert_not_called()
            hardware.assert_not_called()

    def test_voltage_index_default_and_explicit_labels(self):
        values = form()
        cfg = experiment.settings(values)
        self.assertEqual(list(cfg["alice"]), ["S0", "S1", "S2", "S3"])
        values["alice-labels"] = "Assigned H/V/R/L"
        with self.assertRaises(ValueError):
            experiment.settings(values)
        for i, label in enumerate(("V", "L", "H", "R")):
            values[f"alice-{i}-state"] = label
        cfg = experiment.settings(values)
        self.assertEqual(list(cfg["alice"]), ["H", "V", "R", "L"])
        self.assertEqual(cfg["alice"]["V"], -152)
        self.assertEqual(cfg["alice"]["H"], 50.90)
        values["alice-0-v"] = "201"
        with self.assertRaises(ValueError):
            experiment.settings(values)

    def test_internal_mode_manual_confirmation_and_rates(self):
        values = form()
        values.update(source="Laser internal", action="Record", **{"laser-hz": "80000000"})
        self.assertIsNone(experiment.settings(values)["clock"])
        with patch.object(experiment, "record") as record, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(experiment.main(values), 1)
            record.assert_not_called()
        values["source"] = "NI external"
        with self.assertRaises(ValueError):
            experiment.settings(values)

    def test_offsets_use_sdk_ps_once_and_invalid_values_rejected(self):
        cfg = experiment.settings(form())["ph330"]
        api = FakeAPI()
        with patch.object(api, "call", wraps=api.call) as call:
            info = acq.configure(api, cfg)
            call.assert_any_call("SetInputChannelOffset", 0, 1, 4500)
            call.assert_any_call("SetInputChannelOffset", 0, 0, 0)
            call.assert_any_call("SetSyncChannelOffset", 0, 0)
        self.assertEqual(info["channel_offsets_ps"]["inputs"]["1"], 4500)
        for invalid in (float("nan"), 100000, 4.5):
            cfg["detectors"][1]["offset_ps"] = invalid
            with self.assertRaises(ValueError):
                acq.validate(cfg, expected_laser_hz=None)

    def test_dark_detector_allowed_only_when_explicit(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(acq, "configure", return_value={"input_count": 2}), \
                patch.object(acq, "rates", return_value={"sync_hz": 2e6, "input_hz": [0, 0]}), \
                patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "nonzero"):
                acq.run(FakeAPI(), ph_config(), 1, Path(tmp), True)
            folder = acq.run(FakeAPI([[(1 << 31) | (63 << 25) | 1]]), ph_config(), 1,
                             Path(tmp), True, allow_dark_channels=True)
            self.assertTrue(json.loads((folder / "metadata.json").read_text())["complete"])

    def test_histogram_all_records_chunks_gate_and_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            session_fixture(folder)
            counts, edges, _ = analysis.histogram(folder / "raw_0", chunk_size=1)
            self.assertEqual(counts.sum(axis=1).tolist(), [3, 1])
            self.assertEqual(counts[1, 10], 1)  # No additional CH2 delay shift.
            result = analysis.summarize(folder)
            self.assertEqual(result["counts"][0].tolist(), [1, 3, 3, 1])
            np.testing.assert_allclose(result["probabilities"][0], [.25, .75, .75, .25])
            np.testing.assert_allclose(result["rates"][0], [1, 3, 1.5, .5])
            gated = analysis.summarize(folder, gate=[10, 11])
            self.assertEqual(gated["counts"][0].tolist(), [1, 1, 1, 1])
            self.assertEqual(gated["ungated_counts"][0].tolist(), [1, 3, 3, 1])
            self.assertEqual(gated["histograms"].sum(), result["histograms"].sum())

    def test_partial_and_zero_denominator_are_not_zero_probabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            session_fixture(folder, total=1, dark=True)
            result = analysis.summarize(folder)
            self.assertEqual(result["counts"][0, 0], 0)
            self.assertTrue(np.isnan(result["counts"][0, 2]))
            self.assertTrue(np.isnan(result["probabilities"]).all())
            self.assertTrue(all(row["probability_within_basis"] is None for row in result["rows"]))

    def test_corrupt_or_incomplete_recording_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = raw_fixture(Path(tmp))
            (folder / "events.t3raw").write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "size"):
                analysis.histogram(folder)
            raw_fixture(folder)
            meta = json.loads((folder / "metadata.json").read_text())
            meta["flags_seen"] = 2
            experiment.save_json(folder / "metadata.json", meta)
            with self.assertRaisesRegex(ValueError, "invalid"):
                analysis.histogram(folder)

    def test_analysis_preserves_raw_and_previous_gate_exports(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            folder = Path(tmp)
            session_fixture(folder)
            before = (folder / "raw_0" / "events.t3raw").read_bytes()
            first = analysis.analyze(folder, rebin=16, plot_stop_ns=100)
            second = analysis.analyze(folder, gate=[10, 11], plot_stop_ns=100)
            self.assertNotEqual(first, second)
            for output in (first, second):
                self.assertTrue((output / "polarization_matrix.png").exists())
                self.assertTrue((output / "arrival_histograms.png").exists())
                self.assertTrue((output / "arrival_histograms.csv").exists())
                self.assertEqual(json.loads((output / "analysis.json").read_text())["status"], "completed")
            self.assertEqual(before, (folder / "raw_0" / "events.t3raw").read_bytes())

    def test_order_cleanup_and_continuous_clock(self):
        for internal in (False, True):
            with self.subTest(internal=internal), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()):
                daq = FakeDAQ()
                cfg = experiment.settings(form())
                if internal:
                    cfg.update(source="Laser internal", clock=None)
                constants, system = dependencies()
                starts = []

                def capture(api, ph, seconds, dest, acquire, **kwargs):
                    self.assertTrue(kwargs["allow_dark_channels"])
                    starts.append(len([e for e in daq.events if e[0] == "start"]))
                    self.assertEqual(daq.events[-1][0], "write" if len(starts) > 1 or internal else "start")
                    return raw_fixture(dest / "raw")

                with patch.object(experiment, "pause"):
                    folder = experiment.record(cfg, Path(tmp), threading.Event(), daq=daq,
                                               constants=constants, system=system,
                                               api_factory=lambda _: object(), capture=capture)
                meta = json.loads((folder / "session.json").read_text())
                self.assertTrue(meta["complete"])
                self.assertTrue(meta["ao_zeroed_on_exit"])
                self.assertEqual(starts, [0 if internal else 1] * 8)
                self.assertEqual(daq.events[-2:], [("write", [0, 0]), ("close", "ao")])
                writes = [e[1] for e in daq.events if e[0] == "write"]
                self.assertEqual(writes[0], [7.6, -47.52 / 20])
                if not internal:
                    self.assertLess(daq.events.index(("write", writes[0])), daq.events.index(("start", "clock")))
                else:
                    self.assertFalse(any(e[0] == "clock_route" for e in daq.events))

    def test_capture_interrupt_preserves_manifest_and_cleans_outputs(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            daq = FakeDAQ()
            constants, system = dependencies()
            with patch.object(experiment, "pause"), self.assertRaises(KeyboardInterrupt):
                experiment.record(experiment.settings(form()), Path(tmp), threading.Event(),
                                  daq=daq, constants=constants, system=system,
                                  api_factory=lambda _: object(), capture=lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
            meta = json.loads(next(Path(tmp).glob("*/session.json")).read_text())
            self.assertFalse(meta["complete"])
            self.assertEqual(meta["status"], "interrupted")
            self.assertTrue(meta["ao_zeroed_on_exit"])
            self.assertIn(("stop", "clock"), daq.events)
            self.assertEqual(daq.events[-1], ("close", "ao"))

    def test_counter_conflict_never_applies_targets_or_starts_clock(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            daq = FakeDAQ(fail_reserve=True)
            constants, system = dependencies()
            with self.assertRaisesRegex(RuntimeError, "reserved"):
                experiment.record(experiment.settings(form()), Path(tmp), threading.Event(),
                                  daq=daq, constants=constants, system=system,
                                  api_factory=lambda _: object())
            self.assertEqual([e for e in daq.events if e[0] == "write"], [("write", [0, 0])])
            self.assertFalse(any(e[0] == "start" for e in daq.events))

    def test_gui_analyze_displays_matrix_and_links_session_folder(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(gui_app, "SETTINGS", Path(tmp) / "gui.json"):
            folder = Path(tmp) / "session"
            session_fixture(folder)
            root = tk.Tk()
            root.withdraw()
            app = gui_app.App(root, "Pulsed polarization")
            try:
                app.vars["action"].set("Analyze saved run")
                app.vars["folder"].set(str(folder))
                app.run_button.invoke()
                end = time.monotonic() + 30
                while app.running and time.monotonic() < end:
                    root.update()
                    time.sleep(.01)
                self.assertFalse(app.running)
                self.assertEqual(app.status.get(), "Complete", app.log.get("1.0", "end"))
                self.assertEqual(app.last_plot.name, "polarization_matrix.png")
                self.assertEqual(app.last_folder, folder)
                self.assertTrue(any(isinstance(c, tk.Toplevel) for c in root.winfo_children()))
            finally:
                app.close()


def dependencies():
    return (NS(Level=NS(LOW="LOW"), TaskMode=NS(TASK_VERIFY="verify", TASK_COMMIT="commit"),
               AcquisitionType=NS(CONTINUOUS="continuous")),
            NS(devices={"Dev1": NS(product_type="USB-6351", dev_is_simulated=False, dev_serial_num=1)}))


if __name__ == "__main__":
    unittest.main()
