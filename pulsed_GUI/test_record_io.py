"""Windows record replacement must survive brief locks without hiding failures."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import record_io


class RecordTests(unittest.TestCase):
    def test_brief_lock_retries_but_persistent_lock_preserves_previous_record(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            record_io.save_json(path, {"status": "old"})
            original = Path.replace
            attempts = []

            def locked_once(source, target):
                attempts.append(source)
                if len(attempts) == 1:
                    raise PermissionError("temporary file lock")
                return original(source, target)

            with patch.object(Path, "replace", locked_once), patch.object(record_io.time, "sleep"):
                record_io.save_json(path, {"status": "new"})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(path.read_text())["status"], "new")
            with patch.object(Path, "replace", side_effect=PermissionError("persistent lock")), \
                    patch.object(record_io.time, "sleep"), self.assertRaises(PermissionError):
                record_io.save_json(path, {"status": "unsaved"})
            self.assertEqual(json.loads(path.read_text())["status"], "new")
            self.assertTrue(path.with_suffix(".json.tmp").exists())
