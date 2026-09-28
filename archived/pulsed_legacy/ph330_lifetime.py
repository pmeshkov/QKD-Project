"""CH1 lifetime decay acquisition using PicoHarp T3 records.

Uses CH1 (SDK channel 0) and laser SYNC only; other detector channels disabled.
No hardware access unless --rates or --acquire is supplied. --analyze processes
an existing recording. Raw data are preserved. Optional fitting is a preliminary
single-exponential tail fit, without instrument-response reconvolution.
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np

from ph330 import PH330, DEFAULT_DLL
from ph330_acquire import validate, run, BAD_FLAGS
from ph330_preview import decode_t3


OUTPUT_ROOT = Path(__file__).resolve().parent / "runs" / "lifetime_ch1"


def ch1_histogram(folder):
    """Read ALL records in bounded chunks; no 2-million-record preview cap."""
    folder = Path(folder)
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("schema") != "qkd-ph330-raw-t3-v1" or meta.get("record_type") != "0x00010307":
        raise ValueError("Expected a GenericT3 recording from our acquisition code.")
    if not meta.get("complete") or meta.get("status") != "completed":
        raise ValueError("Recording is incomplete; lifetime processing refused.")
    if meta.get("flags_seen", 0) & BAD_FLAGS:
        raise ValueError("Recording reports device data-loss/error flags.")
    raw = folder / "events.t3raw"
    size = raw.stat().st_size
    if size % 4 or size//4 != meta["records"]:
        raise ValueError("Raw file size does not match metadata.")
    resolution = float(meta["hardware"]["resolution_ps"])
    period = float(meta["measured_sync_period_s"])*1e9
    if not all(math.isfinite(x) and x > 0 for x in (resolution, period)):
        raise ValueError("Invalid timing calibration in metadata.")
    counts = np.zeros(32768, dtype=np.int64)
    carry, other, overflows = 0, 0, 0
    with raw.open("rb") as stream:
        while True:
            words = np.fromfile(stream, dtype="<u4", count=1_048_576)
            if not len(words):
                break
            channel, _, micro, carry, stats = decode_t3(words, carry)
            mask = channel == 0
            counts += np.bincount(micro[mask].astype(np.int64), minlength=32768)
            other += int((~mask).sum())
            overflows += stats["overflow_records"]
    edges = np.arange(32769)*resolution/1000  # ps -> ns, fixed detector-delay origin
    return counts, edges, meta, {"ch1_photons": int(counts.sum()),
                                "other_channel_photons_ignored": other,
                                "overflow_records": overflows, "sync_period_ns": period}


def rebin_histogram(counts, edges, factor):
    if type(factor) is not int or factor < 1 or len(counts) % factor:
        raise ValueError("Rebin factor must be a positive divisor of 32768 (e.g. 1, 8, 16).")
    return counts.reshape(-1, factor).sum(axis=1), edges[::factor]


def tail_fit(counts, edges, start_ns, stop_ns):
    """Poisson likelihood fit A*exp(-(t-t0)/tau)+B in a user-chosen tail.

    Includes zero-count bins. The flat-model comparison is a diagnostic
    heuristic, not a statistical confidence interval or proof of fit adequacy.
    """
    from scipy.optimize import minimize
    centers = (edges[:-1]+edges[1:])/2
    mask = (edges[:-1] >= start_ns) & (edges[1:] <= stop_ns)
    x, y = centers[mask], counts[mask].astype(float)
    result = {"model": "A exp(-(t-t0)/tau) + B", "method": "Poisson maximum likelihood",
              "requested_start_ns": start_ns, "requested_stop_ns": stop_ns,
              "irf_reconvolution": False, "status": "not_identifiable"}
    if len(x) < 10 or y.sum() < 100:
        return dict(result, reason="Need at least 10 complete bins and 100 photons in the fit interval."), None
    step, span = float(edges[1]-edges[0]), float(x[-1]-x[0])
    t = x-x[0]
    limit = max(float(y.max())*100, 1)
    bounds = [(math.log(1e-9), math.log(limit)),
              (math.log(step/10), math.log(span*10)),
              (math.log(1e-9), math.log(limit))]

    def model(p):
        amplitude, tau, background = np.exp(p)
        return amplitude*np.exp(-t/tau)+background

    def objective(p):
        prediction = model(p)
        return float(np.sum(prediction-y*np.log(prediction)))

    background0 = max(float(np.percentile(y, 20)), 0.1)
    amplitude0 = max(float(y.max())-background0, 1)
    fits = [minimize(objective, np.log([amplitude0, span*f, background0]),
                     method="L-BFGS-B", bounds=bounds) for f in (0.1, 0.3, 0.8)]
    candidates = [f for f in fits if f.success and np.isfinite(f.fun)]
    if not candidates:
        return dict(result, reason="Optimizer did not converge."), None
    best = min(candidates, key=lambda f: f.fun)
    amplitude, tau, background = map(float, np.exp(best.x))
    flat_nll = float(np.sum(y.mean()-y*np.log(y.mean())))
    gain = max(0.0, 2*(flat_nll-best.fun))
    result["twice_log_likelihood_gain_vs_flat"] = gain
    if gain < 9 or tau <= step/5 or tau >= span*5:
        return dict(result, reason="No well-resolved decreasing tail in this window; no lifetime reported."), None
    prediction = model(best.x)
    logterm = np.zeros_like(y)
    nonzero = y > 0
    logterm[nonzero] = y[nonzero]*np.log(y[nonzero]/prediction[nonzero])
    deviance = float(2*np.sum(prediction-y+logterm))
    result.update(status="preliminary_tail_fit", tau_ns=tau, amplitude_counts_per_bin=amplitude,
                  background_counts_per_bin=background, t0_ns=float(x[0]),
                  fitted_bins=len(x), fitted_photons=int(y.sum()),
                  poisson_deviance=deviance, degrees_of_freedom=len(x)-3,
                  caution="Inspect residuals, fit-window sensitivity and IRF before interpreting tau.")
    return result, (x, prediction, (y-prediction)/np.sqrt(prediction))


def analyze(folder, rebin=8, fit_window=None, show=False):
    import matplotlib
    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    folder = Path(folder).resolve()
    counts, edges, meta, summary = ch1_histogram(folder)
    plot_counts, plot_edges = rebin_histogram(counts, edges, rebin)
    centers = (plot_edges[:-1]+plot_edges[1:])/2
    period = summary["sync_period_ns"]
    summary.update(channel="front-panel CH1 / SDK index 0", processed_records=meta["records"],
                   native_bin_width_ns=float(edges[1]-edges[0]),
                   plot_bin_width_ns=float(plot_edges[1]-plot_edges[0]),
                   status="decay_histogram_only", fit=None)
    summary["photons_with_microtime_at_or_beyond_period"] = int(counts[edges[:-1] >= period].sum())
    curve = None
    if fit_window is not None:
        start, stop = fit_window
        if not all(math.isfinite(v) for v in fit_window) or not 0 <= start < stop <= period:
            raise ValueError("Fit window must satisfy 0 <= start < stop <= measured laser period (ns).")
        summary["fit"], curve = tail_fit(plot_counts, plot_edges, start, stop)
        summary["status"] = summary["fit"]["status"]
    if summary["ch1_photons"] == 0:
        summary["status"] = "no_ch1_photons"

    np.savetxt(folder / "lifetime_ch1.csv", np.column_stack((edges[:-1], edges[1:], counts)),
               delimiter=",", header="delay_left_ns,delay_right_ns,ch1_counts", comments="",
               fmt=["%.9f", "%.9f", "%d"])
    (folder / "lifetime_ch1.json").write_text(json.dumps(summary, indent=2)+"\n", encoding="utf-8")
    nplots = 3 if curve is not None else 2
    fig, axes = plt.subplots(nplots, 1, figsize=(10, 8 if nplots == 2 else 10),
                             sharex=True, layout="constrained")
    title = f"CH1 fluorescence decay | {summary['ch1_photons']:,} photons"
    if meta.get("synthetic_test_data"):
        title = "SYNTHETIC TEST — " + title
    if curve is not None:
        title += f"\nPreliminary tail-fit τ = {summary['fit']['tau_ns']:.3g} ns (no IRF correction)"
    elif fit_window:
        title += "\nNo resolved lifetime from the selected interval"
    else:
        title += "\nChoose a decay-tail interval to estimate lifetime"
    fig.suptitle(title)
    for ax in axes[:2]:
        ax.stairs(plot_counts, plot_edges, color="#2865ad", label="CH1 recorded counts")
        ax.set_ylabel(f"Counts / {summary['plot_bin_width_ns']:.3g} ns")
        if fit_window:
            ax.axvspan(*fit_window, color="orange", alpha=0.12, label="Fit interval")
        if curve is not None:
            ax.plot(curve[0], curve[1], color="#d5651d", label="Exponential + background")
        ax.legend(fontsize=8)
    axes[1].set_yscale("log")
    axes[1].set_ylim(bottom=0.5, top=max(2, float(plot_counts.max())*2))
    if curve is not None:
        axes[2].plot(curve[0], curve[2], linewidth=0.8)
        axes[2].axhline(0, color="gray", linewidth=0.8)
        axes[2].set_ylabel("(Counts − fit) / √fit")
    limit = period
    if summary["photons_with_microtime_at_or_beyond_period"]:
        limit = max(period, float(edges[np.flatnonzero(counts)[-1]+1]))
    axes[-1].set(xlabel="Photon delay from SYNC (ns; includes cable/instrument delay)", xlim=(0, limit))
    for ax in axes:
        ax.grid(alpha=0.2)
    fig.savefig(folder / "lifetime_ch1.png", dpi=150)
    print(json.dumps(summary, indent=2))
    print(f"Lifetime plot: {folder / 'lifetime_ch1.png'}")
    if show:
        plt.show()
    plt.close(fig)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--rates", action="store_true")
    action.add_argument("--acquire", action="store_true")
    action.add_argument("--analyze", type=Path, metavar="RUN_FOLDER")
    parser.add_argument("--sync-edge", choices=["rising", "falling"])
    parser.add_argument("--sync-level-mv", type=int)
    parser.add_argument("--ch1-level-mv", type=int, default=300)
    parser.add_argument("--laser-hz", type=float, default=2_000_000)
    parser.add_argument("--binning", type=int, default=6, help="T3 binning code; 6 gives 64 ps on this device")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial", default="1050578")
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--output", type=Path, default=OUTPUT_ROOT)
    parser.add_argument("--rebin", type=int, default=8, help="Combine this many native bins for plotting/fitting")
    parser.add_argument("--fit-window", type=float, nargs=2, metavar=("START_NS", "STOP_NS"))
    parser.add_argument("--show", action="store_true", help="Open the saved plot after recording/analysis")
    args = parser.parse_args(argv)
    folder = None
    try:
        if args.rebin < 1 or 32768 % args.rebin:
            raise ValueError("--rebin must be a positive divisor of 32768.")
        if args.fit_window and (not all(math.isfinite(v) for v in args.fit_window) or
                                not 0 <= args.fit_window[0] < args.fit_window[1]):
            raise ValueError("Provide a finite increasing --fit-window in ns.")
        if args.analyze:
            analyze(args.analyze, args.rebin, args.fit_window, args.show)
            return 0
        if args.sync_edge is None or args.sync_level_mv is None:
            raise ValueError("Specify --sync-edge and --sync-level-mv for the actual attenuated SYNC pulse.")
        if (args.sync_edge == "rising" and args.sync_level_mv <= 0) or (
                args.sync_edge == "falling" and args.sync_level_mv >= 0):
            raise ValueError("Use positive threshold/rising for positive pulses; negative/falling for negative pulses.")
        if args.ch1_level_mv <= 0:
            raise ValueError("CH1 profile expects positive SPCM pulses and a positive threshold.")
        if not math.isfinite(args.seconds) or not 0.1 <= args.seconds <= 360000:
            raise ValueError("Duration must be 0.1 s through 100 hours.")
        cfg = {"device_index": args.device_index, "serial": args.serial,
               "laser_hz": args.laser_hz, "sync_divider": 1, "binning": args.binning,
               "sync": {"mode": "edge", "edge": args.sync_edge, "level_mv": args.sync_level_mv},
               "detectors": [{"channel": 0, "label": "CH1 lifetime detector", "mode": "edge",
                              "edge": "rising", "level_mv": args.ch1_level_mv}],
               "notes": "CH1 lifetime measurement; laser internally clocked. User-specified SYNC. No DAQ/EOM control."}
        validate(cfg, detector_count=1, expected_laser_hz=None)
        if args.fit_window and args.fit_window[1] >= 1e9/args.laser_hz:
            raise ValueError("Fit window must end before the next excitation pulse.")
        print(json.dumps(cfg, indent=2))
        if not (args.rates or args.acquire):
            print("DRY RUN. Add --rates to check inputs or --acquire to record CH1.")
            return 0
        folder = run(PH330(args.dll), cfg, args.seconds, args.output, args.acquire)
        if folder:
            analyze(folder, args.rebin, args.fit_window, args.show)
        return 0
    except KeyboardInterrupt:
        print("Interrupted. Check metadata for acquisition completion status.")
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"CH1 lifetime: {exc}")
        if folder:
            print(f"Raw acquisition is saved at {folder}; plotting/fitting can be retried offline.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
