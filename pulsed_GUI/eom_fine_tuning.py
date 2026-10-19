"""Single-EOM compensator sweep, conditional phase fit and fresh pair check."""
import hashlib
import json
import math
from pathlib import Path
import threading

import numpy as np

import eom_voltage_sweep as sweep
import eom_fine_model as model
from eom_calibration import new_output
from eom_calibration_core import TARGET, load_sweep
from eom_calibration_verify import exact_interval
from eom_model_calibration import check_stop, prediction_table
from calibration_transfer import payload
from record_io import save_json
from figure_options import validated_dpi

TOOL = 'EOM calibration fine-tuning'
RUN, VIEW = 'Run fine-tuning + verification', 'Analyze saved result'
REFIT = 'Refit saved data (curve_fit)'
SCHEMA = 'qkd-eom-fine-tuning-v2'
SAVED_SCHEMAS = ('qkd-eom-fine-tuning-v1', SCHEMA)
EOMS = {'Bob (AO1)': 'bob', 'Alice (AO0)': 'alice'}
PAIRS = {f'S{i} / Bob {j+1} — CH1 target {TARGET[i,j]:g}': (i,j) for i in range(4) for j in range(2)}
HELP = ('Choose a completed global calibration analysis.json and one voltage pair (S indices retain the original calibration roles). '
        'The rejected-candidate override allows a previously rejected voltage set with an identified fit; voltage limits and fresh result checks still apply. '
        'Sweep one EOM while holding the other at the selected pair voltage. Use curve_fit seeded from the prior calibration to refit phase and both detector amplitudes/backgrounds, keeping Vpi fixed; '
        'translate only the swept EOM\'s settings to compensate the combined drift, then independently count at the optimized pair. '
        'Steps EACH SIDE of the center: 5 gives 11 points, rather than 121. Use the saved time for longer dwell. '
        'Near an extinction point, use enough span/dwell to resolve curvature. Both outputs return to zero between stages and on exit. '
        'One pair cannot separately identify drift of each EOM or verify all eight combinations. '
        'Keep the optical configuration unchanged from the global sweep. No raw TTTR files are saved. '
        'Analyze saved result redraws the frozen fit. Refit saved data applies curve_fit offline; new candidates require fresh verification.')
FORM = [
    ('action', 'Action', 'Check settings', ['Check settings', RUN, VIEW, REFIT]),
    ('fit-report', 'Previous global calibration analysis.json (contains voltages, Vpi and phase)', '', 'file'),
    ('allow-rejected-prior', 'Override: allow rejected global voltage candidate', 'No', ['No', 'Yes']),
    ('pair', 'Pair to refine — target is CH1 / (CH1 + CH2)', list(PAIRS)[1], list(PAIRS)),
    ('sweep-eom', 'EOM to sweep and use as compensator (other EOM held fixed)', next(iter(EOMS)), list(EOMS)),
    ('sweep-step', 'Voltage step on swept EOM (target V)', '2', None),
    ('sweep-half-steps', 'Steps each side of center (5 → ±10 V at 2 V steps; 11 points)', '5', None),
    ('voltage-limit', 'Maximum absolute target voltage on either EOM (V)', '180', None),
    ('verify-seconds', 'Fresh verification at optimized pair (s)', '30', None),
    ('confidence', 'Single-pair verification confidence', '0.95', None),
    *[(k, label, '10' if k == 'seconds' else value, choices) for k,label,value,choices in sweep.FORM
      if k not in ('action', 'points', 'output')],
    ('output', 'Fine-tuning runs directory (one folder per run)', str(sweep.DATA/'fine_tuning'), 'directory'),
    ('folder', 'Analyze: saved fine-tuning run directory', '', 'directory'),
]


def settings(values):
    action = values.get('action', 'Check settings')
    if action == 'View saved result':
        action = VIEW
    if action not in ('Check settings', RUN, VIEW, REFIT):
        raise ValueError('Select a fine-tuning action.')
    if action in (VIEW, REFIT):
        folder = Path(values.get('folder', ''))
        if not values.get('folder', '').strip() or not (folder/'analysis.json').is_file():
            raise ValueError('Select a saved fine-tuning run folder.')
        return dict(action=action, folder=str(folder.resolve()),
                    plot_dpi=validated_dpi(values.get('plot-dpi', '300')))
    override = values.get('allow-rejected-prior', 'No')
    if override not in ('No', 'Yes'):
        raise ValueError('Select Yes or No for the rejected-candidate override.')
    prior, source = model.load_prior(values.get('fit-report', ''), allow_rejected=override == 'Yes')
    def number(key, lo, hi):
        value = float(values[key])
        if not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f'{key} must be between {lo:g} and {hi:g}.')
        return value
    half = int(values['sweep-half-steps'])
    if not 2 <= half <= 100:
        raise ValueError('Use 2–100 steps each side of center (5–201 points on the swept EOM).')
    if values['pair'] not in PAIRS:
        raise ValueError('Select one of the eight calibration pairs.')
    i,j = PAIRS[values['pair']]
    if values['sweep-eom'] not in EOMS:
        raise ValueError('Select Alice or Bob as the swept EOM / compensator.')
    sweep_eom = EOMS[values['sweep-eom']]
    step, limit = number('sweep-step', .001, 100), number('voltage-limit', .001, 199.999)
    axes = model.scan_axes(prior, i, j, step, half, limit, sweep_eom)
    cfg = sweep.settings(dict(values, points=str(2*half+1)))
    verify_s = number('verify-seconds', .001, 3600)
    if not math.isclose(verify_s*1000, round(verify_s*1000), abs_tol=1e-6):
        raise ValueError('Verification duration must be a whole number of milliseconds.')
    cfg.update(axes)
    cfg.update(action=action, prior=prior, prior_source=source, pair_indices=[i,j],
               allow_rejected_prior=override == 'Yes',
               sweep_eom=sweep_eom, sweep_step_v=step, sweep_half_steps=half, voltage_limit=limit,
               scan_points=2*half+1,
               minimum_scan_seconds=(2*half+1)*(cfg['duration_ms']/1000+cfg['settle_s']),
               verify_duration_ms=round(verify_s*1000), confidence=number('confidence', .5, .99999))
    return cfg


def acquire(cfg, folder, axes, duration_ms, stop, runner):
    check_stop(stop)
    acquisition = {k:v for k,v in cfg.items() if k not in ('prior', 'prior_source')}
    acquisition.update(axes, output=str(folder), duration_ms=duration_ms,
                       calibration_stage=Path(folder).name)
    acquisition['scan_points'] = len(acquisition['alice_axis'])*len(acquisition['bob_axis'])
    acquisition['points'] = max(len(acquisition['alice_axis']), len(acquisition['bob_axis']))
    acquisition['minimum_scan_seconds'] = acquisition['scan_points']*(duration_ms/1000+cfg['settle_s'])
    result = {}
    code = runner(acquisition, stop, completed_result=result)
    if code == 130:
        raise KeyboardInterrupt
    if code != 0 or not result.get('csv'):
        raise RuntimeError('Acquisition failed or was not finalized; see saved sweep manifest. No later stage was started.')
    return result


def verification_result(result, selected, prior, i, j, confidence):
    manifest = json.loads(Path(result['manifest']).read_text())
    if not manifest.get('complete') or manifest.get('cleanup_errors') or manifest.get('rows') != 1:
        raise ValueError('Independent verification did not complete exactly one voltage pair.')
    rows = [json.loads(line) for line in Path(result['points']).read_text().splitlines() if line.strip()]
    if len(rows) != 1:
        raise ValueError('Expected one independent verification dwell.')
    row = rows[0]
    counts = row['counts']
    if (len(counts) != 2 or any(type(n) is not int or n < 0 for n in counts)
            or not math.isfinite(row['seconds']) or row['seconds'] <= 0):
        raise ValueError('Invalid verification counts or exposure.')
    # Verify the acquisition manifest, not merely the requested configuration.
    for role, voltage in (('alice', selected['alice_v'][i]), ('bob', selected['bob_v'][j])):
        if manifest['settings'][role+'_axis'] != [voltage]:
            raise ValueError('Verification voltages differ from the frozen candidate.')
    bad = 0xda if manifest['settings']['mode'] == 2 else 0xde
    if row.get('flags', 0) & bad:
        raise ValueError('Verification contains PicoHarp error/data-loss flags.')
    p, lo, hi = exact_interval(*counts, confidence)
    target = float(TARGET[i,j])
    tolerance = prior['parameters']['balance_tolerance' if target == .5 else 'extinction_tolerance']
    low, high = max(0,target-tolerance), min(1,target+tolerance)
    passed = bool(sum(counts) and lo >= low and hi <= high)
    return dict(alice_index=i, bob_index=j, alice_v=selected['alice_v'][i], bob_v=selected['bob_v'][j],
                counts=counts, seconds=row['seconds'], rates_hz=(np.asarray(counts)/row['seconds']).tolist(),
                target_ch1=target, predicted_ch1=selected['predicted_ch1'][i][j],
                measured_ch1=p if sum(counts) else None, ci_low=lo, ci_high=hi, confidence=confidence,
                allowed_low=low, allowed_high=high, passed=passed,
                method='Fresh ungated photon counts; exact conditional-binomial Clopper-Pearson interval wholly inside target tolerance',
                scope='Only this voltage pair. Other seven combinations and physical state labels unverified.',
                files=result,
                file_sha256={k:hashlib.sha256(Path(v).read_bytes()).hexdigest() for k,v in result.items()})


def run(cfg, stop, runner=None):
    check_stop(stop)
    runner = sweep.run if runner is None else runner
    output = new_output(cfg['output'])
    report_path = output/'analysis.json'
    report = dict(schema=SCHEMA, status='preparing', transfer_available=False,
                  parameters={k:v for k,v in cfg.items() if k != 'prior'}, prior_source=cfg['prior_source'],
                  independent_count_check_passed=False, physical_state_labels_confirmed=False,
                  calibration_target='Ungated detected fractions; no background subtraction or efficiency correction',
                  helper_sha256=hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest())
    save_json(output/'prior_analysis.json', cfg['prior'])
    save_json(report_path, report)
    print(f'Fine-tuning folder: {output}', flush=True)
    i,j = cfg['pair_indices']
    prior, axes = cfg['prior'], {r+'_axis':cfg[r+'_axis'] for r in ('alice','bob')}
    try:
        report['status'] = 'local_sweep'; save_json(report_path, report)
        scan = acquire(cfg, output/'local_sweep', axes, cfg['duration_ms'], stop, runner)
        report['local_sweep'] = scan
        grid = load_sweep(scan['csv'], single_eom=cfg['sweep_eom'])
        report['source'] = grid.provenance
        report['status'] = 'fitting'; save_json(report_path, report)
        fit = report['fit'] = model.fit_local(prior, grid, stop)
        save_json(report_path, report)
        from eom_fine_figures import render
        render(grid, report, output, cfg['plot_dpi'], stop)
        print(f"Preview image: {output/'fine_tuning.png'}", flush=True)
        if not fit['usable']:
            raise ValueError('Local fit not accepted: ' + ' '.join(fit['diagnostics']))
        selected = report['selected'] = model.refine_voltages(prior, fit, axes, i, j, cfg['voltage_limit'], cfg['sweep_eom'])
        prediction_table(selected, prior['parameters']['balance_tolerance'], prior['parameters']['extinction_tolerance']).to_csv(output/'predicted_fractions.csv', index=False)
        report['status'] = 'verifying'; save_json(report_path, report)
        print(f"Frozen candidate S{i}/Bob {j+1}: Alice {selected['alice_v'][i]:+.6f} V, Bob {selected['bob_v'][j]:+.6f} V. Starting fresh verification.", flush=True)
        verification = acquire(cfg, output/'verification', dict(alice_axis=[selected['alice_v'][i]], bob_axis=[selected['bob_v'][j]]),
                               cfg['verify_duration_ms'], stop, runner)
        report['verification'] = verification_result(verification, selected, prior, i, j, cfg['confidence'])
        check_stop(stop)
        report.update(status='completed', transfer_available=bool(selected['predicted_targets_pass'] and report['verification']['passed']))
        save_json(report_path, report)
        render(grid, report, output, cfg['plot_dpi'], stop)
        if report['transfer_available']:
            transfer = payload(selected, scan['csv'])
            transfer.pop('heldout_count_check_passed')
            transfer.update(method='local-curve-fit-v1', analysis_report=str(report_path),
                            compensation_eom=cfg['sweep_eom'],
                            alice_common_shift_v=selected['alice_common_shift_v'],
                            bob_common_shift_v=selected['bob_common_shift_v'],
                            parent_analysis_report=cfg['prior_source']['path'],
                            parent_candidate_accepted=cfg['prior_source']['candidate_accepted'],
                            parent_rejection_reason=cfg['prior_source']['rejection_reason'],
                            rejected_candidate_override=cfg['prior_source']['rejected_candidate_override'],
                            operating_bounds=selected['operating_bounds'], predicted_targets_passed=True,
                            curve_fit_model_check_passed=True, fit_diagnostics=fit['diagnostics'],
                            single_pair_check_passed=True, verification_scope=report['verification']['scope'],
                            status='Selected pair verified; other seven combinations and physical labels require verification')
            save_json(output/'calibration_for_app.json', transfer)
            print(f"Calibration transfer: {output/'calibration_for_app.json'}", flush=True)
        v = report['verification']
        print(f"Selected-pair verification: {'PASS' if v['passed'] else 'NOT PASSED'}; counts {v['counts']}, "
              f"CH1 fraction {v['measured_ch1']}, {v['confidence']:.1%} interval [{v['ci_low']:.4f}, {v['ci_high']:.4f}].", flush=True)
        print(f"Alice voltages: {selected['alice_v']}\nBob voltages: {selected['bob_v']}", flush=True)
        if not report['transfer_available']:
            print('No app transfer exported: selected-pair verification or eight predicted targets did not pass. Inspect the report; zero/low counts can leave verification inconclusive.', flush=True)
        print('Remaining combinations are predictions. Use a full polarization matrix to verify them before BB84.', flush=True)
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'error', error=str(exc), transfer_available=False)
        save_json(report_path, report)
        raise
    finally:
        print(f'Calibration result: {report_path}', flush=True)
        print(f'Run record: {output / "analysis.json"}', flush=True)
    print(f"Preview image: {output/'fine_tuning.png'}", flush=True)
    return output


def load_saved(cfg, stop=None):
    """Load the original sweep, preserving relocation and integrity checks."""
    check_stop(stop)
    folder = Path(cfg['folder']).resolve()
    source = folder/'analysis.json'
    report = json.loads(source.read_text(encoding='utf-8-sig'))
    if report.get('schema') not in SAVED_SCHEMAS or not report.get('fit'):
        raise ValueError('Select a fine-tuning run with a saved local fit. Failed fit checks can still be plotted.')
    saved_csv = report.get('local_sweep', {}).get('csv')
    if not saved_csv:
        raise ValueError('This run has no completed local sweep to plot.')
    # Prefer files beside the report, so copied/moved run folders stay portable.
    candidates = list((folder/'local_sweep').rglob(Path(saved_csv).name))
    if len(candidates) > 1:
        raise ValueError('Multiple matching local sweep CSVs found; select an unambiguous run folder.')
    csv_path = candidates[0] if candidates else Path(saved_csv)
    sweep_eom = report['parameters'].get('sweep_eom') if report['schema'] == SCHEMA else None
    if report['schema'] == SCHEMA and sweep_eom not in ('alice', 'bob'):
        raise ValueError('Saved single-EOM run is missing its swept EOM.')
    grid = load_sweep(csv_path, single_eom=sweep_eom)
    expected = report.get('source', {}).get('csv_sha256')
    if expected and grid.provenance['csv_sha256'] != expected:
        raise ValueError('Saved sweep CSV differs from the data used for this fit.')
    check_stop(stop)
    return folder, source, report, grid, sweep_eom


def analyze_saved(cfg, stop=None):
    """Regenerate display products without changing the frozen calibration/check."""
    folder, source, report, grid, sweep_eom = load_saved(cfg, stop)
    output = new_output(folder/'analysis')
    from eom_fine_figures import render
    render(grid, report, output, cfg['plot_dpi'], stop)
    save_json(output/'plot_record.json', dict(schema='qkd-eom-fine-tuning-plot-v1',status='completed',
              source_report=str(source), source_report_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
              source=grid.provenance, plot_dpi=cfg['plot_dpi'],
              operation='Redraw saved counts and frozen fit; no refit, acquisition, or calibration transfer',
              display='Single-EOM voltage response curves' if sweep_eom else 'Legacy 2D mesh; measured fraction color scale'))
    print(f"Saved run status: {report.get('status')}; {report.get('error') or 'no saved error'}. Fit and verification unchanged.", flush=True)
    print(f'Run record: {source}', flush=True)
    print(f"Preview image: {output/'fine_tuning.png'}", flush=True)
    print(f'Redrawn plots saved: {output}', flush=True)
    return output


def refit_saved(cfg, stop=None):
    """Fit saved counts anew; never reuse verification at an old candidate."""
    folder, source, report, grid, sweep_eom = load_saved(cfg, stop)
    prior_path = folder/'prior_analysis.json'
    prior, _ = model.load_prior(prior_path, allow_rejected=bool(
        report.get('prior_source', {}).get('rejected_candidate_override')))
    # Use the frozen prior that actually seeded this run, not a newer calibration.
    fit = model.fit_local(prior, grid, stop)
    for key in ('selected', 'verification', 'error'):
        report.pop(key, None)
    report.update(fit=fit, status='refit_only', transfer_available=False,
                  independent_count_check_passed=False,
                  source_report=str(source),
                  source_report_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  refit_prior_sha256=hashlib.sha256(prior_path.read_bytes()).hexdigest(),
                  operation='Offline curve_fit refit; no fresh verification or calibration transfer')
    if fit['usable'] and sweep_eom:
        params = report['parameters']
        axes = dict(alice_axis=grid.alice.tolist(), bob_axis=grid.bob.tolist())
        try:
            report['selected'] = model.refine_voltages(prior, fit, axes,
                *params['pair_indices'], params['voltage_limit'], sweep_eom)
        except ValueError as exc:
            report['candidate_error'] = str(exc)
    elif not sweep_eom:
        report['candidate_error'] = 'Legacy two-EOM scan refitted; no single-EOM compensation assumed.'
    check_stop(stop)
    output = new_output(folder/'analysis')
    save_json(output/'refit_report.json', report)
    if report.get('selected'):
        prediction_table(report['selected'], prior['parameters']['balance_tolerance'],
                         prior['parameters']['extinction_tolerance']).to_csv(output/'predicted_fractions.csv', index=False)
    from eom_fine_figures import render
    render(grid, report, output, cfg['plot_dpi'], stop)
    print('Offline refit complete. Fresh verification has NOT been performed; no app transfer exported.', flush=True)
    for message in fit['diagnostics'] + ([report['candidate_error']] if report.get('candidate_error') else []):
        print(message, flush=True)
    print(f"Refit report: {output/'refit_report.json'}", flush=True)
    print(f'Run record: {source}', flush=True)
    print(f"Preview image: {output/'fine_tuning.png'}", flush=True)
    return output


def main(values, stop_event=None):
    stop = stop_event if stop_event is not None else threading.Event()
    try:
        check_stop(stop)
        cfg = settings(values)
        if cfg['action'] in (VIEW, REFIT):
            (refit_saved if cfg['action'] == REFIT else analyze_saved)(cfg, stop)
            return 0
        i,j = cfg['pair_indices']
        if cfg['prior_source']['rejected_candidate_override']:
            print('Rejected global candidate override enabled: ' + str(cfg['prior_source']['rejection_reason']) +
                  ' Fine-tuning fit, predicted-target and fresh verification checks still apply.', flush=True)
        p = cfg['prior']['fit']['parameters']
        print(f"Prior Vpi: Alice {np.pi/(2*abs(p[0])):.3f} V; Bob {np.pi/(2*abs(p[1])):.3f} V; phase {p[2]:.6f} rad. "
              f"S{i}/Bob {j+1}, CH1 target {TARGET[i,j]:g}.", flush=True)
        swept = cfg['sweep_eom']; fixed = 'bob' if swept == 'alice' else 'alice'
        print(f"{cfg['scan_points']}-point {swept.title()} scan: {cfg[swept+'_axis'][0]:.3f}…{cfg[swept+'_axis'][-1]:.3f} V; "
              f"{fixed.title()} held at {cfg[fixed+'_axis'][0]:.3f} V. "
              f"{cfg['duration_ms']/1000:g} s per point. "
              f"At least {(cfg['minimum_scan_seconds']+cfg['verify_duration_ms']/1000+cfg['settle_s'])/60:.1f} minutes plus setup/FIFO overhead.", flush=True)
        if cfg['action'] == 'Check settings':
            print('Settings valid; no hardware accessed. Prior half-wave voltages are retained; detector amplitudes and backgrounds are refitted.')
            return 0
        if cfg['source'] != sweep.NI and values['manual-ready'] != 'Yes':
            raise ValueError('Disconnect PFI12 and select the requested laser mode, then confirm Manual mode in the Run tab.')
        run(cfg, stop)
        return 0
    except KeyboardInterrupt:
        print('Fine-tuning stopped; partial results retained.', flush=True)
        return 130
    except Exception as exc:
        print(f'Fine-tuning: {exc}', flush=True)
        return 1


if __name__ == '__main__':
    from gui_app import launch
    launch(TOOL)
