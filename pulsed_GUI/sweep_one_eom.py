"""Full-range single-EOM acquisition; counts only, with no analysis."""
import math
import threading

import numpy as np

import eom_voltage_sweep as sweep

TOOL = 'Sweep one EOM'
EOMS = {'Alice (AO0)': 'alice', 'Bob (AO1)': 'bob'}
HELP = ('Select the EOM to sweep; the other holds the fixed target voltage. '
        'Evenly spaced voltage points include both -200 and +200 V (spacing 400/(points-1) V). '
        'Both detectors are counted simultaneously at each voltage; settling is excluded from dwell. '
        'CSV contains per-dwell integer counts, actual dwell time and both target voltages. '
        'Repeat sweeps in one CSV; repeated runs include Sweep_Number. Between sweeps, '
        'return from +200 to -200 V and wait the specified dead time before counting resumes. '
        'No fitting, analysis or plots. Stop retains partial data. Both outputs return to zero on exit.')
FORM = [
    ('action', 'Action', 'Check settings', ['Check settings', 'Run sweep']),
    ('sweep-eom', 'EOM to sweep from -200 to +200 V', 'Bob (AO1)', list(EOMS)),
    ('fixed-voltage', 'Other EOM: fixed target voltage (-200 to +200 V)', '0', None),
    ('points', 'Number of voltage points (2–1000, including both endpoints)', '101', None),
    ('seconds', 'Dwell / photon collection time at each voltage (s)', '1', None),
    ('repeats', 'Number of sweeps (N)', '1', None),
    ('sweep-dead-seconds', 'Dead time after returning to -200 V between sweeps (s)', '1', None),
    *[item for item in sweep.FORM if item[0] not in ('action', 'points', 'seconds', 'output', 'plot-dpi')],
    ('output', 'CSV runs directory (one folder per run)', str(sweep.DATA/'single_eom'), 'directory'),
]


def settings(values):
    if values.get('action') not in ('Check settings', 'Run sweep'):
        raise ValueError('Select Check settings or Run sweep.')
    if values.get('sweep-eom') not in EOMS:
        raise ValueError('Select Alice or Bob as the EOM to sweep.')
    fixed = float(values['fixed-voltage'])
    if not math.isfinite(fixed) or not -200 <= fixed <= 200:
        raise ValueError('Fixed EOM voltage must be finite and between -200 and +200 V.')
    repeats = int(values.get('repeats', '1'))
    if not 1 <= repeats <= 1_000_000:
        raise ValueError('Number of sweeps must be an integer between 1 and 1000000.')
    dead = float(values.get('sweep-dead-seconds', '1'))
    if not math.isfinite(dead) or not 0 <= dead <= 3600:
        raise ValueError('Inter-sweep dead time must be between 0 and 3600 seconds.')
    cfg = sweep.settings(values)
    role = EOMS[values['sweep-eom']]
    other = 'alice' if role == 'bob' else 'bob'
    cfg.update(csv_format='single-eom-counts-v1', sweep_eom=role, fixed_eom=other,
               fixed_voltage_v=fixed, scan_points=cfg['points']*repeats,
               repeats=repeats, sweep_dead_s=dead,
               minimum_scan_seconds=repeats*cfg['points']*(cfg['duration_ms']/1000+cfg['settle_s'])+(repeats-1)*dead)
    cfg[role+'_axis'] = np.linspace(-200., 200., cfg['points']).tolist()
    cfg[other+'_axis'] = [fixed]
    return cfg


def main(values, stop_event=None):
    stop = stop_event if stop_event is not None else threading.Event()
    try:
        if stop.is_set():
            return 130
        cfg = settings(values)
        print(f"{cfg['sweep_eom'].title()}: {cfg['points']} points from -200 to +200 V; "
              f"{cfg['fixed_eom'].title()} fixed at {cfg['fixed_voltage_v']:g} V. "
              f"Dwell {cfg['duration_ms']/1000:g} s; spacing {400/(cfg['points']-1):g} V. "
              f"{cfg['repeats']} sweep(s); inter-sweep dead time {cfg['sweep_dead_s']:g} s. "
              f"At least {cfg['minimum_scan_seconds']:g} s plus acquisition overhead.", flush=True)
        if values['action'] == 'Check settings':
            print('Settings valid; no hardware accessed. Data-only acquisition; no analysis or plots.')
            return 0
        if cfg['source'] != sweep.NI and values['manual-ready'] != 'Yes':
            raise ValueError('Disconnect PFI12 and select the requested laser mode manually, then confirm in the Run tab.')
        return sweep.run(cfg, stop)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f'{TOOL}: {exc}', flush=True)
        return 1


if __name__ == '__main__':
    from gui_app import launch
    launch(TOOL)
