"""Offline GenericT3 diagnostics: count trace, microtimes and raw HBT pairs.

This is a bounded diagnostic preview, not a normalized or fitted g2 result.
Raw acquisition remains intact. No device or DAQ access.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np


def decode_t3(words, carry=0):
    """GenericT3: 1 special, 6 channel, 15 microtime, 10 sync bits.

    Compressed overflows advance the sync count by 1024 * multiplicity.
    Zero multiplicity is the old single-overflow convention. Markers are
    excluded from photons. Layout checked against PicoQuant Read_PTU.py.
    """
    words = np.asarray(words, dtype=np.uint32)
    special = (words >> 31) != 0
    channel = (words >> 25) & 63
    nsync = (words & 1023).astype(np.int64)
    overflow = special & (channel == 63)
    marker = special & (channel >= 1) & (channel <= 15)
    if np.any(special & ~(overflow | marker)):
        raise ValueError("Unexpected GenericT3 special record.")
    if np.any(~special & (channel >= 4)):
        raise ValueError("Unexpected PH330 photon channel; check record format.")
    increments = np.where(overflow, np.maximum(nsync, 1)*1024, 0)
    corrected = np.cumsum(increments, dtype=np.int64) + carry
    photons = ~special
    return (channel[photons], (nsync + corrected)[photons],
            ((words >> 10) & 32767)[photons],
            int(corrected[-1]) if len(words) else carry,
            {"overflow_records": int(overflow.sum()),
             "overflow_wraps": int(increments.sum() // 1024),
             "marker_records": int(marker.sum())})


def cross_histogram(a, b, halfwidth_ps=2_500_000, bin_ps=2000, max_pairs=20_000_000):
    """All A/B pairs in [-halfwidth,+halfwidth), lag = B-A, bounded work."""
    a, b = np.sort(np.asarray(a, dtype=np.int64)), np.sort(np.asarray(b, dtype=np.int64))
    if halfwidth_ps <= 0 or bin_ps <= 0 or (2*halfwidth_ps) % bin_ps:
        raise ValueError("Correlation width must be positive and divisible into whole bins.")
    edges = np.arange(-halfwidth_ps, halfwidth_ps + bin_ps, bin_ps, dtype=np.int64)
    hist = np.zeros(len(edges)-1, dtype=np.int64)
    total = 0
    for start in range(0, len(a), 1000):
        segment = a[start:start+1000]
        lo = np.searchsorted(b, segment-halfwidth_ps, side="left")
        hi = np.searchsorted(b, segment+halfwidth_ps, side="left")
        counts = hi-lo
        n = int(counts.sum())
        total += n
        if total > max_pairs:
            raise ValueError("Preview pair limit exceeded; reduce --max-records.")
        if n:
            owner = np.repeat(np.arange(len(segment)), counts)
            offsets = np.repeat(np.cumsum(counts)-counts, counts)
            indices = np.repeat(lo, counts) + np.arange(n) - offsets
            lag = b[indices] - segment[owner]
            hist += np.bincount((lag+halfwidth_ps)//bin_ps, minlength=len(hist))
    return edges, hist


def preview(folder, max_records=2_000_000):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    folder = Path(folder).resolve()
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("schema") != "qkd-ph330-raw-t3-v1" or meta.get("record_type") != "0x00010307":
        raise ValueError("Unsupported metadata/record format.")
    if not meta.get("complete") or meta.get("status") != "completed":
        raise ValueError("Run is not complete; inspect metadata before using its partial data.")
    if meta.get("flags_seen", 0) & 0xDE:
        raise ValueError("Metadata reports device data-loss/error flags.")
    raw = folder / "events.t3raw"
    size = raw.stat().st_size
    if size % 4 or size//4 != meta["records"]:
        raise ValueError("Raw file byte count does not match metadata; possible truncation.")
    if size == 0 or type(max_records) is not int or not 1 <= max_records <= 10_000_000:
        raise ValueError("Require nonempty data and max_records in 1..10000000.")
    words = np.fromfile(raw, dtype="<u4", count=min(size//4, max_records))
    channel, cycles, micro, _, specials = decode_t3(words)
    dt_ps = float(meta["hardware"]["resolution_ps"])
    period_ps = float(meta["measured_sync_period_s"])*1e12
    if not all(math.isfinite(x) and x >= 1 for x in (dt_ps, period_ps)):
        raise ValueError("Invalid timing units in metadata.")
    # Integer ps prevent float cancellation at long elapsed times. Rounding
    # period affects short-lag estimates by <0.5 ps per laser period.
    times = cycles*round(period_ps) + np.rint(micro*dt_ps).astype(np.int64)
    ids = [d["channel"] for d in meta["config"]["detectors"]]
    if len(ids) != 2 or len(set(ids)) != 2:
        raise ValueError("Need two distinct detector indices in metadata.")
    counts = {str(i): int((channel == i).sum()) for i in ids}
    unexpected = sorted(set(channel.tolist()) - set(ids))
    if unexpected:
        raise ValueError(f"Unexpected enabled detector channels: {unexpected}")
    notes = []
    if not all(counts.values()):
        notes.append("At least one detector has no photons in the preview prefix.")
    out_of_period = int(np.count_nonzero(micro*dt_ps >= period_ps))
    if out_of_period:
        notes.append("Some microtimes reach/exceed the SYNC period; inspect timing settings/data.")
    a, b = [times[channel == i] for i in ids]
    edges, hist = cross_histogram(a, b)
    truncated = len(words) < size//4
    coverage_s = float(times.max()/1e12) if len(times) else 0
    report = {"kind": "diagnostic_preview_not_normalized_g2",
              "full_file_records": size//4, "file_size_matches_metadata": True,
              "preview_records": len(words), "preview_is_prefix": truncated,
              "preview_photons_by_sdk_channel": counts, **specials,
              "preview_last_photon_s": coverage_s,
              "sync_period_ns": period_ps/1000, "resolution_ps": dt_ps,
              "microtimes_at_or_beyond_period": out_of_period,
              "correlation_pairs": int(hist.sum()), "correlation_bin_ns": 2,
              "correlation_lag_definition": "CH2 time minus CH1 time (configured second minus first)",
              "notes": notes}
    fig, axes = plt.subplots(3, 1, figsize=(10, 10), layout="constrained")
    fig.suptitle("PicoHarp recording check\n" + ("First " if truncated else "All ") +
                 f"{len(words):,} records | raw HBT correlation; no g² normalization", fontsize=13)
    duration = max(coverage_s, 1e-9)
    time_edges = np.linspace(0, duration, 101)
    micro_edges = np.linspace(0, max(period_ps, float((micro*dt_ps).max()) if len(micro) else 0)/1000, 501)
    for i, label in zip(ids, [d.get("label", f"SDK channel {d['channel']}") for d in meta["config"]["detectors"]]):
        mask = channel == i
        curve, _ = np.histogram(times[mask]/1e12, time_edges)
        axes[0].stairs(curve/np.diff(time_edges), time_edges, label=f"{label}: {counts[str(i)]:,} photons")
        decay, _ = np.histogram(micro[mask]*dt_ps/1000, micro_edges)
        axes[1].stairs(decay, micro_edges, label=label)
    axes[0].set(xlabel="Time since acquisition origin (s)", ylabel="Detected rate (s⁻¹)")
    axes[0].legend(fontsize=8)
    axes[1].set(xlabel="Detector delay from SYNC (ns)", ylabel="Photon counts")
    axes[1].legend(fontsize=8)
    axes[2].stairs(hist, edges/1000, color="#533cba")
    for lag in range(-4, 5):
        axes[2].axvline(lag*period_ps/1000, color="gray", linewidth=0.6, alpha=0.4)
    axes[2].set(xlabel="CH2 − CH1 delay (ns)", ylabel="Raw pair counts / 2 ns",
                title="Cable delays can shift peaks; no antibunching dip is required for this test")
    for ax in axes:
        ax.grid(alpha=0.15)
    if notes:
        fig.text(0.5, 0.005, " | ".join(notes), ha="center", fontsize=8, color="darkred")
    fig.savefig(folder / "preview.png", dpi=150)
    plt.close(fig)
    np.savetxt(folder / "coincidences_preview.csv",
               np.column_stack(((edges[:-1]+edges[1:])/2000, hist)), delimiter=",",
               header="lag_center_ns,raw_pair_count", comments="", fmt=["%.3f", "%d"])
    (folder / "preview_summary.json").write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    print(f"Preview image: {folder / 'preview.png'}")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path, help="Run folder containing metadata.json")
    parser.add_argument("--max-records", type=int, default=2_000_000)
    args = parser.parse_args(argv)
    try:
        preview(args.folder, args.max_records)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"Preview failed: {exc}")
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch("Preview saved run")
