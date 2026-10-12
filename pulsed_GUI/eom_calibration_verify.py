"""Independent verification of a frozen model-derived candidate."""
import hashlib
import itertools
import json
from pathlib import Path
import numpy as np
import pandas as pd
from scipy.stats import beta
from eom_calibration_core import TARGET, STATES
from eom_model_calibration import check_stop, prediction_table
from record_io import save_json


def exact_interval(c1, c2, confidence):
    """Two-sided Clopper-Pearson interval, conditional on total counts."""
    n, alpha = c1+c2, 1-confidence
    if not n:
        return float('nan'), 0., 1.
    return (c1/n, float(beta.ppf(alpha/2, c1, c2+1)) if c1 else 0.,
            float(beta.ppf(1-alpha/2, c1+1, c2)) if c2 else 1.)


def verify_rows(selected, rows, balance, extinction, confidence):
    """Match physical voltage pairs and detector numbers, never display labels."""
    table = prediction_table(selected, balance, extinction)
    if len(rows) != 16:
        raise ValueError("Verification requires exactly eight settings with both detectors.")
    used, measurements = set(), []
    for i, j in itertools.product(range(4), range(2)):
        pair = []
        for channel in ('CH1', 'CH2'):
            matches = [(ridx, r) for ridx, r in enumerate(rows)
                       if r['detector'] == channel
                       and abs(r['alice_target_v']-selected['alice_v'][i]) <= 1e-7
                       and abs(r['bob_target_v']-selected['bob_v'][j]) <= 1e-7]
            if len(matches) != 1 or matches[0][0] in used:
                raise ValueError(f"Verification voltages do not match S{i}/Bob {j+1}/{channel} exactly. "
                                 "Load the fitted calibration and record a fresh eight-setting polarization run; "
                                 "nearby grid points do not verify interpolated voltages.")
            idx, row = matches[0]
            used.add(idx)
            pair.append(row)
        if pair[0]['exposure_s'] != pair[1]['exposure_s'] or pair[0]['raw_folder'] != pair[1]['raw_folder']:
            raise ValueError("The two detectors must share one acquisition per voltage pair.")
        c1, c2 = (r['ungated_counts'] for r in pair)
        if any(type(c) is not int or c < 0 for c in (c1, c2)):
            raise ValueError("Verification requires integer photon counts.")
        p, lo, hi = exact_interval(c1, c2, 1-(1-confidence)/8)
        t = TARGET[i,j]
        tolerance = balance if t == .5 else extinction
        measurements.append(dict(ch1_counts=c1, ch2_counts=c2, exposure_s=pair[0]['exposure_s'],
                                 measured_ch1=p, ci_low=lo, ci_high=hi,
                                 interval_within_tolerance=bool(c1+c2 and lo >= max(0,t-tolerance) and hi <= min(1,t+tolerance))))
    result = pd.concat([table, pd.DataFrame(measurements)], axis=1)
    result['measurement_status'] = 'Independent measurement at the exact selected voltage pair; ungated'
    return result


def file_hash(path, stop):
    digest = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1048576), b''):
            check_stop(stop)
            digest.update(block)
    return digest.hexdigest()


def verify(cfg, stop=None):
    from eom_calibration import SCHEMA, new_output, model_transfer
    from polarization_analysis import summarize
    check_stop(stop)
    original_path = Path(cfg['fit_report']).resolve()
    original = json.loads(original_path.read_text(encoding='utf-8-sig'))
    if original.get('schema') != SCHEMA or original.get('status') != 'completed' or not original.get('selected'):
        raise ValueError('Select the completed physical-fit analysis.json with a fitted voltage candidate.')
    folder = Path(cfg['verification_folder']).resolve()
    session = json.loads((folder/'session.json').read_text(encoding='utf-8'))
    if not session.get('complete') or session.get('status') != 'completed' or session.get('cleanup_errors'):
        raise ValueError('Verification requires a complete polarization run without cleanup errors.')
    print(f'Reading independent polarization data: {folder}', flush=True)
    result = summarize(folder, gate=None, stop_event=stop)
    params = original['parameters']
    table = verify_rows(original['selected'], result['rows'], params['balance_tolerance'],
                        params['extinction_tolerance'], cfg['confidence'])
    passed = bool(table.interval_within_tolerance.all())
    source_files = {'session.json': file_hash(folder/'session.json', stop)}
    for raw in sorted({r['raw_folder'] for r in result['rows']}):
        for name in ('metadata.json','events.t3raw'):
            source_files[f'{raw}/{name}'] = file_hash(folder/raw/name, stop)
    output = new_output(cfg['output'] or folder/'analysis'/'calibration_verification')
    report = dict(schema='qkd-eom-calibration-verification-v2', status='writing',
                  original_fit=str(original_path), original_fit_sha256=file_hash(original_path, stop),
                  selected=original['selected'], verification_parameters=cfg,
                  fit_acquisition_settings=original['source']['acquisition_settings'],
                  verification_acquisition_settings=session['settings'], verification_source=str(folder),
                  verification_file_hashes=source_files, independent_count_check_passed=passed,
                  confidence_method='Exact conditional-binomial Clopper-Pearson intervals; Bonferroni across eight pairs',
                  confidence_note='Coverage assumes stationary independent photon detection. Does not include drift or voltage errors.',
                  gate=None, calibration_target=original['calibration_target'],
                  checks=json.loads(table.to_json(orient='records')))
    report_path = output/'analysis.json'
    save_json(report_path, report)
    try:
        table.to_csv(output/'independent_checks.csv', index=False)
        from eom_calibration_figures import fractions_plot
        fractions_plot(original['selected'], table, output, cfg['plot_dpi'], lambda: check_stop(stop), verification=True)
        check_stop(stop)
        if original['transfer_available']:
            transfer = model_transfer(original, original_path)
            transfer.update(independent_count_check_passed=passed, verification_report=str(report_path),
                            status='Independent fraction checks PASS; physical labels unverified' if passed else
                                   'Independent fraction checks NOT PASSED; inspect verification report')
            save_json(output/'calibration_for_app.json', transfer)
        report['status'] = 'completed'
        save_json(report_path, report)
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'error', error=str(exc))
        save_json(report_path, report)
        raise
    print(table[['state','bob_setting','predicted_ch1','measured_ch1','ci_low','ci_high','interval_within_tolerance']].to_string(index=False), flush=True)
    print(f"Independent fraction checks: {'PASS' if passed else 'NOT PASSED'}. Voltages were not refitted. "
          'This check applies to the saved verification conditions; physical labels remain unverified.', flush=True)
    print(f'Preview image: {(output/"verification.png").resolve()}', flush=True)
    print(f'Run record: {report_path}', flush=True)
    if original['transfer_available']:
        print(f'Calibration transfer: {(output/"calibration_for_app.json").resolve()}', flush=True)
    return output
