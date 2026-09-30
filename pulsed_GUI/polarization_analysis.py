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


def plots(result, output, rebin, plot_stop_ns, plot_y_max=0, plot_diagonal=True,
          plot_dpi=300, plot_provisional_labels=True, plot_start_ns=0):
    from polarization_figures import render
    render(result, output, rebin, plot_stop_ns, plot_y_max, plot_diagonal, plot_dpi, plot_provisional_labels, plot_start_ns)


def analyze(folder, gate=None, rebin=1, plot_stop_ns=0, stop_event=None, plot_y_max=0, plot_diagonal=True,
            plot_dpi=300, plot_provisional_labels=True, plot_start_ns=0):
    """Each analysis gets its own folder, preserving earlier gate comparisons."""
    from figure_options import validated_dpi
    plot_dpi = validated_dpi(plot_dpi)
    if type(rebin) is not int or rebin < 1 or 32768 % rebin:
        raise ValueError("Rebin must divide 32768.")
    if not math.isfinite(plot_stop_ns) or plot_stop_ns < 0:
        raise ValueError("Plot end must be finite and nonnegative.")
    if not math.isfinite(plot_start_ns) or plot_start_ns < 0 or (plot_stop_ns and plot_start_ns >= plot_stop_ns):
        raise ValueError("Plot start must be finite, nonnegative, and before the plot end.")
    if not math.isfinite(plot_y_max) or plot_y_max < 0:
        raise ValueError("Plot Y-axis maximum must be finite and nonnegative.")
    result = summarize(folder, gate, stop_event)
    from polarization_figures import layout, arrival_limits
    xlimits = arrival_limits(result, plot_start_ns, plot_stop_ns)
    plot_layout = layout(result, plot_diagonal)
    check_stop(stop_event)
    output = Path(folder) / "analysis" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output.mkdir(parents=True)
    report = dict(schema="qkd-static-polarization-analysis-v1", source_session="../../session.json",
                  status="writing", completed_settings=result["completed_settings"],
                  source_session_status=result["session"]["status"], gate_ns=gate, rebin=rebin,
                  native_bin_width_ns=float(result["edges"][1]-result["edges"][0]),
                  plotted_bin_width_ns=float(result["edges"][1]-result["edges"][0])*rebin,
                  plot_start_ns=plot_start_ns, plot_stop_ns=plot_stop_ns, plot_xlim_ns=xlimits,
                  plot_y_max=plot_y_max, plot_layout=plot_layout,
                  plot_dpi=plot_dpi, plot_provisional_labels=plot_provisional_labels,
                  provisional_aliases={"S0": "H", "S1": "R", "S2": "V", "S3": "L"} if plot_provisional_labels else {},
                  figure_formats=["png", "pdf", "svg"], plot_color_meaning="Output channel; not inferred from measured counts.",
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
    plots(result, output, rebin, plot_stop_ns, plot_y_max, plot_diagonal, plot_dpi, plot_provisional_labels, plot_start_ns)
    report["status"] = "completed"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Arrival histograms: {(output / 'arrival_histograms.png').resolve()}", flush=True)
    print(f"Preview image: {(output / 'polarization_matrix.png').resolve()}", flush=True)
    print(f"Run record: {(Path(folder) / 'session.json').resolve()}", flush=True)
    return output
