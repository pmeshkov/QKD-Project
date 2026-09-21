"""Offline, bounded-memory analysis of static polarization sessions.

All raw records are read, not a preview prefix. This module applies no channel
delay corrections: those were configured in the PicoHarp before recording.
"""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np

from ph330_acquire import BAD_FLAGS
from ph330_preview import decode_t3

STATES = ("H", "V", "R", "L")
BASES = ("HV", "RL")


def check_stop(stop_event):
    if stop_event is not None and stop_event.is_set():
        raise KeyboardInterrupt


def histogram(folder, stop_event=None, chunk_size=1_048_576):
    folder = Path(folder)
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if (meta.get("schema") != "qkd-ph330-raw-t3-v1" or
            meta.get("record_type") != "0x00010307" or
            meta.get("record_format") != "GenericT3"):
        raise ValueError("Expected PH330 GenericT3 raw data and metadata.")
    if not meta.get("complete") or meta.get("status") != "completed" or meta.get("flags_seen", 0) & BAD_FLAGS:
        raise ValueError("Incomplete or invalid raw recording; analysis refused.")
    raw = folder / "events.t3raw"
    size = raw.stat().st_size
    if size % 4 or size // 4 != meta["records"]:
        raise ValueError("Raw file size differs from metadata.")
    resolution = float(meta["hardware"]["resolution_ps"])
    period = float(meta["measured_sync_period_s"]) * 1e9
    seconds = float(meta["elapsed_ms"]) / 1000
    if not all(math.isfinite(x) and x > 0 for x in (resolution, period, seconds)):
        raise ValueError("Invalid resolution, SYNC period or acquisition duration.")
    counts = np.zeros((2, 32768), dtype=np.int64)
    carry = 0
    with raw.open("rb") as stream:
        while True:
            check_stop(stop_event)
            words = np.fromfile(stream, dtype="<u4", count=chunk_size)
            if not len(words):
                break
            channel, _, micro, carry, stats = decode_t3(words, carry)
            if np.any(channel > 1) or stats["marker_records"]:
                raise ValueError("Unexpected input channel or marker in static two-channel run.")
            for ch in (0, 1):
                counts[ch] += np.bincount(micro[channel == ch].astype(np.int64), minlength=32768)
    return counts, np.arange(32769) * resolution / 1000, meta


def column(basis, channel, mapping):
    """Known bases ordered H,V or R,L; unknown bases keep physical CH1,CH2."""
    first = mapping[basis]
    outcome = None if first == "Unassigned" else first if channel == 0 else next(s for s in basis if s != first)
    col = 2 * BASES.index(basis) + (channel if outcome is None else basis.index(outcome))
    label = f"{basis}/CH{channel + 1}" if outcome is None else f"{outcome} (CH{channel + 1})"
    return col, label, outcome


def summarize(folder, gate=None, stop_event=None):
    folder = Path(folder)
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    if session.get("schema") != "qkd-static-polarization-v1":
        raise ValueError("Select a static polarization session folder with session.json.")
    if gate is not None and (len(gate) != 2 or not all(math.isfinite(v) for v in gate) or not 0 <= gate[0] < gate[1]):
        raise ValueError("Gate must have finite, increasing, nonnegative limits.")
    mapping = session["settings"]["mapping"]
    states = list(session["settings"]["alice"])
    if len(states) != 4 or set(states) not in (set(STATES), {"S0", "S1", "S2", "S3"}):
        raise ValueError("Invalid Alice state labels.")
    if any(mapping[b] not in (*b, "Unassigned") for b in BASES):
        raise ValueError("Invalid saved detector mapping.")
    counts = np.full((4, 4), np.nan)
    full_counts = counts.copy()
    rates = counts.copy()
    probabilities = counts.copy()
    labels = [None] * 4
    for basis in BASES:
        for ch in (0, 1):
            col, labels_col, _ = column(basis, ch, mapping)
            labels[col] = labels_col
    histograms = np.zeros((4, 4, 32768), dtype=np.int64)
    available = np.zeros((4, 4), dtype=bool)
    rows, seen, edges = [], set(), None
    for block in session["blocks"]:
        check_stop(stop_event)
        if block["status"] != "completed":
            continue
        state, basis = block["alice"], block["bob"]
        if state not in states or basis not in BASES or (state, basis) in seen:
            raise ValueError("Invalid or duplicate completed setting in session manifest.")
        seen.add((state, basis))
        raw_folder = (folder / block["raw_folder"]).resolve()
        if not raw_folder.is_relative_to(folder.resolve()):
            raise ValueError("Raw folder must be inside this session.")
        hist, current_edges, meta = histogram(raw_folder, stop_event)
        if edges is None:
            edges = current_edges
        elif not np.array_equal(edges, current_edges):
            raise ValueError("T3 resolution changed between settings; cannot combine histograms.")
        period = meta["measured_sync_period_s"] * 1e9
        if gate is not None and gate[1] > min(period * 1.001, edges[-1]):
            raise ValueError("Gate extends beyond the recorded SYNC period or T3 range.")
        # Quantized event times, not fractional-bin interpolation. No second offset.
        mask = np.ones(32768, dtype=bool) if gate is None else (edges[:-1] >= gate[0]) & (edges[:-1] < gate[1])
        if not mask.any():
            raise ValueError("Gate contains no native T3 timestamp bins.")
        seconds = meta["elapsed_ms"] / 1000
        totals = hist[:, mask].sum(axis=1)
        denominator = int(totals.sum())
        row = states.index(state)
        for ch in (0, 1):
            col, _, outcome = column(basis, ch, mapping)
            counts[row, col] = totals[ch]
            full_counts[row, col] = hist[ch].sum()
            rates[row, col] = totals[ch] / seconds
            probabilities[row, col] = totals[ch] / denominator if denominator else np.nan
            histograms[row, col] = hist[ch]
            available[row, col] = True
            rows.append(dict(alice=state, bob_basis=basis, detector=f"CH{ch + 1}", outcome=outcome,
                             alice_target_v=block["target_eom_v"][0], bob_target_v=block["target_eom_v"][1],
                             ungated_counts=int(hist[ch].sum()), selected_counts=int(totals[ch]),
                             exposure_s=seconds, selected_rate_hz=float(rates[row, col]),
                             probability_within_basis=float(probabilities[row, col]) if denominator else None,
                             sync_period_ns=period, raw_folder=block["raw_folder"]))
    if edges is None:
        raise ValueError("No completed settings available. Raw partial recordings remain saved.")
    if session.get("complete") and len(seen) != 8:
        raise ValueError("Session claims completion but does not contain all eight settings.")
    return dict(session=session, counts=counts, ungated_counts=full_counts, rates=rates,
                probabilities=probabilities, labels=labels, histograms=histograms,
                available=available, edges=edges, rows=rows, completed_settings=len(seen), gate=gate, states=states)


def write_matrix(path, matrix, labels, states):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Alice", *labels])
        for state, row in zip(states, matrix):
            writer.writerow([state, *[float(v) if math.isfinite(v) else "" for v in row]])


def plots(result, output, rebin, plot_stop_ns):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg

    complete = result["completed_settings"]
    suffix = f"{complete}/8 settings; session {result['session']['status']}"
    gate = result["gate"]
    gate_text = "Ungated" if gate is None else f"Gate [{gate[0]:g}, {gate[1]:g}) ns"
    fig = Figure(figsize=(13, 9), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(4, 4, sharex=True, sharey=True)
    edges = result["edges"][::rebin]
    period = min(row["sync_period_ns"] for row in result["rows"])
    limit = min(plot_stop_ns, result["edges"][-1]) if plot_stop_ns else min(period, result["edges"][-1])
    for row, state in enumerate(result["states"]):
        for col, label in enumerate(result["labels"]):
            ax = axes[row, col]
            if result["available"][row, col]:
                hist = result["histograms"][row, col].reshape(-1, rebin).sum(axis=1)
                ax.stairs(hist, edges, color="#1b5f98", linewidth=1)
            else:
                ax.text(0.5, 0.5, "Not acquired", transform=ax.transAxes, ha="center")
            if gate:
                ax.axvspan(*gate, color="#e49b22", alpha=0.18)
            if row == 0:
                ax.set_title(label)
            if col == 0:
                ax.set_ylabel(f"Alice {state}\nCounts / {edges[1] - edges[0]:g} ns")
            if row == 3:
                ax.set_xlabel("Arrival after SYNC (ns)")
            ax.set_xlim(0, limit)
            ax.set_ylim(bottom=0)
            ax.grid(alpha=0.15)
    fig.suptitle(f"Static polarization: all recorded arrival histograms\n{suffix}; shading = analysis gate only")
    fig.savefig(output / "arrival_histograms.png", dpi=140)
    fig.clear()

    fig = Figure(figsize=(12, 5.4), layout="constrained")
    FigureCanvasAgg(fig)
    axes = fig.subplots(1, 2)
    for ax, key, title in zip(axes, ("rates", "probabilities"), ("Detection rate (counts/s)", "Detection probability within each Bob basis")):
        matrix = result[key]
        cmap = __import__("matplotlib").colormaps["Blues"].copy()
        cmap.set_bad("#dedede")
        kwargs = dict(vmin=0, vmax=1) if key == "probabilities" else dict(vmin=0, vmax=max(1, float(np.nanmax(matrix))))
        image = ax.imshow(np.ma.masked_invalid(matrix), cmap=cmap, **kwargs)
        ax.set_xticks(range(4), result["labels"], rotation=20, ha="right")
        ax.set_yticks(range(4), result["states"])
        ax.set_xlabel("Bob outcome (two separate basis acquisitions)")
        ax.set_ylabel("Alice state")
        ax.set_title(title, fontsize=11)
        ax.axvline(1.5, color="black", linewidth=1.5)
        for row in range(4):
            for col in range(4):
                v = matrix[row, col]
                text = "N/A" if not math.isfinite(v) else f"{v:.3f}" if key == "probabilities" else f"{v:.1f}"
                ax.text(col, row, text, ha="center", va="center", color="white" if math.isfinite(v) and v > kwargs["vmax"] * 0.55 else "black")
        fig.colorbar(image, ax=ax, shrink=0.75)
    fig.suptitle(f"Static polarization baseline | {gate_text}\n{suffix}; no background or efficiency correction")
    fig.savefig(output / "polarization_matrix.png", dpi=150)
    fig.clear()


def analyze(folder, gate=None, rebin=16, plot_stop_ns=0, stop_event=None):
    """Each analysis gets its own folder, preserving earlier gate comparisons."""
    if type(rebin) is not int or rebin < 1 or 32768 % rebin:
        raise ValueError("Rebin must divide 32768.")
    if not math.isfinite(plot_stop_ns) or plot_stop_ns < 0:
        raise ValueError("Plot end must be finite and nonnegative.")
    result = summarize(folder, gate, stop_event)
    check_stop(stop_event)
    output = Path(folder) / "analysis" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output.mkdir(parents=True)
    report = dict(schema="qkd-static-polarization-analysis-v1", source_session="../../session.json",
                  status="writing", completed_settings=result["completed_settings"],
                  source_session_status=result["session"]["status"], gate_ns=gate, rebin=rebin,
                  plot_stop_ns=plot_stop_ns,
                  probability_definition="CH count / (CH1 + CH2) at fixed Alice and Bob setting; zero denominator is null.",
                  gate_definition="Select quantized T3 event times start <= microtime < stop; no interpolation or second channel offset.",
                  raw_gating=False, detector_efficiency_correction=False, background_subtraction=False,
                  cells=result["rows"])
    path = output / "analysis.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    for key in ("counts", "ungated_counts", "rates", "probabilities"):
        write_matrix(output / f"{key}.csv", result[key], result["labels"], result["states"])
    with (output / "cells.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(result["rows"][0]))
        writer.writeheader()
        writer.writerows(result["rows"])
    np.savez_compressed(output / "arrival_histograms.npz", counts=result["histograms"],
                        edges_ns=result["edges"], available=result["available"],
                        alice_labels=np.asarray(result["states"]), outcome_labels=np.asarray(result["labels"]))
    flat = result["histograms"].reshape(16, 32768).T.astype(float)
    flat[:, ~result["available"].reshape(16)] = np.nan
    np.savetxt(output / "arrival_histograms.csv", np.column_stack([result["edges"][:-1], flat]),
               delimiter=",", fmt=["%.9g"] + ["%.0f"] * 16, comments="",
               header="time_bin_start_ns," + ",".join(f"Alice_{s}_{label}" for s in result["states"] for label in result["labels"]))
    check_stop(stop_event)
    plots(result, output, rebin, plot_stop_ns)
    report["status"] = "completed"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Arrival histograms: {(output / 'arrival_histograms.png').resolve()}", flush=True)
    print(f"Preview image: {(output / 'polarization_matrix.png').resolve()}", flush=True)
    print(f"Run record: {(Path(folder) / 'session.json').resolve()}", flush=True)
    return output
