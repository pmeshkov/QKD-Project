"""Offline timing-tool commissioning tests; no physical devices opened."""
import contextlib
import ctypes as ct
import io
import json
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import numpy as np
import gui_app as gui
import timing_marker_controls as controls
import timing_marker_analysis as analysis
import timing_marker_capture as capture
import marker_reference as reference
import eom_timing_scope as scope
import bb84_controls as bb84
import bb84_markers as markers
from record_io import save_json
from test_eom_timing_scope import FakeTask, FakeDAQ


def values(**changes):
    v=gui.defaults(controls.TOOL)
    v.update(changes)
    return v


def loop_values(**changes):
    return values(**dict({'action':controls.RECORD, 'reference-input':controls.LOOPBACK,
                         'reference-ready':'Yes', 'reference-lead-ns':'1200',
                         'reference-note':'synthetic scope capture; rising conditioned edge, following same-trial SYNC',
                         'function-generator-note':'synthetic cable setup A'}, **changes))


def make_session(root, normal=False, offsets=(1,1,1), fault=None):
    cfg=controls.settings(values(action=controls.RECORD) if normal else loop_values())
    cfg.pop('scope_args')
    cfg['repeats']=len(offsets)
    runs=[];total=0
    for j,offset in enumerate(offsets):
        run=root/f'start_{j}';(run/'tttr').mkdir(parents=True)
        runs.append(run.name)
        save_json(run/'metadata.json',dict(status='completed',cleanup_errors=[],ao_zeroed_on_exit=True,
                                          marker_low_on_exit=True,serial_number=42,plan=cfg['plan']))
        events=[]
        for i in range(40):
            m=1000+i*32
            if fault!='missing' or i!=20:
                events.append((m,0x82000000))
            if not normal:
                micro=12500 if fault!='lead' else 15000  # 800 ns after previous SYNC at 64 ps/bin
                events.append((m+offset-1,micro<<10))
                if fault=='extra' and i==20:events.append((m+offset-1,(micro+1)<<10))
        words=[];wrap=0
        for cycle,word in sorted(events):
            new=cycle//1024
            if new>wrap:words.append(0xFE000000|(new-wrap));wrap=new
            words.append(word|(cycle%1024))
        np.asarray(words,dtype='<u4').tofile(run/'tttr/events.t3raw');total+=4*len(words)
        save_json(run/'tttr/metadata.json',dict(schema='qkd-marker-test-t3-v1',status='completed',complete=True,
            cleanup_errors=[],flags_seen=0,record_type='0x00010307',record_format='GenericT3',
            config=cfg['ph330'],records=len(words),hardware=dict(resolution_ps=64),measured_sync_period_s=2e-6))
    save_json(root/'session.json',dict(schema='qkd-marker-test-session-v1',status='completed',complete=True,
                                      settings=cfg,runs=runs,raw_bytes=total))
    return cfg


class MarkerToolTests(unittest.TestCase):
    def test_compact_waveform_and_scope_ignores_inactive_ph_fields(self):
        cfg=controls.settings(values(action=controls.SCOPE, serial='', binning='junk'))
        p=cfg['plan'];a=scope.waveform(p)
        self.assertEqual(a.shape,(2,32))
        self.assertEqual(p['marker_sample'],1)
        self.assertEqual(a[0,1]*-20,150)
        self.assertNotIn('ph330',cfg)
        p=controls.settings(values(**{'hold-pulses':'3','marker-every':'32'}))['plan']
        self.assertEqual(p['samples_per_channel'],36)
        with patch.object(scope,'run_session',return_value=0) as ni, patch.object(capture,'PH330') as ph, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute(controls.TOOL,values(action=controls.SCOPE),threading.Event()),0)
            ni.assert_called_once();ph.assert_not_called()

    def test_loopback_requires_evidence_margins_and_zero_offsets(self):
        cfg=controls.settings(loop_values())
        self.assertEqual(len(cfg['ph330']['detectors']),1)
        for change in ({'reference-ready':'No'},{'reference-note':''},{'reference-lead-ns':'1950'},
                       {'reference-lead-ns':'50'},{'ch1-offset-ns':'4.5'}, {'sync-offset-ns':'1'},
                       {'ch1-edge':'falling'},{'max-raw-mib':'65'},{'function-generator-note':''},
                       {'record-repeats':'1.5'}, {'marker-every':'2048'}):
            with self.subTest(change=change),self.assertRaises(ValueError):controls.settings(loop_values(**change))

    def test_migrations_and_binary_bb84_fields(self):
        old={'action':'Output timing pattern','alice-a-v':'-80'}
        new=gui.initial_settings(controls.TOOL,{controls.TOOL:old})
        self.assertEqual(new['action'],controls.SCOPE);self.assertEqual(new['alice-a-v'],'-80')
        for tool in bb84.MODES:
            fields=gui.defaults(tool)
            self.assertEqual({k for k in fields if k.startswith('marker-')},{'marker-use'})
            new=gui.initial_settings(tool,{tool:{'marker-mode':markers.COMMISSION,'marker-offset':'999'}})
            self.assertEqual(new['marker-use'],'Yes')
        self.assertEqual(gui.display_name('Laser clock'),'Laser clock only')
        self.assertEqual(gui.internal_name('Advanced: laser clock only'),'Laser clock')

    def test_controls_only_enable_applicable_fields(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(gui,'SETTINGS',Path(temp)/'settings.json'):
            root=tk.Tk();root.withdraw();app=gui.App(root,controls.TOOL)
            state=lambda k:str(app.fields[k][0].cget('state'))
            try:
                app.vars['action'].set(controls.SCOPE)
                self.assertEqual(state('serial'),'disabled');self.assertEqual(state('seconds'),'normal')
                app.vars['action'].set(controls.RECORD)
                self.assertEqual(state('serial'),'normal');self.assertEqual(state('seconds'),'disabled')
                self.assertEqual(state('reference-lead-ns'),'disabled')
                app.vars['reference-input'].set(controls.LOOPBACK)
                self.assertEqual(state('reference-lead-ns'),'normal');self.assertEqual(state('ch2-level-mv'),'disabled')
                app.vars['action'].set(controls.ANALYZE)
                self.assertEqual(state('folder'),'normal');self.assertEqual(state('frequency-hz'),'disabled')
                app.tool.set(bb84.ORDERED);app.change_tool()
                app.vars['marker-use'].set('Yes')
                self.assertIn('No matching reference',app.marker_status.get())
            finally:app.close()

    def test_offsets_zero_one_and_overflow_and_publication(self):
        for offset in (0,1):
            with self.subTest(offset=offset),tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
                folder=Path(temp)/'run';cfg=make_session(folder,offsets=(offset,)*3)
                with patch.object(reference,'ROOT',Path(temp)/'references'):
                    self.assertEqual(reference.for_bb84('Yes',cfg['profile'])['mode'],markers.COMMISSION)
                    out,report=analysis.analyze(folder,dpi=72)
                    self.assertTrue(report['reference_eligible']);self.assertEqual(report['offset'],offset)
                    self.assertGreater((out/'marker_test.png').stat().st_size,1000)
                    refpath=reference.publish(report,out/'analysis.json')
                    self.assertTrue(refpath.is_file())
                    loaded=reference.for_bb84('Yes',cfg['profile'])
                    self.assertEqual(loaded['mode'],markers.CALIBRATED)
                    self.assertEqual(loaded['pulse_sync_minus_marker_sync'],offset)
                    self.assertEqual(reference.for_bb84('No',cfg['profile'])['mode'],markers.OFF)
                    altered=dict(cfg['profile'],delay_ticks=90)
                    self.assertEqual(reference.for_bb84('Yes',altered)['mode'],markers.COMMISSION)
                    v=gui.defaults(bb84.RANDOM);v.update({'marker-use':'Yes','function-generator-note':'synthetic cable setup A'})
                    from bb84_run import prepare_plan
                    plan=prepare_plan(bb84.settings(v,'random'))
                    self.assertEqual(plan['markers']['pulse_sync_minus_marker_sync'],offset)
                    self.assertIsNotNone(reference.for_saved_run(plan,42))
                    with self.assertRaises(ValueError):reference.for_saved_run(plan,43)

    def test_missing_extra_wrong_phase_and_restart_disagreement_refused(self):
        for fault,offsets in [('missing',(1,)*3),('extra',(1,)*3),('lead',(1,)*3),(None,(1,0,1))]:
            with self.subTest(fault=fault,offsets=offsets),tempfile.TemporaryDirectory() as temp:
                folder=Path(temp);make_session(folder,fault=fault,offsets=offsets)
                with self.assertRaises(ValueError):analysis.analyze(folder)

    def test_normal_detectors_or_single_start_cannot_publish(self):
        for normal,offsets in [(True,(1,)*3),(False,(1,))]:
            with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
                folder=Path(temp);make_session(folder,normal=normal,offsets=offsets)
                out,report=analysis.analyze(folder,dpi=72)
                self.assertFalse(report['reference_eligible'])
                with self.assertRaises(ValueError):reference.publish(report,out/'analysis.json')

    def test_stopped_analysis_and_corrupt_session_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);make_session(folder)
            stop=threading.Event();stop.set()
            with self.assertRaises(KeyboardInterrupt):analysis.analyze(folder,stop=stop)
            data=json.loads((folder/'session.json').read_text());data['raw_bytes']+=4
            save_json(folder/'session.json',data)
            with self.assertRaisesRegex(ValueError,'size'):analysis.analyze(folder)


class MarkerTask(FakeTask):
    def __init__(self,owner):
        super().__init__(owner);self.do_channels=self
    def add_do_chan(self,path,**kw):
        self.kind='do';self.owner.events.append(('do_channels',path))
    def cfg_samp_clk_timing(self,rate,**kw):
        super().cfg_samp_clk_timing(rate,**kw);self.samp_clk_src=kw.get('source')
    def start(self):
        super().start();self.owner.stop.clear()
    def write(self,data,**kw):
        if isinstance(data,bool):
            self.owner.events.append(('marker_low',data));return 1
        return super().write(data,**kw)


class MarkerDAQ(FakeDAQ):
    def Task(self):return MarkerTask(self)


class MarkerWriter:
    def __init__(self,stream,**kw):self.stream=stream
    def write_many_sample_port_byte(self,data,**kw):
        raise RuntimeError('DAQmx -200565: Minimum Write Size (In Bits): 32')
    def write_many_sample_port_uint32(self,data,**kw):
        assert data.dtype == np.uint32 and data.ndim == 1 and data.flags.c_contiguous
        assert np.all((data == 0) | (data == 1))
        self.stream.owner.events.append(('marker_data',np.flatnonzero(data).tolist()))
        return len(data)


class MarkerHardwareTests(unittest.TestCase):
    def test_armed_do_clock_and_callback_cleanup_even_on_failure(self):
        for failure in (False,True):
            with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()),patch.object(scope,'save_preview'):
                cfg=controls.settings(values(output=temp));stop=threading.Event();daq=MarkerDAQ(stop)
                constants=NS(AcquisitionType=NS(CONTINUOUS='continuous'),TaskMode=NS(TASK_VERIFY='verify',TASK_COMMIT='commit'),
                             RegenerationMode=NS(ALLOW_REGENERATION='regen'),Edge=NS(RISING='rising'),Level=NS(LOW='low'),
                             LineGrouping=NS(CHAN_FOR_ALL_LINES='all'))
                system=NS(devices={'Dev1':NS(product_type='USB-6351',dev_is_simulated=False,dev_serial_num=42)})
                def callback(folder,record,check):
                    check()
                    self.assertEqual(record['daqmx_readback']['marker']['clock'],'/Dev1/ao/SampleClock')
                    if failure:raise RuntimeError('TCSPC failed')
                code=scope.run_session(cfg['scope_args'],cfg['plan'],stop,daq,system,constants,callback,MarkerWriter)
                self.assertEqual(code,int(failure))
                e=daq.events
                self.assertLess(e.index(('start','do')),e.index(('start','ao')))
                self.assertLess(e.index(('start','co')),e.index(('start','ao')))
                self.assertLess(e.index(('stop','co')),e.index(('stop','ao')))
                self.assertIn(('marker_data',[1]),e);self.assertIn(('marker_low',False),e)
                m=json.loads(next(Path(temp).glob('*/metadata.json')).read_text())
                self.assertTrue(m['marker_low_on_exit']);self.assertTrue(m['ao_zeroed_on_exit'])

    def capture_one(self,temp,limit=1000,cancel=False,badflags=0,cleanup=False):
        cfg=controls.settings(loop_values());cfg['warmup_seconds']=0
        class API:
            path='fake.dll';version='test'
            def __init__(self):self.calls=[];self.read=0
            def bind(self,*a):pass
            def call(self,name,*a):
                self.calls.append(name)
                if name=='OpenDevice':a[1].value=cfg['ph330']['serial'].encode()
                if name=='ReadFiFo':
                    a[-1]._obj.value=2 if self.read==0 else 0
                    if not self.read:a[1][0]=0x82000005;a[1][1]=800<<10|5
                    self.read+=1
                if cleanup and name=='CloseDevice':raise RuntimeError('close failed')
        api=API()
        def scalar(api,index,name,*args):
            return {'GetSyncPeriod':2e-6,'GetFlags':badflags,'CTCStatus':1,'GetElapsedMeasTime':1000}[name]
        def check():
            if cancel and api.read:raise KeyboardInterrupt
        with patch.object(capture.ph,'bind_acquisition'),patch.object(capture.ph,'configure',return_value=dict(input_count=2,resolution_ps=64)), \
             patch.object(capture.ph,'rates',return_value=dict(sync_hz=500000)),patch.object(capture.ph,'scalar',side_effect=scalar), \
             contextlib.redirect_stdout(io.StringIO()):
            error=None
            try:capture.capture(Path(temp),cfg,check,limit,api_factory=lambda _:api)
            except BaseException as exc:error=exc
        record=json.loads((Path(temp)/'tttr/metadata.json').read_text())
        self.assertIn('StopMeas',api.calls);self.assertIn('CloseDevice',api.calls)
        return error,record

    def test_bounded_capture_completion_stop_flags_and_cleanup(self):
        for changes in ({},{'limit':4},{'cancel':True},{'badflags':4},{'cleanup':True}):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as temp:
                error,record=self.capture_one(temp,**changes)
                self.assertEqual(record['complete'],not changes)
                self.assertEqual(error is None,not changes)
                self.assertLessEqual((Path(temp)/'tttr/events.t3raw').stat().st_size,changes.get('limit',1000))


if __name__=='__main__':unittest.main()
