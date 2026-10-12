"""Export PH330 GenericT3 photons AND measured markers; no hardware access.

Run from VS Code to choose a BB84 run folder (or its tttr subfolder).
Alternatively pass that folder as the only command-line argument.
Outputs beside events.t3raw:
  photons.csv: detector_channel (1-based), sync_index, arrival_time_ns.
  markers.csv: marker_mask, sync_index (no photon-style fine arrival time).
  decoded_events.npz: named arrays for both event types, with int64 SYNC indices.

Marker masks are bit fields: 1=M1, 2=M2, 4=M3, 8=M4; 3 means M1 and M2.
Both event types share the overflow-corrected PicoHarp SYNC counter. These are
NOT Alice/Bob trial indices: assigning trials still needs the calibrated origin.
Like the original converter, this loads the recording into memory. Generated
exports are replaced on rerun; the raw file and acquisition metadata are untouched.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pulsed_GUI"))
from ph330_preview import decode_t3_events


def convert(folder):
    folder = Path(folder).expanduser().resolve()
    if not (folder / "events.t3raw").is_file():
        folder = folder / "tttr"
    metadata = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if metadata.get("record_format") != "GenericT3" or metadata.get("record_type") != "0x00010307":
        raise ValueError("Expected PH330 GenericT3 metadata (record_type 0x00010307).")
    raw = folder / "events.t3raw"
    size = raw.stat().st_size
    if size % 4 or size // 4 != metadata.get("records"):
        raise ValueError("Raw file length disagrees with metadata or contains an incomplete 32-bit record.")
    resolution_ps = float(metadata["hardware"]["resolution_ps"])
    if not np.isfinite(resolution_ps) or resolution_ps <= 0:
        raise ValueError("Invalid saved PicoHarp time resolution.")

    events = decode_t3_events(np.fromfile(raw, dtype="<u4"))
    arrays = {
        "detector_channel": (events["channel"] + 1).astype(np.uint8),
        "photon_sync_index": events["cycles"],
        "arrival_time_ns": events["micro"].astype(np.float64) * resolution_ps / 1000,
        "marker_mask": events["marker_masks"].astype(np.uint8),
        "marker_sync_index": events["marker_cycles"],
    }
    # Structured tables keep SYNC integers exact; column_stack with arrival
    # floats would convert the entire photon table to floating point.
    photons = np.empty(len(events["cycles"]), dtype=[
        ("detector_channel", "u1"), ("sync_index", "<i8"), ("arrival_time_ns", "<f8")])
    photons["detector_channel"] = arrays["detector_channel"]
    photons["sync_index"] = arrays["photon_sync_index"]
    photons["arrival_time_ns"] = arrays["arrival_time_ns"]
    markers = np.empty(len(events["marker_cycles"]), dtype=[("marker_mask", "u1"), ("sync_index", "<i8")])
    markers["marker_mask"] = arrays["marker_mask"]
    markers["sync_index"] = arrays["marker_sync_index"]

    np.savetxt(folder / "photons.csv", photons, delimiter=",",
               header="detector_channel,sync_index,arrival_time_ns", comments="", fmt=["%d", "%d", "%.6f"])
    np.savetxt(folder / "markers.csv", markers, delimiter=",",
               header="marker_mask,sync_index", comments="", fmt=["%d", "%d"])
    np.savez_compressed(folder / "decoded_events.npz", **arrays)

    print(f"Exported {len(photons):,} photons and {len(markers):,} marker records.")
    print(f"Decoded {events['stats']['overflow_records']:,} overflow records into continuous SYNC indices.")
    print(f"Files: {folder / 'photons.csv'}\n       {folder / 'markers.csv'}\n       {folder / 'decoded_events.npz'}")
    print("SYNC indices are PicoHarp cycle counts, not calibrated experimental trial indices.")
    if not metadata.get("complete") or metadata.get("status") != "completed":
        print("Source acquisition is incomplete; this export preserves that partial data, not a validated run.")
    return arrays


def choose_folder():
    import tkinter as tk
    from tkinter import filedialog
    initial = ROOT / "data" / "bb84" / "random"
    runs = sorted(initial.glob("*_random"))
    if runs:
        initial = runs[-1]
    if not initial.is_dir():
        initial = ROOT
    root = tk.Tk()
    root.withdraw()
    try:
        return filedialog.askdirectory(parent=root, initialdir=str(initial),
                                       title="Select the BB84 run folder or its tttr subfolder")
    finally:
        root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", help="Run folder or tttr subfolder; omit to open the folder picker")
    args = parser.parse_args(argv)
    try:
        folder = args.folder or choose_folder()
        if not folder:
            return 0
        convert(folder)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"T3 conversion failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
