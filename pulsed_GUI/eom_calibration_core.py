"""Measured-grid EOM calibration. Offline only; no instrument imports.

Raw_Count columns in the historical eight-column CSV are cumulative counters.
PicoHarp *_points.jsonl supplies exact per-dwell counts and exposure times.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment
from scipy.stats import norm

STATES = ("S0", "S1", "S2", "S3")
# P(CH1 | fixed Alice, fixed Bob). Physical H/V/R/L names need optical checks.
TARGET = np.array([[0., .5], [.5, 0.], [1., .5], [.5, 1.]])


@dataclass
class Grid:
    path: Path
    points: pd.DataFrame
    alice: np.ndarray
    bob: np.ndarray
    counts: np.ndarray
    seconds: np.ndarray
    exact_counts: bool
    provenance: dict


def load_sweep(path, legacy_dwell_s=None):
    path = Path(path).resolve()
    if not path.name.endswith("_Detector_Traces.csv"):
        raise ValueError("Select a completed *_Detector_Traces.csv, not a partial acquisition.")
    df = pd.read_csv(path)
    columns = ["EOM0_Target_V", "EOM1_Target_V", "Raw_Count0", "Raw_Count1", "Rate0_Hz", "Rate1_Hz"]
    if not set(columns).issubset(df):
        raise ValueError("Missing voltage, rate or counter columns.")
    if not len(df) or not np.isfinite(df[columns].to_numpy(float)).all():
        raise ValueError("Empty sweep or nonfinite data.")
    if (df[["Raw_Count0", "Raw_Count1", "Rate0_Hz", "Rate1_Hz"]] < 0).any().any():
        raise ValueError("Negative counts/rates are invalid.")
    if (df[["EOM0_Target_V", "EOM1_Target_V"]].abs() > 200).any().any():
        raise ValueError("Recorded EOM target outside the supported ±200 V range.")
    prefix = path.name.removesuffix("_Detector_Traces.csv")
    meta_path, points_path = path.with_name(prefix + "_sweep.json"), path.with_name(prefix + "_points.jsonl")
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    exact = meta is not None and meta.get("schema") == "qkd-eom-picoharp-sweep-v1"
    if meta is not None and not exact:
        raise ValueError("Unrecognized sweep sidecar schema; inspect provenance before use.")
    if exact:
        if not meta.get("complete") or meta.get("status") != "completed" or meta.get("cleanup_errors"):
            raise ValueError("Sweep is incomplete or reports errors.")
        if meta.get("rows") != len(df) or meta.get("expected_rows") != len(df):
            raise ValueError("CSV row count differs from completed sweep manifest.")
        records = [json.loads(line) for line in points_path.read_text().splitlines() if line.strip()]
        if len(records) != len(df) or [r["step"] for r in records] != list(range(len(df))):
            raise ValueError("Per-point sidecar must match every CSV row in acquisition order.")
        counts = np.array([r["counts"] for r in records], dtype=float)
        seconds = np.array([r["seconds"] for r in records], dtype=float)
        bad = 0xda if meta["settings"]["mode"] == 2 else 0xde
        if any(r.get("flags", 0) & bad for r in records):
            raise ValueError("Sweep contains PicoHarp data-loss/error flags.")
        if counts.shape != (len(df), 2) or not np.isfinite(counts).all() or np.any(counts < 0) or np.any(counts != np.floor(counts)):
            raise ValueError("Invalid per-point detector counts.")
        if not np.isfinite(seconds).all() or np.any(seconds <= 0):
            raise ValueError("Invalid per-point exposure time.")
        if not np.allclose(counts/seconds[:, None], df[["Rate0_Hz", "Rate1_Hz"]], rtol=1e-5, atol=1e-5):
            raise ValueError("Sidecar counts/exposure do not reproduce CSV rates.")
        if not np.array_equal(np.cumsum(counts, axis=0), df[["Raw_Count0", "Raw_Count1"]].to_numpy()):
            raise ValueError("Cumulative CSV counters disagree with per-point counts.")
    else:
        if legacy_dwell_s is None or not np.isfinite(legacy_dwell_s) or legacy_dwell_s <= 0:
            raise ValueError("No PicoHarp point sidecar. Legacy NI counters include inter-point gaps: do NOT difference them. "
                             "Set LEGACY_DWELL_S explicitly for approximate exploratory calibration, or use a new PicoHarp sweep.")
        seconds = np.full(len(df), float(legacy_dwell_s))
        counts = np.rint(df[["Rate0_Hz", "Rate1_Hz"]].to_numpy() * seconds[:, None])
    df = df.copy()
    df["c1"], df["c2"], df["dwell_s"] = counts[:, 0], counts[:, 1], seconds
    grouped = df.groupby(["EOM1_Target_V", "EOM0_Target_V"])[["c1", "c2", "dwell_s"]].sum()
    alice = np.sort(df.EOM0_Target_V.unique())
    bob = np.sort(df.EOM1_Target_V.unique())
    index = pd.MultiIndex.from_product([bob, alice], names=grouped.index.names)
    if len(grouped) != len(index) or not grouped.index.isin(index).all():
        raise ValueError("Sweep grid has missing voltage pairs. Reacquire them; no silent interpolation.")
    ordered = grouped.reindex(index)
    if ordered.isna().any().any():
        raise ValueError("Incomplete voltage grid.")
    if len(alice) < 4 or len(bob) < 2:
        raise ValueError("Need at least four Alice and two Bob voltage values.")
    provenance = dict(csv=str(path), csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                      exact_counts=exact, source=meta["settings"].get("source") if exact else "legacy / unknown",
                      acquisition_settings=meta["settings"] if exact else None,
                      count_method="PicoHarp per-point sidecar" if exact else "Rounded rate × assumed dwell; approximate only",
                      legacy_assumed_dwell_s=None if exact else legacy_dwell_s)
    if exact:
        provenance.update(points_sha256=hashlib.sha256(points_path.read_bytes()).hexdigest(),
                          manifest_sha256=hashlib.sha256(meta_path.read_bytes()).hexdigest())
    return Grid(path, df, alice, bob,
                ordered[["c1", "c2"]].to_numpy(np.int64).reshape(len(bob), len(alice), 2),
                ordered.dwell_s.to_numpy().reshape(len(bob), len(alice)), exact, provenance)


def split_counts(grid, fraction=.7, seed=20260929):
    if not 0 < fraction < 1:
        raise ValueError("Training fraction must lie strictly between 0 and 1.")
    rng = np.random.default_rng(seed)
    train = rng.binomial(grid.counts, fraction)
    return train, grid.counts - train


def wilson(c1, c2, confidence=.95):
    c1, c2 = np.asarray(c1, dtype=float), np.asarray(c2, dtype=float)
    n = c1+c2
    p = np.divide(c1, n, out=np.full_like(n, np.nan), where=n > 0)
    z = norm.ppf((1+confidence)/2)
    safe = np.maximum(n, 1)
    center = (p + z*z/(2*safe)) / (1+z*z/safe)
    half = z*np.sqrt(p*(1-p)/safe + z*z/(4*safe*safe)) / (1+z*z/safe)
    return p, np.maximum(0, center-half), np.minimum(1, center+half)


def search(grid, train, alice_seeds, bob_seeds, alice_radius=35., bob_radius=35.,
           balance_tolerance=.05, extinction_tolerance=.15, min_counts=50, top=20, stop_event=None):
    """Joint six-voltage measured-grid search; four distinct Alice grid points.

    For each pair of Bob grid values, solve a rectangular assignment for four
    Alice roles. Loss is posterior expected squared error scaled by tolerance.
    Low-count pairs are excluded. No held-out counts enter ranking.
    """
    alice_seeds, bob_seeds = np.asarray(alice_seeds, float), np.asarray(bob_seeds, float)
    if alice_seeds.shape != (4,) or bob_seeds.shape != (2,) or not np.isfinite(np.r_[alice_seeds, bob_seeds]).all():
        raise ValueError("Provide four finite Alice seeds (S0,S1,S2,S3) and two Bob seeds.")
    if not (0 < balance_tolerance < .5 and 0 < extinction_tolerance < .5):
        raise ValueError("Tolerances must be between zero and 0.5.")
    if not np.isfinite([alice_radius, bob_radius]).all() or min(alice_radius, bob_radius) <= 0 or min_counts < 1:
        raise ValueError("Search radii and minimum counts must be positive.")
    if train.shape != grid.counts.shape or np.any(train < 0):
        raise ValueError("Invalid training counts.")
    allowed_a = np.abs(grid.alice[None, :] - alice_seeds[:, None]) <= alice_radius
    allowed_b = [np.flatnonzero(np.abs(grid.bob - seed) <= bob_radius) for seed in bob_seeds]
    if np.any(~allowed_a.any(axis=1)) or any(len(v) == 0 for v in allowed_b):
        raise ValueError("A search window contains no measured voltage. Widen its radius or check the seeds.")
    n = train.sum(axis=2)
    a, b = train[:, :, 0]+.5, train[:, :, 1]+.5
    mean = a/(a+b)
    variance = a*b/((a+b)**2 * (a+b+1))
    tolerances = np.where(TARGET == .5, balance_tolerance, extinction_tolerance)
    candidates = []
    for b0 in allowed_b[0]:
        for b1 in allowed_b[1]:
            if stop_event is not None and stop_event.is_set():
                raise KeyboardInterrupt
            if b0 == b1:
                continue
            pair = np.array([b0, b1])
            mu, var = mean[pair].T, variance[pair].T
            cell_loss = ((mu[None, :, :] - TARGET[:, None, :])**2 + var[None, :, :])/tolerances[:, None, :]**2
            loss = cell_loss.sum(axis=2)
            permitted = allowed_a & (n[pair].min(axis=0) >= min_counts)[None, :]
            point_feasible = np.all(np.abs(mu[None, :, :] - TARGET[:, None, :]) <= tolerances[:, None, :], axis=2)
            strict_loss = np.where(permitted & point_feasible, loss, 1e30)
            rr, aa = linear_sum_assignment(strict_loss)
            feasible = len(rr) == 4 and np.all(strict_loss[rr, aa] < 1e30)
            if not feasible:
                loss[~permitted] = 1e30
                rr, aa = linear_sum_assignment(loss)
            if len(rr) != 4 or np.any(loss[rr, aa] >= 1e30):
                continue
            candidates.append(dict(score=float(loss[rr, aa].sum()/8), alice_indices=aa.tolist(),
                                   bob_indices=pair.tolist(), alice_v=grid.alice[aa].tolist(), bob_v=grid.bob[pair].tolist(),
                                   training_point_feasible=bool(feasible)))
    if not candidates:
        raise ValueError("No feasible six-voltage set: widen search windows or collect more counts.")
    # Voltage magnitude only breaks otherwise equal scores; no phase wrapping.
    candidates.sort(key=lambda c: (not c["training_point_feasible"], c["score"], sum(v*v for v in c["alice_v"]+c["bob_v"])))
    return candidates[:top]


def evaluate(grid, candidate, counts, balance_tolerance=.05, extinction_tolerance=.15,
             simultaneous_confidence=.95):
    """Measured cell table with simultaneous Bonferroni-Wilson intervals.

    Applying this to held-out counts supports a check of a training-selected
    candidate. Applying it to full/training counts is descriptive only.
    """
    cell_confidence = 1 - (1-simultaneous_confidence)/8
    rows = []
    for s, ai in enumerate(candidate["alice_indices"]):
        for b, bi in enumerate(candidate["bob_indices"]):
            c1, c2 = counts[bi, ai]
            p, lo, hi = (float(v) for v in wilson(c1, c2, cell_confidence))
            target = TARGET[s, b]
            tol = balance_tolerance if target == .5 else extinction_tolerance
            acceptable = (max(0., target-tol), min(1., target+tol))
            rows.append(dict(state=STATES[s], bob_setting=b+1, alice_v=float(grid.alice[ai]),
                             bob_v=float(grid.bob[bi]), ch1_counts=int(c1), ch2_counts=int(c2),
                             total_counts=int(c1+c2), target_ch1=target, measured_ch1=p,
                             ci_low=lo, ci_high=hi, absolute_error=abs(p-target),
                             allowed_low=acceptable[0], allowed_high=acceptable[1],
                             point_within_tolerance=bool(acceptable[0] <= p <= acceptable[1]),
                             interval_within_tolerance=bool(lo >= acceptable[0] and hi <= acceptable[1]),
                             confidence_per_cell=cell_confidence))
    return pd.DataFrame(rows)


def match_candidate(grid, candidate):
    """Independent scans must have actually measured all six selected voltages."""
    def match(axis, requested):
        indices = []
        for value in requested:
            found = np.flatnonzero(np.isclose(axis, value, rtol=0, atol=1e-7))
            if len(found) != 1:
                raise ValueError(f"Independent sweep did not measure {value:g} V exactly; nearest-grid substitution is not validation.")
            indices.append(int(found[0]))
        return indices
    return dict(candidate, alice_indices=match(grid.alice, candidate["alice_v"]),
                bob_indices=match(grid.bob, candidate["bob_v"]))


def refinement_plan(grid, candidate, half_steps=1., subdivisions=4):
    """Offline suggestions, not measurements or a hardware-executable program."""
    da = float(np.median(np.diff(grid.alice))) / subdivisions
    db = float(np.median(np.diff(grid.bob))) / subdivisions
    rows = []
    for s, av in enumerate(candidate["alice_v"]):
        for b, bv in enumerate(candidate["bob_v"]):
            for variable, step in (("Alice", da), ("Bob", db)):
                for delta in np.arange(-subdivisions*half_steps, subdivisions*half_steps+1)*step:
                    a, v = (av+delta, bv) if variable == "Alice" else (av, bv+delta)
                    if -200 <= a <= 200 and -200 <= v <= 200:
                        rows.append(dict(state=STATES[s], bob_setting=b+1, varied=variable,
                                         alice_v=a, bob_v=v, target_ch1=TARGET[s, b]))
    return pd.DataFrame(rows).drop_duplicates(["state", "bob_setting", "alice_v", "bob_v"])
