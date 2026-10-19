"""Read-only history of independently fitted global EOM model parameters."""
import json
from pathlib import Path, PureWindowsPath
import re

import numpy as np
import pandas as pd

SCHEMA = 'qkd-eom-model-calibration-v2'


def timestamp(text):
    """Recognize this repository's UTC analysis/sweep names, never file mtime."""
    match = re.search(r'(\d{8}T\d{6}_\d{6}Z|\d{8}_\d{6}_\d{6})', str(text))
    if not match:
        return pd.NaT
    token = match.group(0)
    fmt = '%Y%m%dT%H%M%S_%fZ' if 'T' in token else '%Y%m%d_%H%M%S_%f'
    return pd.to_datetime(token, format=fmt, utc=True, errors='coerce')


def load_history(folder):
    """One row per completed global fit, with explicit reasons for exclusions.

    Fine-tuning Vpi values are inherited, not independently fitted observations.
    Older measured-grid analyses do not contain these model parameters.
    """
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        raise ValueError(f'Data directory does not exist: {folder}')
    rows, skipped = [], []
    for path in sorted(folder.rglob('analysis.json')):
        try:
            report = json.loads(path.read_text(encoding='utf-8-sig'))
            if report.get('schema') != SCHEMA:
                raise ValueError('Not a global physical-model fit (legacy, fine-tuning, or verification).')
            if report.get('status') != 'completed':
                raise ValueError('Analysis is not completed.')
            fit = report['fit']
            names, parameters = fit['parameter_names'], fit['parameters']
            if len(names) != len(parameters):
                raise ValueError('Parameter names and values have different lengths.')
            params = dict(zip(names, parameters))
            va, vb = float(fit['vpi_alice_v']), float(fit['vpi_bob_v'])
            phi, ka, kb = (float(params[k]) for k in ('phase_rad','k_alice_rad_per_v','k_bob_rad_per_v'))
            if not np.isfinite([va,vb,phi,ka,kb]).all() or min(va,vb,ka) <= 0 or kb == 0:
                raise ValueError('Missing/nonfinite/nonphysical fitted parameters.')
            when = timestamp(path.parent.name)
            if pd.isna(when):
                raise ValueError('Analysis folder has no recognized UTC timestamp; file modification time is not substituted.')
            source = report.get('source', {})
            csv = source.get('csv') or report.get('parameters', {}).get('csv', '')
            basename = PureWindowsPath(csv).name
            sweep_time = timestamp(basename)
            diagnostics = fit.get('diagnostics') or []
            identified = fit.get('fit_identified') is True
            good_model = fit.get('poisson_model_check_passed') is True
            # Content hashes identify reanalyses even when a CSV was moved.
            key = source.get('csv_sha256') or str(csv).replace('\\','/').casefold() or str(path)
            rows.append(dict(analysis_time_utc=when, sweep_time_utc=sweep_time,
                             vpi_alice_v=va, vpi_bob_v=vb, phase_rad=phi,
                             static_retardance_rad=2*phi, static_retardance_deg=float(np.degrees(2*phi)),
                             k_alice_rad_per_v=ka, k_bob_rad_per_v=kb,
                             fit_identified=identified, poisson_model_check_passed=good_model,
                             flagged=not identified or not good_model or bool(diagnostics),
                             diagnostics='; '.join(str(x) for x in diagnostics),
                             reduced_deviance=fit.get('reduced_deviance'),
                             source_csv=csv, sweep_id=basename.removesuffix('_Detector_Traces.csv'),
                             sweep_key=key, analysis_file=str(path.resolve())))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            skipped.append(dict(analysis_file=str(path), reason=str(exc)))
    table = pd.DataFrame(rows)
    if not table.empty:
        table = table.sort_values(['analysis_time_utc','analysis_file']).reset_index(drop=True)
        table['analyses_of_this_sweep'] = table.groupby('sweep_key')['sweep_key'].transform('size')
    return table, pd.DataFrame(skipped, columns=['analysis_file','reason'])


def select_history(table, time_axis='analysis', latest_per_sweep=False, only_validated=False):
    if table.empty:
        raise ValueError('No completed global physical-model analyses found. Check DATA_FOLDER or run EOM calibration analysis / Fit sweep first.')
    if time_axis not in ('analysis','sweep'):
        raise ValueError("TIME_AXIS must be 'analysis' or 'sweep'.")
    selected = table.copy()
    if latest_per_sweep:
        selected = selected.sort_values('analysis_time_utc').drop_duplicates('sweep_key', keep='last')
    if only_validated:
        selected = selected[~selected.flagged]
    column = time_axis+'_time_utc'
    missing = int(selected[column].isna().sum())
    selected = selected.dropna(subset=[column]).sort_values([column,'analysis_time_utc']).reset_index(drop=True)
    if selected.empty:
        raise ValueError('No results remain with these filters/time axis. Disable ONLY_VALIDATED or inspect missing timestamps.')
    selected['plot_time_utc'] = selected[column]
    return selected, missing


def plot_history(table, time_axis='analysis', timezone='America/New_York', phase_display='stored_phi_rad', dpi=150):
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    options = {'stored_phi_rad':('phase_rad', 'Combined static phase offset φ (rad)'),
               'retardance_rad':('static_retardance_rad', 'Combined retardance offset 2φ (rad)'),
               'retardance_deg':('static_retardance_deg', 'Combined retardance offset 2φ (degrees)')}
    if phase_display not in options:
        raise ValueError(f'PHASE_DISPLAY must be one of {list(options)}.')
    local_time = table.plot_time_utc.dt.tz_convert(timezone)
    fig, axes = plt.subplots(3,1,figsize=(11,8),sharex=True,layout='constrained',dpi=dpi)
    specs = [('vpi_alice_v','Alice / EOM0 — fitted Vπ (V)','#0072B2'),
             ('vpi_bob_v','Bob / EOM1 — fitted Vπ (V)','#D55E00'),
             (*options[phase_display], '#009E73')]
    for ax,(column,label,color) in zip(axes,specs):
        y = table[column].to_numpy()
        # Break the connecting line at phase wraps or Bob-slope branch changes.
        line = y.copy()
        if column in ('phase_rad','static_retardance_rad','static_retardance_deg'):
            period = np.pi if column == 'phase_rad' else 2*np.pi if column == 'static_retardance_rad' else 360
            jumps = np.abs(np.diff(y)) > period/2
            branches = np.diff(np.sign(table.k_bob_rad_per_v.to_numpy())) != 0
            line[np.r_[False,jumps|branches]] = np.nan
        ax.plot(local_time,line,color=color,lw=1.25)
        ax.scatter(local_time,y,color=color,s=36,label='Saved global fit',zorder=3)
        flagged = table.flagged.to_numpy()
        if flagged.any():
            ax.scatter(local_time[flagged],y[flagged],marker='x',color='#B2182B',s=75,
                       linewidths=1.4,label='Fit diagnostic / model check not passed',zorder=4)
        ax.set(ylabel=label)
        ax.grid(alpha=.22)
        ax.ticklabel_format(axis='y',style='plain',useOffset=False)
    locator = mdates.AutoDateLocator(tz=local_time.dt.tz, minticks=3, maxticks=8)
    axes[-1].xaxis.set_major_locator(locator)
    axes[-1].xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator,tz=local_time.dt.tz))
    axes[-1].set_xlabel(('Analysis time' if time_axis == 'analysis' else 'Sweep folder creation time')+f' ({timezone})')
    axes[0].legend(loc='best',fontsize=8)
    fig.suptitle(f'EOM calibration parameter history — {len(table)} analyses, {table.sweep_key.nunique()} sweeps')
    return fig
