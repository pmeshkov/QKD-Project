"""Offline marker spacing, electronic-loopback offset and repeatability checks."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import numpy as np
from ph330_preview import decode_t3_events
from ph330_acquire import BAD_FLAGS
from record_io import save_json


def check_stop(stop):
    if stop is not None and stop.is_set():raise KeyboardInterrupt


def fig_axes(rows, size):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig=Figure(figsize=size,layout='constrained');FigureCanvasAgg(fig)
    return fig,fig.subplots(rows,1,squeeze=False)[:,0]


def command_preview(folder,plan):
    from eom_timing_scope import waveform
    period=plan['period_ns']/1000
    n=plan['samples_per_channel']
    # A single compact period demonstrates the repeated marker phase.
    x=np.arange(n+1)*period
    fig,axes=fig_axes(4,(10,8))
    wave=waveform(plan)*plan['assumed_hv_gain']
    for i,label in enumerate(('Alice target V','Bob target V')):
        axes[i].step(x,np.r_[wave[i],wave[i,0]],where='post');axes[i].set_ylabel(label)
    edges=np.arange(n)*period+plan['nominal_delay_ns']/1000
    for t in edges:axes[2].plot([t,t,t+plan['high_ns']/1000,t+plan['high_ns']/1000],[0,1,1,0],color='C2',lw=.7)
    marker=np.zeros(n);marker[plan['marker_sample']]=1
    axes[3].step(x,np.r_[marker,marker[0]],where='post',color='C3')
    axes[2].set_ylabel('PFI12 trigger');axes[3].set(ylabel='P0.0 marker',xlabel='Time from first AO clock (µs)')
    for ax in axes:ax.set_xlim(0,n*period);ax.grid(alpha=.2)
    fig.suptitle('Repeating scope commands — marker on A→B; settling and laser-chain delay must be measured')
    path=Path(folder)/'command_preview.png';fig.savefig(path,dpi=130);fig.clear()
    if plan.get('show_command_preview',True):
        print(f'Preview image: {path.resolve()}',flush=True)


def match_loopback(markers,cycles,micro,period_ns,resolution_ps,evidence,spacing):
    """Use same-cycle electronic edges, never optical counts or a best BB84 phase."""
    if len(markers)<20:raise ValueError('Need at least 20 recorded markers per start; increase short-record duration.')
    if np.any(np.diff(cycles)<0):raise ValueError('Electronic event SYNC order is not monotonic.')
    # Boundary records can be cut by Start/Stop; only first/last marker are guards.
    interior=markers[1:-1]
    indices=[]
    for m in interior:
        lo=np.searchsorted(cycles,m-2,side='left');hi=np.searchsorted(cycles,m+2,side='right')
        if hi-lo!=1:raise ValueError('Each interior marker must have exactly one CH1 electronic edge within two SYNC cycles. Check wiring, noise or missing pulses.')
        indices.append(lo)
    indices=np.array(indices,dtype=np.int64)
    if len(np.unique(indices))!=len(indices):raise ValueError('Electronic edge reused for multiple markers.')
    in_range=(cycles>=interior[0]-2)&(cycles<=interior[-1]+2)
    if np.count_nonzero(in_range)!=len(indices):raise ValueError('Extra CH1 events between reference pulses; electronic loopback is not clean.')
    selected_micro=micro[indices]*resolution_ps/1000
    if np.any((selected_micro<0)|(selected_micro>=period_ns)):
        raise ValueError('Electronic microtimes are outside the SYNC period.')
    leads=period_ns-selected_micro
    if np.any(abs(leads-evidence['lead_ns'])>evidence['tolerance_ns']):
        raise ValueError('TCSPC electronic-edge timing disagrees with the saved scope lead to the corresponding SYNC.')
    if np.any(np.minimum(leads,selected_micro)<=100):
        raise ValueError('Electronic edge is too close to a SYNC boundary (<100 ns margin).')
    deltas=cycles[indices]+1-interior
    if not np.all(deltas==deltas[0]):raise ValueError('Marker integer offset changes within a start; do not calibrate.')
    if abs(int(deltas[0]))>3:raise ValueError('Unexpected marker integer offset.')
    return dict(offset=int(deltas[0]),matched_edges=len(indices),
                lead_ns_min=float(leads.min()),lead_ns_max=float(leads.max()),lead_ns_mean=float(leads.mean()),
                boundary_markers_excluded=2)


def analyze_run(folder,cfg,stop=None):
    folder=Path(folder)
    ni=json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
    if ni.get('status')!='completed' or ni.get('cleanup_errors') or not ni.get('ao_zeroed_on_exit') or not ni.get('marker_low_on_exit'):
        raise ValueError('NI timing run did not finish and clean up successfully.')
    if ni.get('plan')!=cfg['plan']:
        raise ValueError('Scope plan differs from the parent timing-test settings.')
    raw=folder/'tttr';meta=json.loads((raw/'metadata.json').read_text(encoding='utf-8'))
    if (meta.get('schema')!='qkd-marker-test-t3-v1' or not meta.get('complete') or meta.get('status')!='completed'
            or meta.get('cleanup_errors') or meta.get('flags_seen',0)&BAD_FLAGS
            or meta.get('record_type')!='0x00010307' or meta.get('record_format')!='GenericT3'
            or meta.get('config')!=cfg['ph330']):
        raise ValueError('Incomplete, incompatible or inconsistent TCSPC test recording.')
    file=raw/'events.t3raw'
    if file.stat().st_size!=4*meta['records'] or file.stat().st_size>cfg['max_raw_bytes']:
        raise ValueError('Raw file size differs from metadata or configured cap.')
    period=float(meta['measured_sync_period_s'])*1e9
    resolution=float(meta['hardware']['resolution_ps'])
    if not np.isfinite(period) or not np.isfinite(resolution) or min(period,resolution)<=0 or abs(period/cfg['plan']['period_ns']-1)>.05:
        raise ValueError('Invalid timing resolution or measured SYNC period.')
    markers=[];cycles=[];micro=[];hist=np.zeros((2,32768),dtype=np.int64)
    carry=0;digest=hashlib.sha256();reference_count=0;marker_count=0
    from timing_marker_controls import LOOPBACK
    loopback=cfg['wiring']==LOOPBACK
    with file.open('rb') as stream:
        while True:
            check_stop(stop)
            words=np.fromfile(stream,dtype='<u4',count=262144)
            if not len(words):break
            digest.update(words.tobytes())
            d=decode_t3_events(words,carry);carry=d['carry']
            if np.any(d['marker_masks']!=1):raise ValueError('Unexpected marker mask; only Marker 1 is enabled.')
            markers.append(d['marker_cycles']);marker_count+=len(d['marker_cycles'])
            if marker_count>2000000:raise ValueError('Too many markers for a short calibration analysis; use a shorter test.')
            ch=d['channel']
            if np.any(ch>(0 if loopback else 1)):raise ValueError('Unexpected enabled input channel.')
            for channel in (0,1):hist[channel]+=np.bincount(d['micro'][ch==channel].astype(int),minlength=32768)
            if loopback:
                cycles.append(d['cycles']);micro.append(d['micro']);reference_count+=len(d['cycles'])
                if reference_count>2000000:raise ValueError('Too many electronic events; shorten test/check loopback wiring.')
    marks=np.concatenate(markers) if markers else np.array([],dtype=np.int64)
    spacing=cfg['plan']['marker_spacing_samples']
    if len(marks)<20:raise ValueError('Need at least 20 markers per start. Check wiring or increase recording duration.')
    if not np.all(np.diff(marks)==spacing):
        raise ValueError('Marker spacing failed: missing/extra marker or SYNC, or marker tagging changed near a SYNC boundary.')
    result=dict(folder=str(folder.resolve()),marker_count=len(marks),spacing_samples=spacing,spacing_passed=True,
                first_marker_sync=int(marks[0]),last_marker_sync=int(marks[-1]),
                raw_sha256=digest.hexdigest(),ni_serial=ni['serial_number'],
                photon_counts=hist.sum(axis=1).tolist(),offset=None,period_ns=period,resolution_ps=resolution)
    if loopback:
        if not cfg.get('evidence'):raise ValueError('Missing saved scope lead evidence.')
        result.update(match_loopback(marks,np.concatenate(cycles),np.concatenate(micro),period,resolution,cfg['evidence'],spacing))
    return result,hist


def analyze(folder,dpi=150,stop=None):
    folder=Path(folder).resolve();session_path=folder/'session.json'
    session=json.loads(session_path.read_text(encoding='utf-8'))
    if session.get('schema')!='qkd-marker-test-session-v1' or not session.get('complete') or session.get('status')!='completed':
        raise ValueError('Select a complete short timing-test recording folder containing session.json.')
    cfg=session['settings'];rows=[];hists=[];total_bytes=0
    from marker_reference import profile
    p=cfg['plan']
    expected_profile=profile(p['device'],p['period_ticks'],p['delay_ticks'],p['high_ticks'],cfg['ph330'],cfg['function_generator_note'])
    if cfg['profile']!=expected_profile:raise ValueError('Saved reference profile differs from the recorded hardware settings.')
    if len(session['runs'])!=cfg['repeats'] or len(set(session['runs']))!=len(session['runs']):
        raise ValueError('Missing or duplicate independent starts.')
    for name in session['runs']:
        run=(folder/name).resolve()
        if not run.is_relative_to(folder):raise ValueError('Run folder is outside its session.')
        total_bytes+=(run/'tttr/events.t3raw').stat().st_size
        r,h=analyze_run(run,cfg,stop);rows.append(r);hists.append(h)
    if total_bytes != session['raw_bytes'] or total_bytes > cfg['max_raw_bytes']:
        raise ValueError('Session raw-data size differs from metadata or exceeds the cap.')
    if len({r['resolution_ps'] for r in rows})!=1:raise ValueError('T3 resolution changed between starts.')
    if len({r['ni_serial'] for r in rows})!=1:raise ValueError('NI device changed between starts.')
    offsets={r['offset'] for r in rows}
    if len(offsets)!=1:raise ValueError('Marker offset differs between independent starts.')
    delta=rows[0]['offset']
    eligible=delta is not None and len(rows)>=3
    output=folder/'analysis'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    output.mkdir(parents=True,exist_ok=False)
    report=dict(schema='qkd-marker-test-analysis-v1',status='completed',source_session=str(session_path),
                source_session_sha256=hashlib.sha256(session_path.read_bytes()).hexdigest(),
                profile=cfg['profile'],ni_serial=rows[0]['ni_serial'],runs=rows,offset=delta,
                reference_eligible=eligible,evidence=cfg['evidence'],
                interpretation='Spacing only: absolute origin remains unverified' if delta is None else
                               'Offset = SYNC of same AO sample laser pulse minus marker SYNC; scope-backed electronic reference',
                limitation='Constant net spacing does not rule out compensating errors. Reference applies only to matching timing/cable settings.')
    fig,axes=fig_axes(3,(10,9))
    axes[0].bar(np.arange(len(rows))+1,[r['marker_count'] for r in rows],color='C0')
    axes[0].set(xlabel='Independent start',ylabel='Recorded markers',xticks=np.arange(len(rows))+1,
                title=f"Every marker interval = {cfg['plan']['marker_spacing_samples']} SYNCs: PASS")
    if delta is not None:
        axes[1].plot(np.arange(len(rows))+1,[r['offset'] for r in rows],'o',color='C3')
        axes[1].set(xlabel='Independent start',ylabel='Integer offset (SYNC cycles)',ylim=(delta-1,delta+1),
                    xticks=np.arange(len(rows))+1,yticks=[delta-1,delta,delta+1],
                    title=f"Offset {delta:+d}; {'eligible to save reference' if eligible else 'record at least 3 starts to save reference'}")
    else:
        axes[1].text(.5,.5,'Normal detector wiring: marker spacing verified\nAbsolute pulse-index offset NOT determined',ha='center',va='center',transform=axes[1].transAxes)
        axes[1].set_axis_off()
    hist=np.sum(hists,axis=0);edges=np.arange(32769)*rows[0]['resolution_ps']/1000
    for ch in (0,1):
        if hist[ch].sum():axes[2].stairs(hist[ch],edges,label=f'CH{ch+1}')
    axes[2].set(xlabel='Arrival after SYNC (ns)',ylabel='Events',xlim=(0,rows[0]['period_ns']))
    if hist.sum():axes[2].legend()
    fig.suptitle('EOM / timing marker commissioning')
    check_stop(stop)
    for ext in ('png','pdf'):fig.savefig(output/f'marker_test.{ext}',dpi=dpi)
    fig.clear();save_json(output/'analysis.json',report)
    print(f"Marker spacing PASS across {len(rows)} start(s). " + (f'Offset = {delta:+d} cycles.' if delta is not None else 'Absolute offset remains unverified.'),flush=True)
    print(f"Reference eligible to save: {eligible}. Saving is a separate explicit action.",flush=True)
    print(f'Preview image: {(output/"marker_test.png").resolve()}',flush=True)
    print(f'Run record: {session_path}',flush=True)
    return output,report
