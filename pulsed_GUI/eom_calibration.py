"""Offline calibration workflow shared with the measured-grid notebook."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from figure_options import FIELD as DPI_FIELD, validated_dpi
from record_io import save_json
from calibration_transfer import payload

TOOL = "EOM calibration analysis"
ROOT = Path(__file__).resolve().parents[1]
HELP = ("Offline measured-grid calibration: four Alice voltages and two Bob voltages selected jointly. "
        "Use a completed sweep CSV; keep its JSON/JSONL sidecars beside it. Candidate selection uses training "
        "counts only; held-out and optional independent checks remain separate. H/R/V/L names are provisional. "
        "After reviewing the results, load the six voltages into a measurement form below. No hardware is accessed.")
FORM = [
    ("action", "Action", "Analyze sweep", ["Analyze sweep"]),
    ("csv", "Completed calibration sweep (*_Detector_Traces.csv)", "", "file"),
    ("independent-csv", "Optional independent confirmation sweep (blank = none)", "", "file"),
    *[(f"alice-{i}-seed", f"Alice S{i} / provisional {s}: search center (EOM V)", str(v), None)
      for i, (s, v) in enumerate(zip("HRVL", (-165, -61, 43, 147)))],
    ("bob-0-seed", "Bob setting 1 / H-V candidate: search center (EOM V)", "30", None),
    ("bob-1-seed", "Bob setting 2 / R-L candidate: search center (EOM V)", "-68", None),
    ("alice-radius", "Alice search radius around each center (V)", "35", None),
    ("bob-radius", "Bob search radius around each center (V)", "35", None),
    ("balance-tolerance", "Allowed error around 0.5 detected fraction", "0.05", None),
    ("extinction-tolerance", "Allowed wrong-detector fraction for 0/1 targets", "0.15", None),
    ("min-counts", "Minimum training photons per voltage pair", "50", None),
    ("training-fraction", "Fraction of counts used for candidate selection", "0.70", None),
    ("confidence", "Simultaneous confidence for all eight checks", "0.95", None),
    ("split-seed", "Fixed count-split seed (do not tune to improve check results)", "20260929", None),
    ("legacy-dwell", "Legacy NI CSV only: assumed dwell (s; blank = require exact sidecars)", "", None),
    DPI_FIELD,
    ("output", "Optional analysis directory override (blank = beside source run)", "", "directory"),
    ("note", "Analysis notes", "", None),
]


def settings(values):
    def number(key, low, high):
        value = float(values[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}.")
        return value
    def integer(key, low, high):
        value = int(values[key])
        if not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}.")
        return value
    if not values.get("csv", "").strip():
        raise ValueError("Select a completed calibration sweep CSV, or click Use latest.")
    return dict(csv=str(Path(values["csv"]).resolve()), independent_csv=values.get("independent-csv", "").strip(),
                alice_seeds=[number(f"alice-{i}-seed", -200, 200) for i in range(4)],
                bob_seeds=[number(f"bob-{i}-seed", -200, 200) for i in range(2)],
                alice_radius=number("alice-radius", .001, 400), bob_radius=number("bob-radius", .001, 400),
                balance_tolerance=number("balance-tolerance", .00001, .49999),
                extinction_tolerance=number("extinction-tolerance", .00001, .49999),
                min_counts=integer("min-counts", 1, 10**12),
                training_fraction=number("training-fraction", .01, .99),
                confidence=number("confidence", .01, .99999), seed=integer("split-seed", 0, 2**32-1),
                legacy_dwell=number("legacy-dwell", .000001, 360000) if values.get("legacy-dwell", "").strip() else None,
                plot_dpi=validated_dpi(values.get("plot-dpi", "300")), output=values["output"], note=values.get("note", ""))


def analyze(cfg, stop=None):
    # Lazy imports keep instrument-only controls usable without analysis packages.
    import pandas as pd
    import eom_calibration_core as cal
    def check():
        if stop is not None and stop.is_set():
            raise KeyboardInterrupt
    check()
    print(f"Reading calibration sweep: {cfg['csv']}", flush=True)
    grid = cal.load_sweep(cfg["csv"], cfg["legacy_dwell"])
    train, heldout = cal.split_counts(grid, cfg["training_fraction"], cfg["seed"])
    print(f"Searching measured grid: {len(grid.alice)} Alice × {len(grid.bob)} Bob voltages.", flush=True)
    candidates = cal.search(grid, train, cfg["alice_seeds"], cfg["bob_seeds"],
                            cfg["alice_radius"], cfg["bob_radius"], cfg["balance_tolerance"],
                            cfg["extinction_tolerance"], cfg["min_counts"], stop_event=stop)
    selected = candidates[0]
    def evaluate(g, candidate, counts):
        return cal.evaluate(g, candidate, counts, cfg["balance_tolerance"], cfg["extinction_tolerance"], cfg["confidence"])
    full = evaluate(grid, selected, grid.counts)
    held = evaluate(grid, selected, heldout)
    held_pass = bool(grid.exact_counts and held.interval_within_tolerance.all())
    independent = other = None
    independent_pass = False
    conditions_match = None
    if cfg["independent_csv"]:
        check()
        other = cal.load_sweep(cfg["independent_csv"], cfg["legacy_dwell"])
        if other.provenance["csv_sha256"] == grid.provenance["csv_sha256"]:
            raise ValueError("Independent validation cannot reuse the same CSV.")
        independent = evaluate(other, cal.match_candidate(other, selected), other.counts)
        def conditions(g):
            saved = g.provenance["acquisition_settings"] or {}
            return {key: saved.get(key) for key in ("source", "clock", "ph330")}
        conditions_match = conditions(grid) == conditions(other)
        independent_pass = bool(grid.exact_counts and other.exact_counts and conditions_match
                                and independent.interval_within_tolerance.all())
    check()
    analysis_root = analysis_directory(grid.path, cfg["output"])
    output = analysis_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output.mkdir(parents=True, exist_ok=False)
    report_path = output / "analysis.json"
    report = dict(schema="qkd-eom-calibration-analysis-v1", status="writing", parameters=cfg,
                  source=grid.provenance, selected=selected, target_ch1=cal.TARGET.tolist(),
                  physical_state_labels_confirmed=False, heldout_count_check_passed=held_pass,
                  independent_count_check_passed=independent_pass, independent_conditions_match=conditions_match,
                  independent_source=other.provenance if other else None,
                  full_data_checks=json.loads(full.to_json(orient="records")),
                  heldout_checks=json.loads(held.to_json(orient="records")),
                  helper_sha256=hashlib.sha256(Path(cal.__file__).read_bytes()).hexdigest(),
                  calibration_target="Detected fractions; no background/efficiency correction")
    save_json(report_path, report)
    try:
        ranking = pd.DataFrame([dict(rank=i+1, training_score=c["score"],
                                    training_targets_feasible=c["training_point_feasible"],
                                    **dict(zip(cal.STATES, c["alice_v"])), Bob1=c["bob_v"][0], Bob2=c["bob_v"][1])
                                for i, c in enumerate(candidates)])
        ranking.to_csv(output / "training_candidates.csv", index=False)
        full.to_csv(output / "selected_measured_points.csv", index=False)
        held.to_csv(output / "heldout_checks.csv", index=False)
        cal.refinement_plan(grid, selected).to_csv(output / "proposed_refinement_points_UNMEASURED.csv", index=False)
        if independent is not None:
            independent.to_csv(output / "independent_checks.csv", index=False)
        check()
        from eom_calibration_figures import render
        render(grid, selected, full, held, output, cfg["plot_dpi"], check)
        check()
        transfer = payload(selected, grid.path, held_pass, independent_pass)
        transfer.update(analysis_report=str(report_path.resolve()), csv_sha256=grid.provenance["csv_sha256"])
        save_json(output / "calibration_for_app.json", transfer)
        report["status"] = "completed"
        save_json(report_path, report)
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
        save_json(report_path, report)
        raise
    print("Alice [S0/H, S1/R, S2/V, S3/L] (provisional):", selected["alice_v"], flush=True)
    print("Bob [setting 1/HV, setting 2/RL] (provisional):", selected["bob_v"], flush=True)
    print(full[["state", "bob_setting", "alice_v", "bob_v", "target_ch1", "measured_ch1"]].to_string(index=False), flush=True)
    if not selected["training_point_feasible"]:
        print("No training-feasible set in these windows: candidate is a compromise.", flush=True)
    print(f"Held-out count check: {'PASS' if held_pass else 'NOT VALIDATED'}. "
          f"Independent check: {'PASS' if independent_pass else 'NOT VALIDATED'}. Physical labels remain unverified.", flush=True)
    print(f"Preview image: {(output / 'heldout_check.png').resolve()}", flush=True)
    print(f"Preview image: {(output / 'selected_matrix.png').resolve()}", flush=True)
    print(f"Calibration result: {report_path.resolve()}", flush=True)
    print(f"Calibration transfer: {(output / 'calibration_for_app.json').resolve()}", flush=True)
    return output


def analysis_directory(csv, override=""):
    """New run folders contain analysis/; legacy flat CSVs get per-source grouping."""
    if str(override).strip():
        return Path(override)
    csv = Path(csv).resolve()
    stem = csv.name.removesuffix("_Detector_Traces.csv")
    meta_path = csv.with_name(stem + "_sweep.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    if meta.get("layout") == "one-folder-per-sweep-v1":
        return csv.parent / "analysis"
    return csv.parent / "analysis" / stem


def main(values, stop_event=None):
    try:
        analyze(settings(values), stop_event)
        return 0
    except KeyboardInterrupt:
        print("Calibration analysis stopped. No calibration was automatically applied.", flush=True)
        return 130
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"Calibration analysis: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch(TOOL)
