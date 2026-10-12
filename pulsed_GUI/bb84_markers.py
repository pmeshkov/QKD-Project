"""AO-clocked Marker 1 schedule and strict offline T3 reference checks.

Marker timestamps alone do not determine their offset from the corresponding
laser pulse. That integer offset must be commissioned on the actual wiring.
"""
import numpy as np

OFF = "Off (breakout not connected)"
COMMISSION = "Record markers (commissioning)"
CALIBRATED = "Record markers (offset verified on bench)"
MODES = [OFF, COMMISSION, CALIBRATED]


def make_plan(cfg, plan):
    mode = cfg.get("mode", OFF)
    if mode not in MODES:
        raise ValueError("Select a valid timing marker mode.")
    if mode == OFF:
        return dict(enabled=False, mode=mode)
    warmup, trials = plan["warmup_count"], plan["trial_count"]
    if warmup < 8 or trials < 16 or plan["realized_rate_hz"] > 1e6:
        raise ValueError("Markers require at least 8 warm-up pulses, 16 trials and rate <= 1 MHz.")
    interval = max(8, round(plan["realized_rate_hz"] * .1))
    end = warmup + trials - 1
    indices = [warmup-4, warmup]
    indices += list(range(warmup+interval, end-1, interval))
    # Always retain an interior checkpoint, even for short commissioning runs.
    if len(indices) == 2:
        indices.append(warmup + trials//2)
    indices.append(end)
    delta = cfg.get("pulse_sync_minus_marker_sync")
    note = str(cfg.get("verification_note", "")).strip()
    if mode == CALIBRATED:
        if type(delta) is not int or abs(delta) > 100 or not note:
            raise ValueError("Enter the bench-measured pulse-SYNC minus marker-SYNC offset and verification evidence.")
    else:
        delta = None  # Never promote a stale GUI value during commissioning.
    return dict(enabled=True, mode=mode, line=f"{plan['device']}/port0/line0",
                clock=f"/{plan['device']}/ao/SampleClock", edge="rising",
                marker_mask=1, high_samples=1, sample_indices=indices,
                interval_samples=interval, pulse_sync_minus_marker_sync=delta,
                verification_note=note if mode == CALIBRATED else "",
                reference=cfg.get('reference'),
                calibration_status=cfg.get('calibration_status','Legacy marker configuration'),
                analyzed_trial_count=trials-1,
                note="Last trial is an end-reference guard and excluded from indexed analysis. "
                     "Exact checkpoint spacing detects net SYNC-count slips, not compensating errors between checkpoints.")


def samples(schedule, start, stop):
    # USB-6351 DAQmx port writes require uint32 even when only P0.0 is used.
    # Only bit 0 carries the marker; the task still reserves just port0/line0.
    result = np.zeros(stop-start, dtype=np.uint32)
    positions = np.asarray(schedule["sample_indices"], dtype=np.int64)
    positions = positions[(positions >= start) & (positions < stop)]
    result[positions-start] = 1
    return result


def validate_observed(schedule, masks, cycles, warmup):
    expected = np.asarray(schedule["sample_indices"], dtype=np.int64)
    observed = np.asarray(cycles, dtype=np.int64)
    if len(expected) < 4 or np.any(np.diff(expected) < 2):
        raise ValueError("Invalid marker schedule; require separated start/interior/end references.")
    if len(observed) != len(expected) or np.any(np.asarray(masks) != 1):
        raise ValueError(f"Marker integrity failed: expected {len(expected)} Marker 1 edges; "
                         f"received {len(observed)} records. Check wiring/edge, missing or extra markers.")
    offsets = observed - expected
    if not np.all(offsets == offsets[0]):
        raise ValueError("Marker spacing does not match NI sample spacing exactly. "
                         "Possible missed SYNC, marker jitter at a SYNC boundary, or streaming/timing fault.")
    delta = schedule["pulse_sync_minus_marker_sync"]
    calibrated = schedule["mode"] == CALIBRATED and type(delta) is int and bool(schedule["verification_note"].strip())
    return dict(status="bench_referenced_checkpoints_passed" if calibrated else "checkpoints_passed_offset_unverified",
                observed_sync_indices=observed.tolist(), ni_sample_indices=expected.tolist(),
                marker_sync_minus_sample_index=int(offsets[0]),
                pulse_sync_minus_marker_sync=delta,
                origin=int(offsets[0])+warmup+delta if calibrated else None,
                verification_note=schedule["verification_note"],
                reference=schedule.get('reference'),
                limitation="Exact net pulse counts at checkpoints; not proof against compensating errors within a block.")
