"""Offline joint-response fitting and independent fixed-voltage verification."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from figure_options import FIELD as DPI_FIELD, validated_dpi
from record_io import save_json

TOOL = "EOM calibration analysis"
FIT, VERIFY = "Fit sweep", "Verify fitted voltages"
SCHEMA = "qkd-eom-model-calibration-v2"
HELP = ("Fit both detector responses with one sinusoidal phase, separate amplitudes/backgrounds, and all counts. "
        "Jointly match eight detected-fraction targets, then minimize the sum of six squared voltages over "
        "equivalent phase wraps within the measured range and operating limits. The ±180 V defaults are "
        "editable software margins, not hardware ratings. Load the candidate into Polarization matrix, "
        "record all eight settings, then Verify fitted voltages using that run. Verification freezes the "
        "candidate; it never retunes it. Physical labels remain provisional.")
FORM = [
    ("action", "Action", FIT, [FIT, VERIFY]),
    ("csv", "Fit: completed sweep (*_Detector_Traces.csv)", "", "file"),
    ("fit-report", "Verify: original fitted analysis.json", "", "file"),
    ("verification-folder", "Verify: completed polarization run at these exact voltages", "", "directory"),
    ("alice-limit", "Alice: maximum absolute EOM target voltage (V)", "180", None),
    ("bob-limit", "Bob: maximum absolute EOM target voltage (V)", "180", None),
    ("fit-vpi-min", "Fit Vpi lower bound (V; must exceed sweep spacing)", "50", None),
    ("fit-vpi-max", "Fit Vpi upper bound (V)", "400", None),
    ("balance-tolerance", "Allowed absolute error around 0.5 detected fraction", "0.05", None),
    ("extinction-tolerance", "Allowed wrong-detector fraction for 0/1 targets", "0.15", None),
    ("confidence", "Verification: simultaneous confidence for eight fraction checks", "0.95", None),
    ("legacy-dwell", "Legacy NI CSV only: assumed dwell (s; blank = exact sidecars)", "", None),
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
    action = values.get("action", FIT)
    if action not in (FIT, VERIFY):
        raise ValueError("Select Fit sweep or Verify fitted voltages.")
    for key in (("csv",) if action == FIT else ("fit-report", "verification-folder")):
        if not values.get(key, "").strip():
            raise ValueError(f"Select {key}, or click Use latest.")
    cfg = dict(action=action, output=values.get("output", ""), note=values.get("note", ""),
               plot_dpi=validated_dpi(values.get("plot-dpi", "300")))
    if action == VERIFY:
        return dict(cfg, fit_report=values["fit-report"], verification_folder=values["verification-folder"],
                    confidence=number("confidence", .5, .99999))
    cfg.update(csv=str(Path(values["csv"]).resolve()),
               alice_limit=number("alice-limit", .001, 199.999), bob_limit=number("bob-limit", .001, 199.999),
               vpi_min=number("fit-vpi-min", .001, 10000), vpi_max=number("fit-vpi-max", .001, 10000),
               balance_tolerance=number("balance-tolerance", .00001, .49999),
               extinction_tolerance=number("extinction-tolerance", .00001, .49999),
               legacy_dwell=number("legacy-dwell", .000001, 360000) if values.get("legacy-dwell", "").strip() else None)
    if cfg["vpi_min"] >= cfg["vpi_max"]:
        raise ValueError("Vpi lower bound must be less than upper bound.")
    return cfg


def new_output(root):
    output = Path(root) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    output.mkdir(parents=True, exist_ok=False)
    return output


def model_transfer(report, report_path):
    from calibration_transfer import payload
    transfer = payload(report["selected"], report["parameters"]["csv"])
    transfer.pop("heldout_count_check_passed")
    transfer.update(method="joint-sinusoidal-poisson-v2", analysis_report=str(report_path.resolve()),
                    csv_sha256=report["source"]["csv_sha256"],
                    predicted_targets_passed=report["selected"]["predicted_targets_pass"],
                    poisson_model_check_passed=report["fit"]["poisson_model_check_passed"],
                    fit_diagnostics=report["fit"]["diagnostics"],
                    operating_bounds=report["selected"]["operating_bounds"],
                    status="Fitted candidate for independent bench verification; physical labels unverified")
    return transfer


def analyze(cfg, stop=None):
    import eom_calibration_core as cal
    import eom_model_calibration as model
    if cfg.get("action") == VERIFY:
        from eom_calibration_verify import verify
        return verify(cfg, stop)
    model.check_stop(stop)
    print(f"Reading calibration sweep: {cfg['csv']}", flush=True)
    grid = cal.load_sweep(cfg["csv"], cfg["legacy_dwell"])
    print(f"Joint Poisson fit: {len(grid.alice)} Alice x {len(grid.bob)} Bob voltages; all counts used.", flush=True)
    fit = model.fit_response(grid, cfg["vpi_min"], cfg["vpi_max"], stop)
    selected, rejection = None, None
    try:
        selected = model.choose_voltages(fit, grid, cfg["alice_limit"], cfg["bob_limit"],
                                        cfg["balance_tolerance"], cfg["extinction_tolerance"], stop)
    except ValueError as exc:
        rejection = str(exc)
    eligible = bool(fit["fit_identified"] and selected and selected["predicted_targets_pass"])
    if not eligible and rejection is None:
        rejection = "Unidentified fit or predicted fractions fail target tolerances. No app transfer exported."
    model.check_stop(stop)
    output = new_output(analysis_directory(grid.path, cfg["output"]))
    report_path = output / "analysis.json"
    report = dict(schema=SCHEMA, status="writing", parameters=cfg, source=grid.provenance,
                  fit=fit, selected=selected, target_ch1=cal.TARGET.tolist(), transfer_available=eligible,
                  rejection_reason=rejection, physical_state_labels_confirmed=False,
                  independent_count_check_passed=False, measurement_status="Fitted predictions; no independent verification yet",
                  helper_sha256=hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest(),
                  calibration_target="Ungated detected fractions; no background subtraction or detector-efficiency correction")
    save_json(report_path, report)
    try:
        table = model.prediction_table(selected, cfg["balance_tolerance"], cfg["extinction_tolerance"]) if selected else None
        if table is not None:
            table.to_csv(output / "predicted_fractions.csv", index=False)
            report["predictions"] = json.loads(table.to_json(orient="records"))
        from eom_calibration_figures import render
        render(grid, fit, selected, table, output, cfg["plot_dpi"], lambda: model.check_stop(stop))
        model.check_stop(stop)
        if eligible:
            save_json(output / "calibration_for_app.json", model_transfer(report, report_path))
        report["status"] = "completed"
        save_json(report_path, report)
    except BaseException as exc:
        report.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error", error=str(exc))
        save_json(report_path, report)
        raise
    print(f"Vpi Alice/Bob: {fit['vpi_alice_v']:.3f} / {fit['vpi_bob_v']:.3f} V. "
          f"Poisson deviance/dof: {fit['reduced_deviance']:.3f}.", flush=True)
    for diagnostic in fit["diagnostics"]:
        print("Fit diagnostic: " + diagnostic, flush=True)
    if not grid.exact_counts:
        print("Legacy CSV: reconstructed counts depend on assumed dwell; uncertainty is provisional.", flush=True)
    if selected:
        print("Alice [S0/H, S1/R, S2/V, S3/L] (provisional):", selected["alice_v"], flush=True)
        print("Bob [setting 1/HV, setting 2/RL] (provisional):", selected["bob_v"], flush=True)
        print(table[["state", "bob_setting", "alice_v", "bob_v", "target_ch1", "predicted_ch1"]].to_string(index=False), flush=True)
    if rejection:
        print("Candidate NOT accepted: " + rejection, flush=True)
    else:
        print("Candidate exported for a fresh eight-setting polarization measurement. Not experimentally validated.", flush=True)
    print(f"Preview image: {(output / 'response_fit.png').resolve()}", flush=True)
    if selected:
        print(f"Preview image: {(output / 'predicted_fractions.png').resolve()}", flush=True)
    print(f"Calibration result: {report_path.resolve()}", flush=True)
    if eligible:
        print(f"Calibration transfer: {(output / 'calibration_for_app.json').resolve()}", flush=True)
    return output


def analysis_directory(csv, override=""):
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
