"""Typed recent data locations and conservative, schema-checked fallbacks."""
import json
from pathlib import Path

REGISTRY = "__recent_data__"
PROFILES = {
    "bb84_ordered": ("Ordered BB84", "data/bb84/ordered", "session.json"),
    "bb84_random": ("Randomized BB84", "data/bb84/random", "session.json"),
    "sweep": ("EOM calibration sweep", "data/eoms", "*_Detector_Traces.csv"),
    "calibration": ("EOM calibration analysis", "data/eoms", "calibration_for_app.json"),
    "polarization": ("Pulsed polarization", "data/polarization", "session.json"),
    "g2": ("Pulsed / CW g2", "data/g2", "metadata.json"),
    "lifetime": ("CH1 lifetime", "pulsed_GUI/runs/lifetime_ch1", "metadata.json"),
    "preview": ("G2 acquisition", "pulsed_GUI/runs/ph330", "metadata.json"),
}
INPUTS = {("EOM calibration analysis", "csv"): "sweep",
          ("Ordered BB84", "folder"): "bb84_ordered", ("Randomized BB84", "folder"): "bb84_random",
          ("EOM calibration analysis", "independent-csv"): "sweep",
          ("Pulsed polarization", "folder"): "polarization",
          ("Pulsed / CW g2", "folder"): "g2", ("CH1 lifetime", "folder"): "lifetime"}


def read(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError):
        return {}


def valid(kind, path):
    """Filter partial sweeps, incompatible TTTR formats, and unfinished analyses."""
    try:
        return bool(_valid(kind, path))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _valid(kind, path):
    path = Path(path)
    if kind == "sweep":
        if not path.is_file() or not path.name.endswith("_Detector_Traces.csv"):
            return False
        meta = path.with_name(path.name.removesuffix("_Detector_Traces.csv") + "_sweep.json")
        data = read(meta)
        # Legacy CSVs remain selectable; the analysis requires explicit dwell.
        return not meta.exists() or (data.get("schema") == "qkd-eom-picoharp-sweep-v1"
                                    and data.get("complete") and not data.get("cleanup_errors")
                                    and data.get("status") == "completed")
    if kind == "calibration":
        if not path.is_file():
            return False
        try:
            from calibration_transfer import parse
            parse(path.read_text(encoding="utf-8"))
            return read(path.parent / "analysis.json").get("status") == "completed"
        except (ValueError, OSError, TypeError):
            return False
    manifest = path / ("session.json" if kind == "polarization" or kind.startswith("bb84_") else "metadata.json")
    data = read(manifest)
    if kind.startswith("bb84_"):
        return (data.get("schema") == "qkd-bb84-acquisition-v1" and data.get("complete")
                and data.get("status") == "completed" and not data.get("cleanup_errors")
                and data.get("plan", {}).get("mode") == kind.removeprefix("bb84_"))
    if kind == "polarization":
        return data.get("schema") == "qkd-static-polarization-v1" and any(
            b.get("status") == "completed" for b in data.get("blocks", []))
    if not data.get("complete") or data.get("status") != "completed":
        return False
    if kind == "g2":
        return data.get("schema") == "qkd-g2-tttr-v1" and not data.get("cleanup_errors")
    if data.get("schema") != "qkd-ph330-raw-t3-v1":
        return False
    detectors = data.get("config", {}).get("detectors", [])
    return kind == "preview" or (len(detectors) == 1 and detectors[0].get("channel") == 0)


def remember(settings, kind, path):
    path = Path(path).resolve()
    if valid(kind, path):
        registry = settings.get(REGISTRY)
        if not isinstance(registry, dict):
            registry = settings[REGISTRY] = {}
        registry[kind] = str(path)
        return True
    return False


def roots(kind, settings, root):
    tool, default, _ = PROFILES[kind]
    saved = settings.get(tool, {})
    configured = saved.get("output", "") if isinstance(saved, dict) else ""
    result = [Path(configured)] if configured else []
    if kind == "calibration":
        # Analyses can live under a custom sweep output root or an imported CSV.
        result.extend(roots("sweep", settings, root))
        csv = saved.get("csv", "") if isinstance(saved, dict) else ""
        if csv:
            result.append(Path(csv).parent)
    result.append(Path(root) / default)
    return list(dict.fromkeys(result))


def latest(kind, settings, root):
    registry = settings.get(REGISTRY, {})
    saved = registry.get(kind) if isinstance(registry, dict) else None
    if saved and valid(kind, Path(saved)):
        return Path(saved)
    candidates = []
    for directory in roots(kind, settings, root):
        if not directory.is_dir():
            continue
        try:
            for artifact in directory.rglob(PROFILES[kind][2]):
                path = artifact if kind in ("sweep", "calibration") else artifact.parent
                candidates.append((artifact.stat().st_mtime_ns, path))
        except OSError:
            continue
    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        if valid(kind, path):
            return path.resolve()
    return None


def dialog_options(current, kind, key, settings, root, tool):
    """Prefer an explicit selection, then recent compatible data, then its root."""
    family = INPUTS.get((tool, key))
    path = Path(current).expanduser() if current.strip() else None
    if path is None and family:
        path = latest(family, settings, root)
        if path is None:
            path = roots(family, settings, root)[0]
    if path is None:
        path = Path(root)
    options = {}
    if kind == "file" and path.is_file():
        options["initialfile"] = path.name
    directory = path.parent if path.is_file() or (kind == "file" and path.suffix) else path
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    options["initialdir"] = str(directory.resolve()) if directory.is_dir() else str(root)
    if kind == "file":
        options["filetypes"] = [("Completed calibration sweeps", "*_Detector_Traces.csv"), ("CSV files", "*.csv")] if key in ("csv", "independent-csv") else [("DLL libraries", "*.dll")] if key == "dll" else [("All files", "*")]
    return options
