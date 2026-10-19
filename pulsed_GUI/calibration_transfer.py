"""Offline, versioned notebook-to-form transfer. Never writes to hardware."""
import json
import math

SCHEMA = "qkd-eom-calibration-transfer-v1"
DEFAULT_LABELS = ["H", "R", "V", "L"]
TOOLS = ("Pulsed polarization", "Ordered BB84", "Randomized BB84")


def publish(settings, data):
    """An explicit import updates all three experiment profiles atomically."""
    all_updates = {tool: form_updates(data, tool == "Pulsed polarization") for tool in TOOLS}
    for tool, updates in all_updates.items():
        profile = dict(settings.get(tool, {}))
        profile.update(updates)
        if tool != "Pulsed polarization":
            profile["calibration-mode"] = "Imported calibration"
        settings[tool] = profile
    settings["__shared_calibration__"] = parse(json.dumps(data))


def require_record_calibration(cfg):
    """Prevent stale/manual edits being mistaken for an imported calibration."""
    mode = cfg.get("calibration_mode", "Manual voltages")
    context = cfg.get("calibration_transfer")
    if mode == "Imported calibration":
        if not context or not context.get("current_voltages_match_import"):
            raise ValueError("Import/paste a calibration before Record. The six voltages must match it, "
                             "or explicitly select Manual voltages for an intentional override.")
        data = parse(json.dumps(context["imported_candidate"]))
        if (cfg["alice_voltages"] != dict(zip(DEFAULT_LABELS, data["alice_v"]))
                or cfg["bob_voltages"] != dict(zip(("HV", "RL"), data["bob_v"]))):
            raise ValueError("Requested BB84 waveform voltages differ from the imported calibration.")
    elif mode != "Manual voltages":
        raise ValueError("Select an EOM calibration mode.")


def parse(text):
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("units") != "EOM target V":
        raise ValueError("Copy the complete calibration JSON from the notebook's final cell (EOM target volts).")
    for key, size in (("alice_v", 4), ("bob_v", 2)):
        vals = data.get(key)
        if not isinstance(vals, list) or len(vals) != size:
            raise ValueError(f"Calibration requires {size} values in {key}.")
        if any(type(v) not in (int, float) or not math.isfinite(v) or not -200 <= v <= 200 for v in vals):
            raise ValueError("Calibration voltages must be finite numbers within ±200 EOM target volts.")
        if len(set(vals)) != size:
            raise ValueError(f"Calibration {key} values must be distinct.")
    if data.get("alice_labels") != DEFAULT_LABELS:
        raise ValueError("This transfer expects S0/S1/S2/S3 = provisional H/R/V/L; check the notebook convention.")
    if data.get("physical_labels_confirmed") is not False:
        raise ValueError("Notebook transfer must retain physical_labels_confirmed=false; confirm labels separately on the bench.")
    if data.get("method") in ("joint-sinusoidal-poisson-v2", "local-phase-poisson-v1", "local-curve-fit-v1"):
        if data.get("predicted_targets_passed") is not True:
            raise ValueError("The physical-fit candidate did not meet the configured fraction targets.")
        for role in ("alice", "bob"):
            limits = data.get("operating_bounds", {}).get(role, [])
            if (len(limits) != 2 or not all(isinstance(v, (int,float)) and math.isfinite(v) for v in limits)
                    or not -200 < limits[0] < limits[1] < 200
                    or any(v < limits[0]-1e-7 or v > limits[1]+1e-7 for v in data[role+"_v"])):
                raise ValueError("Fitted voltages must remain within their saved operating margins.")
    return data


def payload(selected, source, heldout_pass=False, independent_pass=False):
    data = dict(schema=SCHEMA, units="EOM target V",
                alice_v=[float(v) for v in selected["alice_v"]],
                bob_v=[float(v) for v in selected["bob_v"]], alice_labels=DEFAULT_LABELS.copy(),
                bob_order=["HV candidate (notebook setting 1)", "RL candidate (notebook setting 2)"],
                physical_labels_confirmed=False, source=str(source),
                heldout_count_check_passed=bool(heldout_pass), independent_count_check_passed=bool(independent_pass),
                status="Candidate; physical labels and optical performance require bench confirmation")
    return parse(json.dumps(data, allow_nan=False))


def form_updates(data, indexed):
    """Return all edits together after validating the entire payload."""
    data = parse(json.dumps(data, allow_nan=False))
    updates = {"bob-hv-v": str(data["bob_v"][0]), "bob-rl-v": str(data["bob_v"][1]),
               "calibration-transfer": json.dumps(data, allow_nan=False)}
    if indexed:
        updates.update({f"alice-{i}-v": str(v) for i, v in enumerate(data["alice_v"])})
        updates.update({f"alice-{i}-state": s for i, s in enumerate(DEFAULT_LABELS)})
        updates.update({"alice-labels": "Voltage index only", "hv-ch1": "Unassigned", "rl-ch1": "Unassigned"})
    else:
        updates.update({f"alice-{s.lower()}-v": str(v) for s, v in zip(DEFAULT_LABELS, data["alice_v"])})
        updates["labels-confirmed"] = "No"
    return updates


def context(values):
    """Save the imported candidate and flag subsequent manual voltage edits."""
    text = values.get("calibration-transfer", "")
    if not text:
        return None
    data = parse(text)
    updates = form_updates(data, "alice-0-v" in values)
    matches = all(float(values[k]) == float(v) for k, v in updates.items() if k.endswith("-v"))
    return dict(imported_candidate=data, current_voltages_match_import=matches)
