"""Explicitly published, timing-specific marker references; never infer an offset."""
import hashlib
import json
from pathlib import Path
from record_io import save_json

ROOT = Path(__file__).resolve().parents[1]/'data/timing/marker_references'
SCHEMA = 'qkd-marker-reference-v1'


def profile(device, period_ticks, delay_ticks, high_ticks, ph, chain_note):
    return dict(device=device, period_ticks=int(period_ticks), delay_ticks=int(delay_ticks),
                high_ticks=int(high_ticks), timebase_hz=100000000,
                serial=ph['serial'], sync=ph['sync'], binning=ph['binning'],
                function_generator_settings=str(chain_note).strip(),
                route='AO SampleClock rising -> P0.0 rising; Ctr0/PFI12 -> FG -> BDL -> PH330 SYNC',
                sync_divider=1, marker_holdoff_ns=0)


def key(profile):
    return hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()


def lookup(profile):
    path=ROOT/(key(profile)+'.json')
    if not path.exists():
        return None
    data=json.loads(path.read_text(encoding='utf-8'))
    if (data.get('schema') != SCHEMA or data.get('profile') != profile
            or data.get('validated') is not True
            or type(data.get('offset')) is not int or abs(data['offset']) > 3
            or data.get('repeated_starts',0) < 3 or not data.get('source_report')):
        raise ValueError(f'Invalid saved marker reference: {path}')
    return dict(data, reference_file=str(path.resolve()))


def publish(report, report_path):
    if not report.get('reference_eligible'):
        raise ValueError('Reference requires at least three clean electronic-loopback starts, matching offsets and scope timing evidence.')
    data=dict(schema=SCHEMA,validated=True,profile=report['profile'],offset=report['offset'],
              repeated_starts=len(report['runs']), ni_serial=report['ni_serial'],
              source_report=str(Path(report_path).resolve()),
              source_report_sha256=hashlib.sha256(Path(report_path).read_bytes()).hexdigest(),
              evidence=report['evidence'])
    ROOT.mkdir(parents=True,exist_ok=True)
    path=ROOT/(key(data['profile'])+'.json')
    save_json(path,data)
    return path


def for_bb84(enabled, profile):
    import bb84_markers as markers
    if enabled not in ('Yes','No'):
        raise ValueError('Use timing marker must be Yes or No.')
    if enabled == 'No':
        return dict(mode=markers.OFF)
    ref=lookup(profile)
    return dict(mode=markers.CALIBRATED if ref else markers.COMMISSION,
                pulse_sync_minus_marker_sync=ref['offset'] if ref else None,
                verification_note=f"Electronic loopback: {ref['source_report']}" if ref else '',
                reference=ref,
                calibration_status='Matching verified reference loaded' if ref else
                                   'No matching reference: spacing checks only; absolute trial origin unverified')


def for_saved_run(plan, ni_serial):
    """Resolve a later calibration using immutable acquisition settings only."""
    required=('device','period_ticks','delay_ticks','high_ticks','ph330','trigger_chain')
    if ni_serial is None or any(k not in plan for k in required) or not plan['trigger_chain']:
        return None
    p=profile(plan['device'],plan['period_ticks'],plan['delay_ticks'],plan['high_ticks'],
              plan['ph330'],plan['trigger_chain'].get('function_generator_settings',''))
    ref=lookup(p)
    if ref and ref['ni_serial'] != ni_serial:
        raise ValueError('Saved marker reference belongs to a different NI device.')
    return ref
