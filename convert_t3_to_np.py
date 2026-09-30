import sys
import json
from pathlib import Path
import numpy as np

sys.path.insert(0, "pulsed_GUI")
from ph330_preview import decode_t3

folder = Path("data/bb84/ordered/YOUR_RUN/tttr")

metadata = json.loads((folder / "metadata.json").read_text())
words = np.fromfile(folder / "events.t3raw", dtype="<u4")

channel, sync_index, microtime, _, _ = decode_t3(words)
arrival_ns = microtime * metadata["hardware"]["resolution_ps"] / 1000

photons = np.column_stack((channel + 1, sync_index, arrival_ns))

np.savetxt(
    folder / "photons.csv",
    photons,
    delimiter=",",
    header="detector_channel,sync_index,arrival_time_ns",
    comments="",
    fmt=["%d", "%d", "%.6f"],
)