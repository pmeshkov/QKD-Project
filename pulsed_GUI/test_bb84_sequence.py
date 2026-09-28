"""Offline sequence tests: no NI or PicoHarp libraries/devices are opened."""
import csv
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

import bb84_sequence as sequence


ALICE = {"H": -152., "V": 50.90, "R": -50.87, "L": 152.69}
BOB = {"HV": 47.52, "RL": -47.44}


class SequenceTests(unittest.TestCase):
    def test_insufficient_disk_leaves_incomplete_manifest_without_arrays(self):
        from types import SimpleNamespace
        with patch.object(sequence.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
            with self.assertRaisesRegex(OSError, "Insufficient disk space"):
                self.build()
        self.assertFalse(json.loads((self.folder / "sequence.json").read_text())["complete"])
        self.assertFalse((self.folder / "ao_waveform.npy").exists())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name) / "sequence"

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, **changes):
        options = dict(folder=self.folder, mode="ordered", trial_count=19, warmup_count=3,
                       alice_voltages=ALICE, bob_voltages=BOB)
        options.update(changes)
        return sequence.build_sequence(**options)

    def test_ordered_pattern_exercises_eight_pairs_and_voltage_conversion(self):
        with patch.object(sequence, "CHUNK_SIZE", 5):
            manifest = self.build()
        choices = np.load(self.folder / "choices.npy")
        waveform = np.load(self.folder / "ao_waveform.npy")
        np.testing.assert_array_equal(choices[:, 0], np.arange(19) % 4)
        np.testing.assert_array_equal(choices[:, 1], (np.arange(19) // 4) % 2)
        self.assertEqual(choices.dtype, np.uint8)
        self.assertEqual(waveform.dtype, np.float64)
        self.assertEqual(waveform.shape, (2, 22))
        np.testing.assert_allclose(waveform[:, :3], np.array([[7.6] * 3, [-2.376] * 3]), rtol=0, atol=1e-14)
        np.testing.assert_array_equal(waveform[0, 3:], np.array(list(ALICE.values()))[choices[:, 0]] / -20)
        np.testing.assert_array_equal(waveform[1, 3:], np.array(list(BOB.values()))[choices[:, 1]] / -20)
        self.assertEqual(manifest["completed_trials"], 19)
        self.assertEqual(manifest["completed_warmup_samples"], 3)
        self.assertEqual(manifest["counts"]["alice"], {"H": 5, "V": 5, "R": 5, "L": 4})
        self.assertTrue(all(v >= 2 for v in manifest["counts"]["alice_bob_pairs"].values()))
        self.assertEqual(manifest["alice_mapping"]["R"]["basis"], "RL")
        self.assertEqual(manifest["alice_mapping"]["L"]["bit"], 1)
        self.assertEqual(manifest["indexing"]["recorded_sync_alignment"], "not established by sequence generation")
        self.assertTrue(sequence.verify_hashes(self.folder))

    def test_zero_warmup_and_reader_chunks_have_correct_boundaries(self):
        self.build(trial_count=9, warmup_count=0)
        chunks = list(sequence.iter_trials(self.folder, chunk_size=4))
        self.assertEqual([len(c["trial_index"]) for c in chunks], [4, 4, 1])
        for chunk in chunks:
            np.testing.assert_array_equal(chunk["ni_sample_index"], chunk["trial_index"])
        self.assertEqual(chunks[1]["alice_state"].tolist(), ["H", "V", "R", "L"])
        self.assertEqual(chunks[1]["alice_basis"].tolist(), ["HV", "HV", "RL", "RL"])
        self.assertEqual(chunks[1]["alice_bit"].tolist(), [0, 1, 0, 1])
        self.assertEqual(chunks[1]["bob_basis"].tolist(), ["RL"] * 4)

    def test_random_uses_independent_entropy_bytes_and_new_chunks(self):
        # Distinct chunks deliberately prevent a repeated-block implementation
        # from passing. Values above bit masks also exercise unbiased masking.
        entropy_chunks = [bytes([252, 0, 1, 3, 254, 2]), bytes([3, 0, 0, 1, 1, 2]), bytes([2, 1])]
        with patch.object(sequence, "CHUNK_SIZE", 3), patch.object(sequence.os, "urandom", side_effect=entropy_chunks) as entropy:
            manifest = self.build(mode="random", trial_count=7, warmup_count=0)
        self.assertEqual([call.args[0] for call in entropy.call_args_list], [6, 6, 2])
        np.testing.assert_array_equal(np.load(self.folder / "choices.npy"),
                                      [[0, 0], [1, 1], [2, 0], [3, 0], [0, 1], [1, 0], [2, 1]])
        self.assertEqual(manifest["randomness"]["source"], "os.urandom")
        self.assertFalse(manifest["randomness"]["seed_saved"])

    def test_preview_limited_and_excludes_warmup_but_retains_sample_index(self):
        self.build(trial_count=2049, warmup_count=2000)
        with (self.folder / "preview.csv").open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1024)
        self.assertEqual(rows[0]["ni_sample_index"], "2000")
        self.assertEqual(rows[0]["trial_index"], "0")
        self.assertEqual(rows[-1]["trial_index"], "1023")
        self.assertEqual(len(sequence.export_preview(self.folder, max_rows=5)), 5)
        self.assertEqual(sequence.export_preview(self.folder, max_rows=0), [])
        self.assertTrue(sequence.verify_hashes(self.folder))

    def test_cancel_before_allocation_writes_incomplete_manifest(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(sequence.SequenceCancelled):
            self.build(stop_event=stop)
        manifest = json.loads((self.folder / "sequence.json").read_text())
        self.assertEqual(manifest["status"], "cancelled")
        self.assertFalse(manifest["complete"])
        self.assertFalse((self.folder / "choices.npy").exists())
        with self.assertRaisesRegex(ValueError, "incomplete"):
            sequence.load_manifest(self.folder)

    def test_cancel_between_chunks_never_makes_partial_waveform_usable(self):
        stop = threading.Event()

        def entropy(size):
            stop.set()
            return bytes(size)

        with patch.object(sequence, "CHUNK_SIZE", 4), patch.object(sequence.os, "urandom", side_effect=entropy):
            with self.assertRaises(sequence.SequenceCancelled):
                self.build(mode="random", trial_count=10, stop_event=stop)
        manifest = json.loads((self.folder / "sequence.json").read_text())
        self.assertEqual(manifest["status"], "cancelled")
        self.assertEqual(manifest["completed_trials"], 4)
        self.assertFalse(manifest["complete"])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            list(sequence.iter_trials(self.folder))

    def test_failure_to_hash_never_marks_sequence_complete(self):
        with patch.object(sequence, "file_sha256", side_effect=OSError("test read failure")):
            with self.assertRaisesRegex(OSError, "test read failure"):
                self.build()
        manifest = json.loads((self.folder / "sequence.json").read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertFalse(manifest["complete"])
        self.assertEqual(manifest["completed_trials"], 19)

    def test_integrity_check_detects_changed_choices(self):
        self.build()
        choices = np.load(self.folder / "choices.npy", mmap_mode="r+")
        choices[0, 0] = 1
        choices.flush()
        del choices
        with self.assertRaisesRegex(ValueError, "integrity verification"):
            sequence.verify_hashes(self.folder)

    def test_refuses_overwrite_of_existing_artifacts(self):
        manifest = self.build()
        with self.assertRaises(FileExistsError):
            self.build(mode="random")
        self.assertEqual(sequence.load_manifest(self.folder), manifest)
        self.assertTrue(sequence.verify_hashes(self.folder))

    def test_validation_rejects_unsafe_or_ambiguous_inputs_before_files_exist(self):
        bad_cases = [dict(mode="repeat"), dict(trial_count=0), dict(trial_count=1.0),
                     dict(trial_count=True), dict(warmup_count=-1), dict(warmup_count=False),
                     dict(alice_voltages={**ALICE, "H": float("nan")}),
                     dict(alice_voltages={**ALICE, "R": 200.01}),
                     dict(alice_voltages={**ALICE, "H": True}),
                     dict(alice_voltages={"H": 0, "V": 0, "D": 0, "A": 0}),
                     dict(bob_voltages={**BOB, "RL": float("inf")}),
                     dict(bob_voltages={"HV": 0})]
        for changes in bad_cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(**changes)
            self.assertFalse(self.folder.exists())

    def test_choices_file_scales_compactly_and_ao_file_contains_only_planned_samples(self):
        self.build(trial_count=8001, warmup_count=5)
        self.assertLess((self.folder / "choices.npy").stat().st_size, 8001 * 2 + 1024)
        self.assertLess((self.folder / "ao_waveform.npy").stat().st_size, 8006 * 16 + 1024)
        arrays = np.load(self.folder / "ao_waveform.npy", mmap_mode="r")
        self.assertEqual(arrays.shape, (2, 8006))
        del arrays


if __name__ == "__main__":
    unittest.main()
