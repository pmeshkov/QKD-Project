"""Atomic JSON records, tolerating short-lived Windows file locks."""
import json
from pathlib import Path
import time


def save_json(path, data):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    # Scanners/indexers can briefly hold the old JSON without delete sharing.
    # Retry only access/locking failures; persistent failures still propagate.
    for delay in (.02, .05, .1, .2, .4, None):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)
