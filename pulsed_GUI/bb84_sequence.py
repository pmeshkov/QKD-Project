"""Prepare saved Alice/Bob choices and voltages without opening any hardware.

The row numbers here describe the *planned* NI output sequence. They do not
establish correspondence with PicoHarp SYNC indices. Acquisition must establish
and validate that correspondence separately before using detections as key data.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import operator
import os
from pathlib import Path
import shutil

import numpy as np
from record_io import save_json


SCHEMA = "qkd-bb84-sequence-v1"
STATES = ("H", "V", "R", "L")
BASES = ("HV", "RL")
HV_GAIN = -20.0
CHUNK_SIZE = 65_536
FILES = ("sequence.json", "choices.npy", "ao_waveform.npy", "preview.csv")
PREVIEW_COLUMNS = (
    "ni_sample_index", "trial_index", "alice_bit", "alice_basis", "alice_state",
    "bob_basis", "alice_hv_v", "bob_hv_v", "ao0_v", "ao1_v",
)


class SequenceCancelled(RuntimeError):
    """Preparation was stopped; sequence.json records an incomplete sequence."""


def _check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise SequenceCancelled("Sequence preparation stopped before acquisition.")


def _integer(value, name, minimum=0):
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{name} must be an integer, not a Boolean.")
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if result < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    return result


def _voltages(mapping, labels, name):
    if not isinstance(mapping, dict) or set(mapping) != set(labels):
        raise ValueError(f"{name} must map exactly {', '.join(labels)} to target EOM volts.")
    values = {}
    for label in labels:
        value = mapping[label]
        if isinstance(value, (bool, np.bool_)):
            raise ValueError(f"{name}[{label}] must be a finite voltage.")
        try:
            value = float(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{name}[{label}] must be a finite voltage.") from exc
        if not math.isfinite(value) or abs(value) > 200:
            raise ValueError(f"{name}[{label}] must be within -200 to +200 target EOM volts.")
        values[label] = value
    return values


def _save_manifest(folder, manifest):
    save_json(folder / "sequence.json", manifest)


def file_sha256(path, stop_event=None):
    """Hash a file using a bounded buffer, with cancellation between reads."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            _check_stop(stop_event)
            block = stream.read(1_048_576)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def build_sequence(folder, mode, trial_count, warmup_count, alice_voltages,
                   bob_voltages, stop_event=None):
    """Save a complete sequence; return its JSON-compatible manifest.

    ``choices.npy`` is uint8 shape ``(trial_count, 2)``: Alice H/V/R/L index,
    then Bob HV/RL index. ``ao_waveform.npy`` is float64 shape
    ``(2, warmup_count + trial_count)`` in AO volts. Warmup holds H/HV.

    Random trials use fresh OS cryptographic randomness for Alice and Bob;
    ordered trials cycle H,V,R,L and use HV for four trials, then RL for four.
    Generation, hashing and preview export use bounded chunks. No existing
    sequence files are overwritten. On failure or cancellation, a partial
    manifest remains and the exception propagates. No hardware is accessed.
    """
    if mode not in ("ordered", "random"):
        raise ValueError("Sequence mode must be 'ordered' or 'random'.")
    trial_count = _integer(trial_count, "trial_count", minimum=1)
    warmup_count = _integer(warmup_count, "warmup_count")
    alice_voltages = _voltages(alice_voltages, STATES, "alice_voltages")
    bob_voltages = _voltages(bob_voltages, BASES, "bob_voltages")
    sample_count = trial_count + warmup_count
    if sample_count > np.iinfo(np.intp).max // 16:
        raise ValueError("Requested sequence exceeds supported array/file sizes.")
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    occupied = [name for name in FILES + ("sequence.json.tmp",) if (folder / name).exists()]
    if occupied:
        raise FileExistsError(f"Sequence destination already contains: {', '.join(occupied)}")
    manifest = {
        "schema": SCHEMA, "status": "preparing", "complete": False,
        "created_utc": datetime.now(timezone.utc).isoformat(), "mode": mode,
        "trial_count": trial_count, "warmup_count": warmup_count,
        "samples_per_channel": sample_count, "completed_trials": 0,
        "completed_warmup_samples": 0, "assumed_hv_gain": HV_GAIN,
        "alice_voltages": alice_voltages, "bob_voltages": bob_voltages,
        "state_order": list(STATES), "basis_order": list(BASES),
        "alice_mapping": {state: {"state_index": index, "bit": index % 2,
                                  "basis": BASES[index // 2], "basis_index": index // 2}
                          for index, state in enumerate(STATES)},
        "bob_mapping": {basis: {"basis_index": index, "target_hv_v": bob_voltages[basis]}
                        for index, basis in enumerate(BASES)},
        "warmup": {"alice_state": "H", "bob_basis": "HV", "first_ni_sample_index": 0,
                   "sample_count": warmup_count, "contains_experimental_trials": False},
        "indexing": {"trial_index_origin": 0, "ni_sample_index_origin": 0,
                     "first_experiment_ni_sample_index": warmup_count,
                     "relationship": "ni_sample_index = warmup_count + trial_index",
                     "recorded_sync_alignment": "not established by sequence generation"},
        "randomness": {
            "source": "os.urandom" if mode == "random" else "none (deterministic test)",
            "method": ("Two independent OS-random bytes per trial; Alice uses byte0 & 3, "
                       "Bob uses byte1 & 1. Power-of-two masking is unbiased."
                       if mode == "random" else
                       "Alice = trial_index % 4; Bob = (trial_index // 4) % 2."),
            "seed_saved": False, "choices_saved_before_hardware_acquisition": True,
        },
        "calibration_provenance": "Caller-supplied target EOM voltages; physical labels are not inferred or verified.",
        "security_note": "This is a preparation record, not evidence of synchronization or a secure key.",
        "files": {
            "choices": {"path": "choices.npy", "dtype": "uint8", "shape": [trial_count, 2],
                        "columns": ["alice_state_index", "bob_basis_index"]},
            "ao_waveform": {"path": "ao_waveform.npy", "dtype": "float64",
                            "shape": [2, sample_count], "channels": ["AO0/Alice", "AO1/Bob"],
                            "units": "DAQ volts", "conversion": "target EOM volts / -20"},
            "preview": {"path": "preview.csv", "scope": "First up to 1024 experimental trials, excluding warmup"},
        },
    }
    _save_manifest(folder, manifest)
    choices = waveform = None
    try:
        _check_stop(stop_event)
        # Allow room for array headers, manifests and raw data before allocating.
        # This is a preflight check, not a reservation; disk-write failures still abort.
        required_bytes = 16 * sample_count + 2 * trial_count + 64 * 1024 * 1024
        if shutil.disk_usage(folder).free < required_bytes:
            raise OSError(f"Insufficient disk space: sequence needs at least {required_bytes:,} free bytes "
                          "including a 64 MiB margin; raw T3 needs additional space.")
        choices = np.lib.format.open_memmap(folder / "choices.npy", mode="w+", dtype=np.uint8,
                                           shape=(trial_count, 2))
        waveform = np.lib.format.open_memmap(folder / "ao_waveform.npy", mode="w+", dtype=np.float64,
                                            shape=(2, sample_count))
        alice_daq = np.asarray([alice_voltages[s] / HV_GAIN for s in STATES])
        bob_daq = np.asarray([bob_voltages[b] / HV_GAIN for b in BASES])
        for start in range(0, warmup_count, CHUNK_SIZE):
            _check_stop(stop_event)
            end = min(start + CHUNK_SIZE, warmup_count)
            waveform[0, start:end] = alice_daq[0]
            waveform[1, start:end] = bob_daq[0]
            manifest["completed_warmup_samples"] = end
        alice_counts = np.zeros(4, dtype=np.int64)
        bob_counts = np.zeros(2, dtype=np.int64)
        pair_counts = np.zeros(8, dtype=np.int64)
        for start in range(0, trial_count, CHUNK_SIZE):
            _check_stop(stop_event)
            end = min(start + CHUNK_SIZE, trial_count)
            if mode == "random":
                # Separate bytes keep the independence of the two choices explicit.
                entropy = np.frombuffer(os.urandom(2 * (end - start)), dtype=np.uint8).reshape(-1, 2)
                alice = entropy[:, 0] & 3
                bob = entropy[:, 1] & 1
            else:
                indices = np.arange(start, end, dtype=np.int64)
                alice = (indices % 4).astype(np.uint8)
                bob = ((indices // 4) % 2).astype(np.uint8)
            choices[start:end, 0] = alice
            choices[start:end, 1] = bob
            waveform[0, warmup_count + start:warmup_count + end] = alice_daq[alice]
            waveform[1, warmup_count + start:warmup_count + end] = bob_daq[bob]
            alice_counts += np.bincount(alice, minlength=4)
            bob_counts += np.bincount(bob, minlength=2)
            pair_counts += np.bincount(alice * 2 + bob, minlength=8)
            manifest["completed_trials"] = end
        choices.flush()
        waveform.flush()
        # Release mappings before hashing/reopening, especially on Windows.
        del choices, waveform
        choices = waveform = None
        manifest["counts"] = {
            "alice": dict(zip(STATES, map(int, alice_counts))),
            "bob": dict(zip(BASES, map(int, bob_counts))),
            "alice_bob_pairs": {f"{s}/{b}": int(pair_counts[2 * i + j])
                                for i, s in enumerate(STATES) for j, b in enumerate(BASES)},
        }
        manifest["status"] = "verifying"
        _save_manifest(folder, manifest)
        _write_preview(folder, manifest, max_rows=1024, stop_event=stop_event)
        for description in manifest["files"].values():
            path = folder / description["path"]
            description["sha256"] = file_sha256(path, stop_event=stop_event)
            description["bytes"] = path.stat().st_size
        _check_stop(stop_event)
        manifest.update(status="complete", complete=True,
                        completed_utc=datetime.now(timezone.utc).isoformat())
        _save_manifest(folder, manifest)
        return manifest
    except BaseException as exc:
        # Partial .npy files may contain unwritten rows; only complete manifests
        # are accepted by public readers or the acquisition path.
        if choices is not None:
            choices.flush()
        if waveform is not None:
            waveform.flush()
        manifest.update(status="cancelled" if isinstance(exc, (SequenceCancelled, KeyboardInterrupt)) else "failed",
                        complete=False, error=f"{type(exc).__name__}: {exc}")
        _save_manifest(folder, manifest)
        raise


def load_manifest(folder):
    """Read a completed sequence manifest; reject partial preparation."""
    manifest = json.loads((Path(folder) / "sequence.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unsupported BB84 sequence schema.")
    if manifest.get("complete") is not True or manifest.get("status") != "complete":
        raise ValueError("Sequence preparation is incomplete; do not use its arrays for acquisition.")
    return manifest


def _iter_trials(folder, manifest, chunk_size, stop_event=None):
    choices = np.load(Path(folder) / "choices.npy", mmap_mode="r", allow_pickle=False)
    trial_count = manifest["trial_count"]
    if choices.shape != (trial_count, 2) or choices.dtype != np.uint8:
        raise ValueError("Saved choices do not match the sequence manifest.")
    alice_hv = np.asarray([manifest["alice_voltages"][s] for s in STATES])
    bob_hv = np.asarray([manifest["bob_voltages"][b] for b in BASES])
    for start in range(0, trial_count, chunk_size):
        _check_stop(stop_event)
        end = min(start + chunk_size, trial_count)
        alice = np.asarray(choices[start:end, 0])
        bob = np.asarray(choices[start:end, 1])
        if np.any(alice >= 4) or np.any(bob >= 2):
            raise ValueError("Saved choices contain an invalid state or basis index.")
        trial = np.arange(start, end, dtype=np.int64)
        yield {
            "ni_sample_index": manifest["warmup_count"] + trial,
            "trial_index": trial, "alice_bit": alice % 2,
            "alice_basis": np.asarray(BASES)[alice // 2],
            "alice_state": np.asarray(STATES)[alice], "bob_basis": np.asarray(BASES)[bob],
            "alice_hv_v": alice_hv[alice], "bob_hv_v": bob_hv[bob],
            "ao0_v": alice_hv[alice] / HV_GAIN, "ao1_v": bob_hv[bob] / HV_GAIN,
        }


def iter_trials(folder, chunk_size=CHUNK_SIZE, stop_event=None):
    """Yield bounded dictionaries of arrays with explicit trial/voltage columns.

    This reads only saved experimental choices, excluding warmup. Consumers can
    join their validated excitation indices to ``trial_index`` without exporting
    tens of millions of CSV lines. Call verify_hashes when checking data integrity.
    """
    chunk_size = _integer(chunk_size, "chunk_size", minimum=1)
    yield from _iter_trials(folder, load_manifest(folder), chunk_size, stop_event)


def _write_preview(folder, manifest, max_rows, stop_event=None):
    remaining = min(max_rows, manifest["trial_count"])
    path = Path(folder) / "preview.csv"
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(PREVIEW_COLUMNS)
        if remaining:
            for chunk in _iter_trials(folder, manifest, min(CHUNK_SIZE, remaining), stop_event):
                length = min(remaining, len(chunk["trial_index"]))
                writer.writerows(zip(*(chunk[name][:length] for name in PREVIEW_COLUMNS)))
                remaining -= length
                if remaining == 0:
                    break
    return path


def export_preview(folder, max_rows=1024, stop_event=None):
    """Read the existing, hash-tracked preview (at most 1024 experiment rows).

    Return CSV row dictionaries. Use iter_trials for larger exports; this helper
    deliberately leaves the immutable preparation files and their hashes intact.
    """
    max_rows = _integer(max_rows, "max_rows", minimum=0)
    if max_rows > 1024:
        raise ValueError("Preview is limited to 1024 rows; use iter_trials for larger exports.")
    load_manifest(folder)
    rows = []
    with (Path(folder) / "preview.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            _check_stop(stop_event)
            if len(rows) == max_rows:
                break
            rows.append(row)
    return rows


def verify_hashes(folder, stop_event=None):
    """Raise on changed/missing artifacts; return True after streamed checks."""
    manifest = load_manifest(folder)
    for info in manifest["files"].values():
        path = Path(folder) / info["path"]
        if path.stat().st_size != info["bytes"] or file_sha256(path, stop_event) != info["sha256"]:
            raise ValueError(f"Sequence artifact failed integrity verification: {info['path']}")
    return True
