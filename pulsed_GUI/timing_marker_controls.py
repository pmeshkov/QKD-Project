"""GUI-native EOM/marker scope test and bounded electronic timing calibration."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
from types import SimpleNamespace
import eom_timing_scope as scope
import bb84_controls as bb84
import marker_reference
from ph330_acquire import validate
from record_io import save_json

TOOL='EOM timing scope'  # Preserve saved profiles and launcher compatibility.
CHECK='Check settings'
SCOPE='Scope only (no TCSPC recording)'
RECORD='Record short TCSPC test'
ANALYZE='Analyze saved run'
PUBLISH='Save verified reference for BB84'
NORMAL='Normal detectors (marker spacing only)'
LOOPBACK='Conditioned marker copy on CH1'
HELP=('AO0/AO1, P0.0 Marker 1 and delayed Ctr0/PFI12 share hardware timing. '
      'Scope only repeats a small onboard buffer until Stop and saves no TTTR or full-run AO array. '
      'Marker rises on an A→B command, once per repeated buffer; high for one sample. '
      'Short TCSPC tests have a total raw-file cap. Normal detectors check spacing only. '
      'Electrical loopback requires a properly conditioned marker copy on CH1 with the detector disconnected; '
      'record its scope-measured lead to the SAME trial’s SYNC. Never feed raw NI TTL into CH1. '
      'Stop, edit and Run to adjust. Three clean loopback starts allow an explicit Save verified reference action.')
PH_FIELDS=[s for s in bb84.form('ordered') if s[0] in ('dll','device-index','serial','binning') or s[0].startswith(('sync-','ch1-','ch2-'))]
FORM=[
    ('action','Action',CHECK,[CHECK,SCOPE,RECORD,ANALYZE,PUBLISH]),
    ('device','NI USB-6351 device','Dev1',None),
    ('frequency-hz','Laser trigger / AO sample rate (Hz)','500000',None),
    ('delay-ns','NI trigger delay after voltage command (ns)','1200',None),
    ('high-ns','PFI12 pulse width (ns; function-generator output width is manual)','100',None),
    ('function-generator-note','Function generator settings / cable configuration ID (match in BB84)','',None),
    ('hold-pulses','Laser pulses per voltage level (1 = switch each trial)','1',None),
    ('marker-every','Requested marker spacing (trials; rounded up to whole A/B cycles)','32',None),
    ('alice-a-v','Alice / AO0 level A (EOM target V)','-150',None),
    ('alice-b-v','Alice / AO0 level B (EOM target V)','150',None),
    ('bob-a-v','Bob / AO1 level A (EOM target V)','-50',None),
    ('bob-b-v','Bob / AO1 level B (EOM target V)','50',None),
    ('seconds','Scope-only duration (s; 0 = until Stop)','0',None),
    ('record-seconds','TCSPC recording per start (s)','1',None),
    ('record-repeats','Separate NI starts (3 needed to save a reference)','3',None),
    ('warmup-s','Settling before each TCSPC recording (s)','1',None),
    ('max-raw-mib','Maximum raw data across this whole test (MiB)','16',None),
    ('reference-input','TCSPC input wiring',NORMAL,[NORMAL,LOOPBACK]),
    ('reference-ready','Loopback: conditioned signal/edge checked at CH1 input?','No',['No','Yes']),
    ('reference-lead-ns','Loopback edge → SAME trial’s following SYNC: scope-measured ns','',None),
    ('reference-tolerance-ns','Allowed scope/TCSPC lead discrepancy (ns)','100',None),
    ('reference-note','Loopback scope/cable evidence (include which edge and capture ID)','',None),
    *PH_FIELDS,
    ('folder','Saved timing-test folder (analysis / save reference)','', 'directory'),
    ('plot-dpi','Saved figure resolution (DPI)','150',None),
    ('output','Timing-test data directory',str(scope.DATA),'directory'),
    ('note','Scope settings / measured settling / notes','',None),
]


def settings(values):
    from figure_options import validated_dpi
    action=values['action']
    if action not in (CHECK,SCOPE,RECORD,ANALYZE,PUBLISH):
        raise ValueError('Select a timing-test action.')
    dpi=validated_dpi(values['plot-dpi'])
    if action in (ANALYZE,PUBLISH):
        if not values['folder'].strip():raise ValueError('Select the saved timing-test folder.')
        return dict(action=action,folder=values['folder'],plot_dpi=dpi)
    def num(k,low,high):
        x=float(values[k])
        if not math.isfinite(x) or not low<=x<=high:raise ValueError(f'{k} must be within {low:g}..{high:g}.')
        return x
    def integer(k,low,high):
        x=num(k,low,high)
        if not x.is_integer():raise ValueError(f'{k} must be an integer.')
        return int(x)
    args=SimpleNamespace(device=values['device'],frequency_hz=num('frequency-hz',1000,1000000),
        delay_ns=num('delay-ns',20,1000000),high_ns=num('high-ns',20,1000000),
        hold_pulses=integer('hold-pulses',1,256),seconds=num('seconds',0,86400),
        output=Path(values['output']),note=values.get('note',''),
        **{key.replace('-','_'):num(key,-200,200) for key in ('alice-a-v','alice-b-v','bob-a-v','bob-b-v')})
    plan=scope.make_plan(args)
    cycle=2*args.hold_pulses
    spacing=math.ceil(max(8,integer('marker-every',8,1024))/cycle)*cycle
    if spacing>1024:raise ValueError('Choose a marker spacing/hold combination fitting 1024 samples.')
    plan.update(marker_enabled=True,marker_sample=args.hold_pulses,samples_per_channel=spacing,
                marker_spacing_samples=spacing,
                show_command_preview=action!=RECORD,
                mode='continuous onboard regenerated scope pattern; not a BB84 sequence',
                marker_note='P0.0 rises at an A→B voltage command and stays high for one AO sample.')
    cfg=dict(action=action,scope_args=args,plan=plan,plot_dpi=dpi,output=values['output'],note=args.note)
    if action != RECORD:return cfg
    wiring=values['reference-input']
    if wiring not in (NORMAL,LOOPBACK):raise ValueError('Select the TCSPC wiring.')
    def trigger(prefix):
        r=dict(mode=values[prefix+'-mode'],edge=values[prefix+'-edge'],level_mv=int(values[prefix+'-level-mv']),
               offset_ps=round(num(prefix+'-offset-ns',-99,99)*1000))
        if r['mode']=='cfd':r['zero_cross_mv']=int(values[prefix+'-zero'])
        return r
    detectors=[dict(trigger('ch1'),channel=0)]
    if wiring==NORMAL:detectors.append(dict(trigger('ch2'),channel=1))
    ph=dict(device_index=int(values['device-index']),serial=values['serial'],binning=int(values['binning']),
            laser_hz=plan['realized_rate_hz'],sync_divider=1,sync=trigger('sync'),detectors=detectors)
    validate(ph,detector_count=len(detectors),expected_laser_hz=None)
    cfg.update(ph330=ph,dll=values['dll'],record_seconds=num('record-seconds',.01,60),
               repeats=integer('record-repeats',1,10),warmup_seconds=num('warmup-s',.2,60),
               max_raw_bytes=round(num('max-raw-mib',1,64)*1048576),wiring=wiring,
               function_generator_note=values.get('function-generator-note','').strip())
    cfg['profile']=marker_reference.profile(args.device,plan['period_ticks'],plan['delay_ticks'],plan['high_ticks'],ph,cfg['function_generator_note'])
    cfg['evidence']=None
    if wiring==LOOPBACK:
        if values.get('reference-ready')!='Yes' or not values.get('reference-note','').strip():
            raise ValueError('Loopback needs the conditioned CH1 signal checked on scope and a scope/cable evidence note.')
        if ph['sync']['offset_ps'] or ph['detectors'][0]['offset_ps']:
            raise ValueError('Use zero SYNC and CH1 channel offsets for electronic calibration.')
        if ph['detectors'][0]['mode']!='edge' or ph['detectors'][0]['edge']!='rising':
            raise ValueError('The electronic copy must represent the marker rising edge; select CH1 rising-edge mode.')
        lead=num('reference-lead-ns',1,plan['period_ns']-1)
        tol=num('reference-tolerance-ns',1,plan['period_ns']/4)
        if min(lead,plan['period_ns']-lead)<=max(100,tol):
            raise ValueError('Reference edge must be clearly between SYNC boundaries with >100 ns and >tolerance margin.')
        if not cfg['function_generator_note']:
            raise ValueError('Record the function-generator settings / cable configuration ID before loopback calibration.')
        cfg['evidence']=dict(lead_ns=lead,tolerance_ns=tol,note=values['reference-note'].strip(),
                             statement='Conditioned rising edge occurs after preceding SYNC and before the SAME AO sample laser SYNC')
    return cfg


def main(values,stop_event=None):
    stop=stop_event if stop_event is not None else threading.Event()
    try:
        cfg=settings(values)
        if cfg['action'] in (ANALYZE,PUBLISH):
            from timing_marker_analysis import analyze
            output,report=analyze(cfg['folder'],cfg['plot_dpi'],stop)
            if cfg['action']==PUBLISH:
                path=marker_reference.publish(report,output/'analysis.json')
                print(f'Marker reference saved: {path.resolve()}',flush=True)
            return 0
        print(json.dumps(cfg['plan'],indent=2),flush=True)
        if cfg['action']==CHECK:
            print('Offline check only. Scope uses one compact repeating buffer; no instrument opened.')
            return 0
        if cfg['action']==SCOPE:
            print('SCOPE ONLY: no PicoHarp opened; no photon recording or full-run AO array.',flush=True)
            return scope.run_session(cfg['scope_args'],cfg['plan'],stop)
        from timing_marker_capture import capture
        folder=Path(cfg['output'])/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ_marker_test')
        folder.mkdir(parents=True,exist_ok=False)
        manifest=folder/'session.json'
        public={k:v for k,v in cfg.items() if k!='scope_args'}
        session=dict(schema='qkd-marker-test-session-v1',status='preparing',complete=False,settings=public,runs=[])
        save_json(manifest,session)
        print(f"At most {cfg['max_raw_bytes']/1048576:g} MiB raw across {cfg['repeats']} starts; no full-run AO files.",flush=True)
        used=0
        try:
            for i in range(cfg['repeats']):
                if stop.is_set():raise KeyboardInterrupt
                if used >= cfg['max_raw_bytes']:raise RuntimeError('Total raw-data limit reached before the next start.')
                cfg['scope_args'].output=folder
                def record_one(run_folder,ni_record,check):
                    session['runs'].append(run_folder.name)
                    save_json(manifest,session)
                    capture(run_folder,cfg,check,cfg['max_raw_bytes']-used)
                code=scope.run_session(cfg['scope_args'],cfg['plan'],stop,on_running=record_one)
                if code:
                    if code==130:raise KeyboardInterrupt
                    raise RuntimeError('Timing-test start failed; inspect its metadata.')
                used=sum(p.stat().st_size for p in folder.rglob('events.t3raw'))
            session.update(status='completed',complete=True,raw_bytes=used)
        except BaseException as exc:
            session.update(status='cancelled' if isinstance(exc,KeyboardInterrupt) else 'error',error=str(exc))
            raise
        finally:
            save_json(manifest,session)
            print(f'Run record: {manifest.resolve()}',flush=True)
        from timing_marker_analysis import analyze
        analyze(folder,cfg['plot_dpi'],stop)
        return 0
    except KeyboardInterrupt:
        print('Timing test stopped; partial TCSPC data cannot calibrate a reference.',flush=True)
        return 130
    except Exception as exc:
        print(f'Timing / marker test: {exc}',flush=True)
        return 1


if __name__=='__main__':
    from gui_app import launch
    launch(TOOL)
