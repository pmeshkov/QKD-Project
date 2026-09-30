"""Full-file two-detector correlations with bounded decoding/pair work.

GenericT2 layout follows PicoQuant PTU/C/ptudemo.cc ProcessHHT2(version 2).
T3 decoding reuses our checked GenericT3 decoder. All A/B pairs are counted,
including pairs spanning input chunks. No nearest-neighbour approximation.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from figure_options import validated_dpi
import tempfile
import time

import numpy as np

from ph330_preview import decode_t3
from record_io import save_json

READ_CHUNK = 262144


def stopped(stop):
    if stop is not None and stop.is_set():
        raise KeyboardInterrupt


def decode_t2(words, carry=0):
    words = np.asarray(words, dtype=np.uint32)
    special = (words >> 31) != 0
    channel = (words >> 25) & 63
    tag = (words & 0x1ffffff).astype(np.int64)
    overflow = special & (channel == 63)
    if np.any(special & ~((channel == 63) | (channel <= 15))):
        raise ValueError("Unexpected GenericT2 special record.")
    if np.any(~special & (channel >= 4)):
        raise ValueError("Unexpected detector in GenericT2 record.")
    increments = np.where(overflow, np.maximum(tag, 1) * 33554432, 0)
    extended = np.cumsum(increments, dtype=np.int64) + carry
    mask = ~special  # SYNC is special/channel 0; markers and wraps are not photons.
    return channel[mask], (tag + extended)[mask], int(extended[-1]) if len(words) else carry


def options(values):
    def finite(key, low, high):
        value = float(values[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be in {low:g}..{high:g}.")
        return value
    bin_ps = round(finite("corr-bin-ns", .001, 1e6) * 1000)
    peaks = int(values["reference-last"])
    first = int(values["reference-first"])
    if not 1 <= first <= peaks <= 100:
        raise ValueError("Side-peak indices must satisfy 1 <= first <= last <= 100.")
    gate = None
    if values["gate"] == "Yes":
        gate = [finite("gate-start-ns", 0, 1e6) * 1000, finite("gate-stop-ns", 0, 1e6) * 1000]
        if gate[1] <= gate[0]:
            raise ValueError("Arrival gate end must exceed start.")
    return dict(bin_ps=bin_ps, requested_halfwidth_ps=round(finite("corr-halfwidth-ns", 0, 1e9) * 1000),
                zero_ps=round(finite("corr-zero-ns", -1e9, 1e9) * 1000),
                peak_halfwidth_ps=round(finite("peak-halfwidth-ns", 0, 1e6) * 1000),
                first=first, last=peaks, gate_ps=gate, plot_dpi=validated_dpi(values.get("plot-dpi", "300")),
                max_pairs=int(finite("max-pairs", 1, 1e10)))


def add_pairs(hist, a, b, half, width, max_pairs, stop):
    """Bin all pairs with lag CH2-CH1 in [-half, half); bounded scratch memory."""
    total = 0
    progress = time.monotonic() + 2
    for start in range(0, len(a), 512):
        stopped(stop)
        if time.monotonic() >= progress:
            print(f"Correlating: {start:,}/{len(a):,} CH1 photons; {total:,} pairs.", flush=True)
            progress = time.monotonic() + 2
        segment = a[start:start+512]
        lo = np.searchsorted(b, segment-half, side="left")
        hi = np.searchsorted(b, segment+half, side="left")
        counts = hi-lo
        total += int(counts.sum())
        if total > max_pairs:
            raise ValueError("Pair-work limit exceeded. Narrow the lag range or increase Max pairs; no truncated result is reported.")
        if int(counts.sum()) <= 262144:
            owners = np.repeat(np.arange(len(segment)), counts)
            offsets = np.repeat(np.cumsum(counts)-counts, counts)
            indices = np.repeat(lo, counts) + np.arange(len(owners)) - offsets
            if len(indices):
                hist += np.bincount((b[indices]-segment[owners]+half)//width, minlength=len(hist))
        else:
            for t, left, right in zip(segment, lo, hi):
                for block in range(int(left), int(right), 262144):
                    stopped(stop)
                    lag = b[block:min(block+262144, right)] - t
                    hist += np.bincount((lag+half)//width, minlength=len(hist))
    return total


def normalized(hist, edges, counts, duration_ps):
    """Stationary CW normalization, with exact finite-record overlap per bin."""
    x = edges.astype(float)
    overlap = duration_ps * np.diff(x) - .5 * np.diff(x * np.abs(x))
    expected = counts[0] * counts[1] * overlap / duration_ps**2
    result = np.full(len(hist), np.nan)
    np.divide(hist, expected, out=result, where=expected > 0)
    return result


def peak_areas(hist, edges, period_ps, duration_ps, opts):
    width = int(edges[1]-edges[0])
    half_peak = opts["peak_halfwidth_ps"] or .4 * period_ps
    if not 0 < half_peak < period_ps / 2:
        raise ValueError("Peak half-width must be below half a laser period; 0 selects 0.4 periods.")
    nbin = max(1, round(2 * half_peak / width))
    if nbin * width >= period_ps:
        raise ValueError("Bins are too wide to form separate pulse-peak windows.")
    rows = []
    previous_end = None
    for k in range(-opts["last"], opts["last"]+1):
        center = opts["zero_ps"] + k * period_ps
        left = round((center - nbin * width / 2 - edges[0]) / width)
        right = left + nbin
        if left < 0 or right > len(hist):
            raise ValueError("Lag range does not contain all selected peak windows; enlarge half-range or use 0 (auto).")
        if previous_end is not None and left < previous_end:
            raise ValueError("Quantized peak windows overlap; decrease width or bin size.")
        previous_end = right
        raw = int(hist[left:right].sum())
        middle = (float(edges[left]) + float(edges[right])) / 2
        corrected = raw / (1 - abs(middle) / duration_ps)
        rows.append(dict(peak_index=k, start_ns=float(edges[left])/1000, stop_ns=float(edges[right])/1000,
                         raw_pairs=raw, exposure_corrected_pairs=corrected,
                         reference=opts["first"] <= abs(k) <= opts["last"]))
    reference = np.mean([r["exposure_corrected_pairs"] for r in rows if r["reference"]])
    for row in rows:
        row["relative_area"] = row["exposure_corrected_pairs"] / reference if reference > 0 else None
    return rows, rows[opts["last"]]["relative_area"]


def analyze(folder, opts, stop=None):
    folder = Path(folder).resolve()
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("schema") != "qkd-g2-tttr-v1" or meta.get("mode") not in (2, 3):
        raise ValueError("Select a recording made by Pulsed / CW g2 (metadata.json folder).")
    if not meta.get("complete") or meta.get("status") != "completed" or meta.get("cleanup_errors"):
        raise ValueError("Incomplete/error acquisition cannot be normalized. Raw files are retained for diagnosis.")
    mode = meta["mode"]
    if meta["flags_seen"] & (0xde if mode == 3 else 0xda):
        raise ValueError("Recording reports data loss or device errors.")
    raw = folder / ("events.t3raw" if mode == 3 else "events.t2raw")
    if raw.stat().st_size != 4 * meta["records"]:
        raise ValueError("Raw file length differs from metadata.")
    duration_ps = float(meta["elapsed_ms"]) * 1e9
    if not math.isfinite(duration_ps) or duration_ps <= 0:
        raise ValueError("Invalid recorded duration.")
    period = float(meta["measured_sync_period_s"]) * 1e12 if mode == 3 else None
    unit = float(meta["hardware"]["resolution_ps" if mode == 3 else "base_resolution_ps"])
    if not math.isfinite(unit) or unit < 1 or not unit.is_integer():
        raise ValueError("Expected integer-picosecond timing units.")
    if mode == 3 and (not math.isfinite(period) or period <= 0):
        raise ValueError("Invalid recorded SYNC period.")
    if opts["bin_ps"] < unit:
        raise ValueError(f"Correlation bin must be at least the recorded resolution ({unit/1000:g} ns).")
    gate = opts["gate_ps"]
    if gate and (mode != 3 or not 0 <= gate[0] < gate[1] <= period):
        raise ValueError("Arrival gating requires pulsed T3 and a window within one period.")
    half = opts["requested_halfwidth_ps"] or ((opts["last"]+1) * period + abs(opts["zero_ps"]) if mode == 3 else 2_000_000)
    half = math.ceil(half / opts["bin_ps"]) * opts["bin_ps"]
    nbin = 2 * half // opts["bin_ps"]
    if nbin > 2_000_000 or half >= duration_ps / 10:
        raise ValueError("Use at most two million bins and a half-range below one tenth of recording duration.")
    edges = np.arange(nbin+1, dtype=np.int64) * opts["bin_ps"] - half
    hist = np.zeros(nbin, dtype=np.int64)
    out = folder / "analysis" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    out.mkdir(parents=True)
    report = dict(status="processing", complete=False, acquisition=str(folder), mode=mode,
                  geometry=meta["settings"]["geometry"], options=opts, lag="CH2 minus CH1; configured hardware offsets already applied",
                  acquisition_seconds=duration_ps/1e12, period_ns=period/1000 if period else None,
                  background_subtracted=False, detector_efficiency_corrected=False,
                  interpretation="PBS-output cross-correlation" if meta["settings"]["geometry"] == "QKD PBS outputs" else "HBT cross-correlation",
                  assumptions="Stationary intensities; side peaks must represent an uncorrelated reference. No uncertainty interval estimated.")
    save_json(out / "analysis.json", report)
    maps = []
    try:
        with tempfile.TemporaryDirectory(prefix="decoded_", dir=out) as temp:
            paths = [Path(temp) / f"ch{i}.i8" for i in (0, 1)]
            counts = [0, 0]
            before_gate = [0, 0]
            last_times = [-1, -1]
            trace = np.zeros((2, 100), dtype=np.int64)
            trace_edges = np.linspace(0, duration_ps, 101)
            carry = 0
            processed = 0
            progress = time.monotonic() + 2
            with raw.open("rb") as source, paths[0].open("wb") as first, paths[1].open("wb") as second:
                while True:
                    stopped(stop)
                    words = np.fromfile(source, dtype="<u4", count=READ_CHUNK)
                    if not len(words):
                        break
                    processed += len(words)
                    if time.monotonic() >= progress:
                        print(f"Decoding: {processed:,}/{meta['records']:,} records.", flush=True)
                        progress = time.monotonic() + 2
                    if mode == 3:
                        ch, cycles, micro, carry, _ = decode_t3(words, carry)
                        times = cycles * round(period) + micro.astype(np.int64) * int(unit)
                        delays = micro * unit
                        if np.any(delays >= period):
                            raise ValueError("T3 photon delays exceed the measured period; inspect SYNC/input offsets.")
                        keep = (delays >= gate[0]) & (delays < gate[1]) if gate else np.ones(len(ch), dtype=bool)
                    else:
                        ch, ticks, carry = decode_t2(words, carry)
                        times = ticks * int(unit)
                        keep = np.ones(len(ch), dtype=bool)
                    if np.any(ch > 1):
                        raise ValueError("Recording contains an unexpected enabled detector.")
                    if len(times) and (times.min() < -100000 or times.max() > duration_ps + max(1e9, duration_ps * 1e-4)):
                        raise ValueError("Decoded timestamps do not match the recorded duration.")
                    for i, stream in enumerate((first, second)):
                        before_gate[i] += int(np.count_nonzero(ch == i))
                        t = np.sort(times[(ch == i) & keep])
                        if len(t):
                            if t[0] < last_times[i]:
                                raise ValueError("Detector timestamps are not monotonic across chunks.")
                            last_times[i] = int(t[-1])
                            t.astype("<i8").tofile(stream)
                            counts[i] += len(t)
                            trace[i] += np.histogram(t, trace_edges)[0]
            try:
                for i in (0, 1):
                    maps.append(np.memmap(paths[i], dtype="<i8", mode="r") if counts[i] else np.array([], dtype=np.int64))
                pairs = add_pairs(hist, maps[0], maps[1], half, opts["bin_ps"], opts["max_pairs"], stop)
            finally:
                for mapping in maps:
                    if isinstance(mapping, np.memmap):
                        mapping._mmap.close()
                maps.clear()
        stopped(stop)
        g = normalized(hist, edges, counts, duration_ps) if mode == 2 else None
        peaks, ratio = peak_areas(hist, edges, period, duration_ps, opts) if mode == 3 else (None, None)
        report.update(status="completed", complete=True, photons=counts, photons_before_gate=before_gate,
                      pairs=pairs, half_range_ns=half/1000, pulsed_central_to_side_area_ratio=ratio,
                      normalization="Central area / mean selected symmetric side areas (finite exposure corrected)" if mode == 3 else
                                    "Pairs / [CH1 rate × CH2 rate × bin-integrated acquisition overlap]",
                      t3_period_rounding_ps=round(period)-period if period else None,
                      insufficient_reference=bool(mode == 3 and ratio is None) or any(c == 0 for c in counts))
        np.savetxt(out / "correlation.csv", np.column_stack((edges[:-1]/1000, edges[1:]/1000, hist,
                    g if g is not None else np.full(len(hist), np.nan))), delimiter=",",
                   header="lag_start_ns,lag_stop_ns,pair_count,cw_g2", comments="", fmt=["%.6f", "%.6f", "%d", "%.10g"])
        if peaks:
            import csv
            with (out / "peak_areas.csv").open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(peaks[0]))
                writer.writeheader()
                writer.writerows(peaks)
        np.savetxt(out / "count_trace.csv", np.column_stack((trace_edges[:-1]/1e12, trace_edges[1:]/1e12,
                   trace[0], trace[1])), delimiter=",", header="start_s,stop_s,ch1_count,ch2_count", comments="")
        save_json(out / "analysis.json", report)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        fig = Figure(figsize=(11, 10), constrained_layout=True)
        FigureCanvasAgg(fig)
        axes = fig.subplots(3, 1)
        fig.suptitle(report["interpretation"] + (" — pulsed" if mode == 3 else " — CW"))
        axes[0].stairs(hist, edges/1000)
        axes[0].set(xlabel="CH2 − CH1 lag (ns)", ylabel="Raw pair counts")
        if peaks:
            axes[1].bar([r["peak_index"] for r in peaks], [r["relative_area"] if r["relative_area"] is not None else np.nan for r in peaks])
            axes[1].set(xlabel="Pulse separation", ylabel="Area / mean reference area",
                        title=f"Central/reference area = {ratio:.4g}" if ratio is not None else "Insufficient side-peak coincidences")
        else:
            axes[1].stairs(g, edges/1000)
            axes[1].set(xlabel="CH2 − CH1 lag (ns)", ylabel="Normalized cross-correlation")
        axes[1].axhline(1, color="gray", linestyle="--")
        for i in (0, 1):
            axes[2].stairs(trace[i] / (np.diff(trace_edges)/1e12), trace_edges/1e12, label=f"CH{i+1}")
        axes[2].set(xlabel="Time (s)", ylabel="Detected rate (counts/s)")
        axes[2].legend()
        for axis in axes:
            axis.grid(alpha=.2)
        fig.savefig(out / "g2.png", dpi=opts.get("plot_dpi", 300))
        print(f"Analyzed all {meta['records']:,} records; photons {counts}, pairs {pairs:,}.")
        if mode == 3:
            print(f"Pulsed central/reference peak-area ratio: {ratio}. Reference indices: ±{opts['first']}..±{opts['last']}.")
        print(f"Run record: {out / 'analysis.json'}", flush=True)
        print(f"Preview image: {out / 'g2.png'}", flush=True)
        return report
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", complete=False, error=str(exc))
        save_json(out / "analysis.json", report)
        raise
