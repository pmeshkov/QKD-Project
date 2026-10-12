"""Offline CSV/NumPy exports preserve markers and the common SYNC counter."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import convert_t3_to_np as export


class ExportTests(unittest.TestCase):
    def fixture(self, root, words):
        folder = root / "tttr"
        folder.mkdir()
        np.asarray(words, dtype="<u4").tofile(folder / "events.t3raw")
        (folder / "metadata.json").write_text(json.dumps(dict(
            record_format="GenericT3", record_type="0x00010307", records=len(words),
            hardware=dict(resolution_ps=64), complete=True, status="completed")))
        return folder

    def test_photons_and_multibit_markers_share_unwrapped_sync_counter(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(temp)
            folder = self.fixture(root, [0x820003FF, 0xFE000003, 0x86000002, (1 << 25) | (100 << 10) | 3])
            originals = [(folder / name).read_bytes() for name in ("events.t3raw", "metadata.json")]
            arrays = export.convert(root)
            self.assertEqual(arrays["marker_mask"].tolist(), [1, 3])
            self.assertEqual(arrays["marker_sync_index"].tolist(), [1023, 3074])
            self.assertEqual(arrays["photon_sync_index"].tolist(), [3075])
            self.assertEqual(arrays["detector_channel"].tolist(), [2])
            self.assertEqual(arrays["arrival_time_ns"].tolist(), [6.4])
            self.assertEqual((folder / "markers.csv").read_text().splitlines(), ["marker_mask,sync_index", "1,1023", "3,3074"])
            self.assertEqual((folder / "photons.csv").read_text().splitlines(), ["detector_channel,sync_index,arrival_time_ns", "2,3075,6.400000"])
            with np.load(folder / "decoded_events.npz", allow_pickle=False) as saved:
                self.assertEqual(set(saved.files), set(arrays))
                for key in arrays:
                    np.testing.assert_array_equal(saved[key], arrays[key])
                self.assertEqual(saved["marker_sync_index"].dtype, np.dtype("int64"))
            self.assertEqual(originals, [(folder / name).read_bytes() for name in ("events.t3raw", "metadata.json")])

    def test_empty_photon_or_marker_arrays_are_valid(self):
        for words in ([], [5], [0x82000005]):
            with self.subTest(words=words), tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
                folder = self.fixture(Path(temp), words)
                export.convert(folder)
                self.assertTrue((folder / "markers.csv").read_text().startswith("marker_mask,sync_index\n"))
                self.assertTrue((folder / "photons.csv").read_text().startswith("detector_channel,sync_index,arrival_time_ns\n"))

    def test_bad_length_or_wrong_format_rejected_before_export(self):
        for bad_length in (False, True):
            with tempfile.TemporaryDirectory() as temp:
                folder = self.fixture(Path(temp), [5])
                if bad_length:
                    with (folder / "events.t3raw").open("ab") as f:
                        f.write(b"x")
                else:
                    metadata = json.loads((folder / "metadata.json").read_text())
                    metadata["record_format"] = "GenericT2"
                    (folder / "metadata.json").write_text(json.dumps(metadata))
                with self.assertRaises(ValueError):
                    export.convert(folder)
                self.assertFalse((folder / "photons.csv").exists())

    def test_cancelled_picker_does_not_convert(self):
        with patch.object(export, "choose_folder", return_value=""), patch.object(export, "convert") as convert:
            self.assertEqual(export.main([]), 0)
            convert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
