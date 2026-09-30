"""Offline BB84 commissioning diagnostics; no fitted or inferred trial origin."""
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path

import numpy as np

from bb84_sequence import STATES, BASES, file_sha256
from ph330_acquire import BAD_FLAGS
from ph330_preview import decode_t3
from figure_options import validated_dpi
from record_io import save_json

CHUNK = 1_048_576


def stopped(stop):
    if stop is not None and stop.is_set():
        raise KeyboardInterrupt


def wilson(success, total):
    """Pointwise 95% binomial counting intervals, NaN for empty settings."""
    k, n = np.asarray(success, dtype=float), np.asarray(total, dtype=float)
    p = np.divide(k, n, out=np.full_like(n, np.nan), where=n > 0)
    z = 1.959963984540054
    safe = np.where(n > 0, n, 1)
    center = (p + z*z/(2*safe))/(1+z*z/safe)
    width = z*np.sqrt(p*(1-p)/safe + z*z/(4*safe*safe))/(1+z*z/safe)
    return p, np.maximum(0, center-width), np.minimum(1, center+width)


def expected(mapping):
    if len(mapping) != 2 or mapping[0] not in ("H", "V") or mapping[1] not in ("R", "L"):
        raise ValueError("Choose CH1's outcome separately for the HV and RL analyzers.")
    return np.array([[float(s == mapping[b]) if s in BASES[b] else .5
                      for b in range(2)] for s in STATES])


def hypotheses(phase_counts):
    """All eight offsets; never choose a best phase from measured detections."""
    result = np.zeros((8, 4, 2, 2), dtype=np.int64)
    for offset in range(8):
        for phase in range(8):
            slot = (phase-offset) % 8
            result[offset, slot % 4, slot // 4] = phase_counts[phase]
    return result


def agreement(counts, target):
    total = counts.sum(axis=-1)
    p, low, high = wilson(counts[..., 0], total)
    error = np.abs(p-target)
    # An aggregate of a subset could hide an entirely unobserved setting.
    mae = float(error.mean()) if np.all(total > 0) else None
    matched = target != .5
    correct = int(np.where(target == 1, counts[..., 0], counts[..., 1])[matched].sum())
    n = int(total[matched].sum())
    bright, lo, hi = wilson(np.asarray(correct), np.asarray(n))
    return dict(ch1_fraction=p, ci_low=low, ci_high=high,
                mean_absolute_error=mae, matched_correct=correct, matched_total=n,
                matched_fraction=float(bright) if n else None,
                matched_interval=[float(lo), float(hi)] if n else None)


def summarize(folder, *, origin=None, origin_note="", sync_start=None, sync_stop=None,
              mapping=("V", "L"), gate=(50., 60.), stop_event=None):
    folder = Path(folder).resolve()
    session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    if session.get("schema") != "qkd-bb84-acquisition-v1":
        raise ValueError("Select the BB84 run folder containing session.json, not its tttr subfolder.")
    if not session.get("complete") or session.get("status") != "completed" or not session.get("ni_sequence_complete"):
        raise ValueError("This BB84 run did not complete. Inspect session.json; partial data is not a fidelity measurement.")
    plan = session["plan"]
    if plan["mode"] not in ("ordered", "random"):
        raise ValueError("Unsupported sequence mode.")
    target = expected(mapping)
    for name, value in (("origin", origin), ("sync_start", sync_start), ("sync_stop", sync_stop)):
        if value is not None and (type(value) is not int or abs(value) > 2**53):
            raise ValueError(f"{name} must be an integer SYNC index.")
    if origin is not None and not origin_note.strip():
        raise ValueError("Describe the independent reference for the supplied first-experimental-trial SYNC index.")
    if sync_start is not None and sync_start < 0 or sync_stop is not None and sync_stop < 0:
        raise ValueError("Diagnostic SYNC range must be nonnegative.")
    raw_folder = folder / "tttr"
    meta = json.loads((raw_folder / "metadata.json").read_text(encoding="utf-8"))
    if (meta.get("schema") != "qkd-ph330-raw-t3-v1" or meta.get("record_type") != "0x00010307"
            or meta.get("record_format") != "GenericT3" or not meta.get("complete") or meta.get("status") != "completed"):
        raise ValueError("Expected a complete PH330 GenericT3 recording.")
    flags = int(meta.get("flags_seen", 0)) & BAD_FLAGS
    phases = meta.get("flags_by_phase", {})
    if flags & ~4 or (flags & 4 and (not phases or phases.get("active", 0) & 4
                                    or not meta.get("startup_sync_clear_observed"))):
        raise ValueError("Recording reports unqualified SYNC loss or data-loss flags; analysis refused.")
    if flags & 4 and any(value & 4 for key, value in phases.items() if key not in ("startup", "tail", "stopped")):
        raise ValueError("SYNC loss is not confined to logged boundary phases.")
    resolution = float(meta["hardware"]["resolution_ps"])/1000
    rate = float(plan["realized_rate_hz"])
    if not math.isfinite(resolution) or resolution <= 0 or not math.isfinite(rate) or rate <= 0:
        raise ValueError("Invalid time resolution or planned rate.")
    if gate is not None and (len(gate) != 2 or not all(math.isfinite(x) for x in gate)
                            or not 0 <= gate[0] < gate[1] <= min(32768*resolution, 1e9/rate)):
        raise ValueError("Arrival gate must lie within the T3 range and excitation period.")
    lower = (max(0, origin) if origin is not None else plan["warmup_count"] + math.ceil(.5*rate)) if sync_start is None else sync_start
    upper = sync_stop
    if origin is not None:
        lower = max(lower, origin, 0)
        upper = min(upper, origin+plan["trial_count"]) if upper is not None else origin+plan["trial_count"]
    if upper is not None and upper <= lower:
        raise ValueError("Selected SYNC interval is empty; adjust its bounds or supplied origin.")
    choices = None
    if origin is not None:
        seq = json.loads((folder / "sequence.json").read_text(encoding="utf-8"))
        if (seq.get("schema") != "qkd-bb84-sequence-v1" or not seq.get("complete")
                or seq.get("mode") != plan["mode"] or seq.get("trial_count") != plan["trial_count"]
                or seq.get("warmup_count") != plan["warmup_count"]
                or seq.get("state_order") != list(STATES) or seq.get("basis_order") != list(BASES)
                or seq.get("alice_voltages") != plan["alice_voltages"] or seq.get("bob_voltages") != plan["bob_voltages"]):
            raise ValueError("Saved sequence metadata does not match this acquisition.")
        path = folder / "choices.npy"
        if file_sha256(path, stop_event) != seq["files"]["choices"]["sha256"]:
            raise ValueError("Saved choices checksum mismatch.")
        choices = np.load(path, mmap_mode="r", allow_pickle=False)
        if choices.dtype != np.uint8 or choices.shape != (plan["trial_count"], 2):
            raise ValueError("Invalid saved choices array.")
    raw = raw_folder / "events.t3raw"
    if raw.stat().st_size % 4 or raw.stat().st_size // 4 != meta["records"]:
        raise ValueError("Raw file size differs from metadata.")
    hist = np.zeros((2, 32768), dtype=np.int64)
    phase_counts = np.zeros((8, 2), dtype=np.int64)
    assigned = np.zeros((4, 2, 2), dtype=np.int64)
    carry = total = in_range = accepted = 0
    last_cycle = -1
    try:
        with raw.open("rb") as stream:
            while True:
                stopped(stop_event)
                words = np.fromfile(stream, dtype="<u4", count=CHUNK)
                if not len(words):
                    break
                ch, cycles, micro, carry, stats = decode_t3(words, carry)
                if np.any(ch > 1) or stats["marker_records"]:
                    raise ValueError("Unexpected input channel or marker; this analysis expects two-channel unmarked T3.")
                if len(cycles):
                    if cycles[0] < last_cycle or np.any(np.diff(cycles) < 0):
                        raise ValueError("Nonmonotonic SYNC indices; inspect raw data.")
                    last_cycle = int(cycles[-1])
                total += len(ch)
                selected = cycles >= lower
                if upper is not None:
                    selected &= cycles < upper
                ch, cycles, micro = ch[selected], cycles[selected], micro[selected]
                in_range += len(ch)
                for detector in (0, 1):
                    hist[detector] += np.bincount(micro[ch == detector].astype(int), minlength=32768)
                keep = np.ones(len(ch), dtype=bool) if gate is None else (micro*resolution >= gate[0]) & (micro*resolution < gate[1])
                ch, cycles = ch[keep].astype(np.int64), cycles[keep]
                accepted += len(ch)
                phase_counts += np.bincount((cycles % 8)*2+ch, minlength=16).reshape(8, 2)
                if choices is not None:
                    pair = choices[cycles-origin].astype(np.int64)
                    if np.any(pair[:, 0] > 3) or np.any(pair[:, 1] > 1):
                        raise ValueError("Invalid Alice/Bob choice index.")
                    assigned += np.bincount(pair[:, 0]*4 + pair[:, 1]*2 + ch, minlength=16).reshape(4, 2, 2)
    finally:
        if choices is not None:
            choices._mmap.close()
    if not accepted:
        raise ValueError("No photons in the selected SYNC range and arrival gate. Check the gate/range or record longer.")
    return dict(session=session, meta=meta, target=target, mapping=list(mapping), gate=gate,
                origin=origin, origin_note=origin_note, sync_range=[lower, upper],
                hist=hist, resolution_ns=resolution, phase_counts=phase_counts,
                assigned=assigned if origin is not None else None,
                hypotheses=hypotheses(phase_counts) if plan["mode"] == "ordered" and origin is None else None,
                total_photons=total, range_photons=in_range, accepted_photons=accepted,
                mode="supplied_origin" if origin is not None else "ordered_phase_diagnostic" if plan["mode"] == "ordered" else "unassigned_random_diagnostic")


def render(result, output, dpi):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=(14, 10), constrained_layout=True)
    FigureCanvasAgg(fig)
    axes = fig.subplots(2, 2)
    ax = axes[0, 0]
    edges = np.arange(32769)*result["resolution_ns"]
    gate = result["gate"]
    lo, hi = (max(0, gate[0]-5), min(edges[-1], gate[1]+5)) if gate else (0, min(edges[-1], 1e9/result["session"]["plan"]["realized_rate_hz"]))
    bins = np.flatnonzero((edges[:-1] < hi) & (edges[1:] > lo))
    for ch, color in enumerate(("#0072B2", "#D55E00")):
        ax.stairs(result["hist"][ch, bins[0]:bins[-1]+1], edges[bins[0]:bins[-1]+2], color=color, label=f"CH{ch+1}", linewidth=1)
    if gate:
        ax.axvspan(*gate, color="gray", alpha=.12)
    ax.set(xlim=(lo, hi), xlabel="Arrival time after SYNC (ns)", ylabel=f"Photons / {result['resolution_ns']:g} ns bin", title="Arrival distribution · selected SYNC interval")
    ax.legend()
    ax = axes[0, 1]
    p, low, high = wilson(result["phase_counts"][:, 0], result["phase_counts"].sum(axis=1))
    ax.errorbar(range(8), p, yerr=[np.maximum(0,p-low), np.maximum(0,high-p)], fmt="o", capsize=4, color="#0072B2")
    ax.axhline(.5, color="gray", linestyle="--")
    ax.set(xticks=range(8), ylim=(-.05, 1.05), xlabel="Recorded SYNC index modulo 8 (not trial index)", ylabel="CH1 / (CH1 + CH2)", title="Observed eight-cycle pattern · pointwise 95% intervals")
    target = result["target"]
    labels = [f"{s}/{b}\nideal {target[i,j]:.0%}" for i,s in enumerate(STATES) for j,b in enumerate(BASES)]
    if result["hypotheses"] is not None:
        stats = [agreement(c, target) for c in result["hypotheses"]]
        values = np.array([s["ch1_fraction"].ravel() for s in stats])
        ax = axes[1, 0]
        plot = ax.imshow(values-target.ravel(), vmin=-1, vmax=1, cmap="RdBu_r", aspect="auto")
        for y in range(8):
            for x in range(8):
                ax.text(x, y, f"{values[y,x]:.0%}" if np.isfinite(values[y,x]) else "N/A", ha="center", va="center", fontsize=8)
        ax.set(xticks=range(8), xticklabels=labels, yticks=range(8), ylabel="Assumed trial-zero SYNC phase", title="All eight hypotheses · numbers = measured CH1 fraction")
        ax.tick_params(axis="x", labelsize=8)
        fig.colorbar(plot, ax=ax, label="Measured − ideal CH1 fraction", shrink=.75)
        ax = axes[1, 1]
        mae = [np.nan if s["mean_absolute_error"] is None else 100*s["mean_absolute_error"] for s in stats]
        ax.bar(range(8), mae, color="#607D8B")
        ax.set(xticks=range(8), xlabel="Assumed trial-zero SYNC phase", ylabel="Mean absolute deviation (percentage points)", title="Agreement across all eight settings · no phase selected")
        for i,v in enumerate(mae):
            if np.isfinite(v):
                ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=9)
        ax.margins(y=.2)
    elif result["assigned"] is not None:
        stats = agreement(result["assigned"], target)
        ax = axes[1, 0]
        p, low, high = (stats[key].ravel() for key in ("ch1_fraction", "ci_low", "ci_high"))
        ax.errorbar(range(8), p, yerr=[np.maximum(0,p-low), np.maximum(0,high-p)], fmt="o", capsize=4, label="Measured, 95% interval")
        ax.scatter(range(8), target.ravel(), marker="_", s=300, c="black", label="Ideal")
        ax.set(xticks=range(8), xticklabels=labels, ylim=(-.06,1.06), ylabel="CH1 fraction", title="Comparison conditional on supplied trial origin")
        ax.tick_params(axis="x", labelsize=8)
        ax.legend(fontsize=8)
        ax = axes[1, 1]
        matrix = np.full((4, 4), np.nan)
        for i in range(4):
            for b in range(2):
                first = BASES[b].index(result["mapping"][b])
                matrix[i, 2*b+first] = stats["ch1_fraction"][i,b]
                matrix[i, 2*b+1-first] = 1-stats["ch1_fraction"][i,b]
        plot = ax.imshow(matrix, vmin=0, vmax=1, cmap="Blues")
        for i in range(4):
            for j in range(4):
                ax.text(j,i,f"{matrix[i,j]:.1%}" if np.isfinite(matrix[i,j]) else "N/A", ha="center", va="center", color="white" if matrix[i,j]>.65 else "black")
        ax.set(xticks=range(4), xticklabels=STATES, yticks=range(4), yticklabels=STATES, xlabel="Assumed detector outcome", ylabel="Alice state", title="Conditional detection matrix · supplied origin")
        fig.colorbar(plot, ax=ax, shrink=.75)
    else:
        for ax in axes[1]:
            ax.axis("off")
        axes[1,0].text(.05,.7,"Randomized run: no trial origin supplied.\n\nArrival and phase diagnostics only.\nState fidelity requires independently established\nSYNC-to-trial correspondence and saved choices.", fontsize=13)
    gate_text = "ungated" if gate is None else f"gate {gate[0]:g}–{gate[1]:g} ns"
    fig.suptitle(f"{'Ordered' if result['session']['plan']['mode']=='ordered' else 'Randomized'} BB84 · commissioning diagnostic\n"
                 f"{result['accepted_photons']:,} selected photons · {gate_text} · trial alignment UNVERIFIED", fontsize=16)
    fig.supxlabel(f"Assumed CH1 outcomes: HV→{result['mapping'][0]}, RL→{result['mapping'][1]}. "
                  "Physical labels provisional. No background/efficiency correction.\n"
                  "Phase agreement does not prove trial origin, missing-pulse integrity, QBER, or secure-key performance.", fontsize=10)
    for ext in ("png", "pdf", "svg"):
        fig.savefig(output / f"bb84_diagnostic.{ext}", dpi=dpi, facecolor="white")
    fig.clear()


def analyze(folder, *, dpi=300, **kwargs):
    dpi = validated_dpi(dpi)
    result = summarize(folder, **kwargs)
    stopped(kwargs.get("stop_event"))
    output = Path(folder).resolve() / "analysis" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output.mkdir(parents=True, exist_ok=False)
    report = {k: result[k] for k in ("mode", "origin", "origin_note", "sync_range", "mapping", "gate", "resolution_ns", "total_photons", "range_photons", "accepted_photons")}
    report.update(schema="qkd-bb84-analysis-v1", status="writing", source_session=str(Path(folder).resolve()/"session.json"),
                  alignment_status="unverified", eligible_for_key_processing=False, plot_dpi=dpi,
                  flags_seen=result["meta"].get("flags_seen",0), flags_by_phase=result["meta"].get("flags_by_phase", {}),
                  note="All phase offsets are hypotheses, never a fitted/verified origin. Pointwise Wilson intervals assume binomial detection counts; no simultaneous confidence or security claim.",
                  selection_note="Default diagnostic lower SYNC bound is warmup_count + 0.5 s worth of cycles; it is a conservative display cut, not a measured warm-up boundary. No default upper cut. Boundary photons may remain.",
                  alice_voltages=result["session"]["plan"]["alice_voltages"], bob_voltages=result["session"]["plan"]["bob_voltages"])
    save_json(output/"analysis.json", report)
    counts = result["hypotheses"] if result["hypotheses"] is not None else [result["assigned"]] if result["assigned"] is not None else []
    metrics = []
    with (output/"setting_statistics.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["phase_hypothesis", "alice_state", "bob_basis", "CH1", "CH2", "expected_CH1_fraction", "measured_CH1_fraction", "ci95_low", "ci95_high"])
        for phase, matrix in enumerate(counts):
            s = agreement(matrix, result["target"])
            metrics.append(dict(phase_hypothesis=phase if result["hypotheses"] is not None else None,
                                **{k:s[k] for k in ("mean_absolute_error","matched_correct","matched_total","matched_fraction","matched_interval")}))
            for i,state in enumerate(STATES):
                for b,basis in enumerate(BASES):
                    vals = [s[k][i,b] for k in ("ch1_fraction","ci_low","ci_high")]
                    writer.writerow([phase if result["hypotheses"] is not None else "supplied_origin",state,basis,*matrix[i,b],result["target"][i,b],*[v if np.isfinite(v) else "" for v in vals]])
    np.savez_compressed(output/"diagnostics.npz", phase_counts=result["phase_counts"], arrival_counts=result["hist"], arrival_edges_ns=np.arange(32769)*result["resolution_ns"])
    stopped(kwargs.get("stop_event"))
    render(result, output, dpi)
    report.update(status="completed", phase_hypotheses=metrics)
    save_json(output/"analysis.json", report)
    print(f"Analyzed {result['total_photons']:,} photons; selected {result['accepted_photons']:,}. All raw records scanned.")
    print("Origin remains UNVERIFIED. This is a transmission/count-ratio diagnostic, not quantum-state fidelity or QBER.")
    print(f"Run record: {(Path(folder).resolve()/'session.json')}")
    print(f"Preview image: {(output/'bb84_diagnostic.png').resolve()}")
    return output
