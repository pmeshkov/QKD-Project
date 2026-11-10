"""Offline single-EOM CSV acquisition and GUI integration checks."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import gui_app as gui
import sweep_one_eom as single
import eom_voltage_sweep as sweep
import recent_data
from test_eom_voltage_sweep import FastStop, SweepAPI
from test_laser_clock_eom import FakeDAQ
from test_pulsed_polarization import dependencies


def values(**extra):
    result = gui.defaults(single.TOOL)
    result.update(serial='test', **{'manual-ready':'Yes'})
    result.update(extra)
    return result


class SingleEOMTests(unittest.TestCase):
    def test_repeated_sweeps_share_csv_and_wait_after_voltage_reset(self):
        for label,role in single.EOMS.items():
            for repeats,interrupt in ((1,False),(3,False),(3,True)):
                with self.subTest(role=role,repeats=repeats,interrupt=interrupt),tempfile.TemporaryDirectory() as temp:
                    cfg = single.settings(values(output=temp,points='3',repeats=str(repeats),
                        **{'sweep-eom':label,'fixed-voltage':'25','sweep-dead-seconds':'2.5'}))
                    self.assertEqual(cfg['scan_points'],3*repeats)
                    self.assertAlmostEqual(cfg['minimum_scan_seconds'],3*repeats*1.01+(repeats-1)*2.5)
                    stop = FastStop(); api = SweepAPI(stop); daq = FakeDAQ()
                    constants,system = dependencies()
                    events = []
                    def wait(seconds):
                        if seconds == 2.5:
                            writes = [e[1] for e in daq.events if e[0] == 'write']
                            events.append((api.starts,writes[-1]))
                            if interrupt:
                                stop.set()
                        return stop.is_set()
                    with patch.object(stop,'wait',side_effect=wait),contextlib.redirect_stdout(io.StringIO()):
                        code = sweep.run(cfg,stop,daq=daq,api=api,constants=constants,system=system)
                    interrupted = interrupt and repeats > 1
                    self.assertEqual(code,130 if interrupted else 0)
                    reset = [10.,-1.25] if role == 'alice' else [-1.25,10.]
                    self.assertEqual(events,[(3,reset)] if interrupted else [(3*i,reset) for i in range(1,repeats)])
                    manifest = next(Path(temp).rglob('*_sweep.json'))
                    meta = json.loads(manifest.read_text())
                    self.assertEqual(meta['expected_rows'],3*repeats)
                    self.assertEqual(meta['rows'],3 if interrupted else 3*repeats)
                    self.assertEqual(api.starts,meta['rows'])
                    self.assertEqual(len(list(Path(temp).rglob('*.csv'))),1)
                    with (manifest.parent/meta['csv']).open(newline='') as stream:
                        reader = csv.DictReader(stream)
                        self.assertEqual(reader.fieldnames,(['Sweep_Number'] if repeats > 1 else [])+sweep.SINGLE_HEADER)
                        rows = list(reader)
                    finished = 1 if interrupted else repeats
                    self.assertEqual([int(r['Point']) for r in rows],[1,2,3]*finished)
                    column = role.title()+'_Target_V'
                    self.assertEqual([float(r[column]) for r in rows],[-200,0,200]*finished)
                    self.assertEqual([int(r['CH1_Counts']) for r in rows],[2]*len(rows))
                    if repeats > 1:
                        self.assertEqual([int(r['Sweep_Number']) for r in rows],
                                         [n for n in range(1,finished+1) for _ in range(3)])
                    self.assertEqual(meta['complete'],not interrupted)
                    self.assertTrue(meta['ao_zeroed_on_exit'])
                    self.assertEqual(daq.events[-2:],[('write',[0.,0.]),('close','ao')])

    def test_axes_include_endpoints_and_fixed_voltage_for_either_eom(self):
        for label,role in single.EOMS.items():
            for fixed in (-200,17.5,200):
                cfg = single.settings(values(points='5', seconds='2', **{'sweep-eom':label,'fixed-voltage':str(fixed)}))
                pairs = np.array(list(sweep.grid(cfg)))
                col = 0 if role == 'alice' else 1
                np.testing.assert_array_equal(pairs[:,col],[-200,-100,0,100,200])
                np.testing.assert_array_equal(pairs[:,1-col],[fixed]*5)
                self.assertAlmostEqual(cfg['minimum_scan_seconds'],10.05)

    def acquire(self, folder, label='Bob (AO1)', fail=None, source=sweep.CW, hz=0):
        cfg = single.settings(values(output=str(folder),points='3',source=source,
                                     **{'sweep-eom':label,'fixed-voltage':'25'}))
        stop = FastStop(); api = SweepAPI(stop,fail,hz); daq = FakeDAQ(fail_write=fail == 'ao')
        constants,system = dependencies()
        with patch.object(sweep,'plot_completed',side_effect=AssertionError('No plots')), contextlib.redirect_stdout(io.StringIO()):
            code = sweep.run(cfg,stop,daq=daq,api=api,constants=constants,system=system)
        meta_path = next(Path(folder).rglob('*_sweep.json'))
        return code,json.loads(meta_path.read_text()),meta_path.parent,api,daq

    def test_csv_has_per_dwell_counts_exposure_voltages_and_no_analysis(self):
        for label,role in single.EOMS.items():
            with self.subTest(role=role),tempfile.TemporaryDirectory() as temp:
                code,meta,folder,api,daq = self.acquire(temp,label)
                self.assertEqual(code,0)
                self.assertEqual(meta['schema'],'qkd-single-eom-counts-v1')
                self.assertEqual(meta['expected_rows'],3)
                self.assertEqual(api.starts,3)
                path = folder/meta['csv']
                with path.open(newline='') as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(reader.fieldnames,sweep.SINGLE_HEADER)
                    rows = list(reader)
                self.assertEqual([int(r['Point']) for r in rows],[1,2,3])
                self.assertEqual([int(r['CH1_Counts']) for r in rows],[2,2,2])
                self.assertEqual([int(r['CH2_Counts']) for r in rows],[1,1,1])
                self.assertEqual([float(r['Dwell_s']) for r in rows],[.1]*3)
                swept,fixed = ('Alice','Bob') if role == 'alice' else ('Bob','Alice')
                self.assertEqual([float(r[swept+'_Target_V']) for r in rows],[-200,0,200])
                self.assertEqual([float(r[fixed+'_Target_V']) for r in rows],[25]*3)
                writes = [v for event,v in daq.events if event == 'write']
                expected = [[-float(r['Alice_Target_V'])/20,-float(r['Bob_Target_V'])/20] for r in rows]
                self.assertEqual(writes,expected+[[0.,0.]])
                self.assertTrue(meta['ao_zeroed_on_exit'])
                self.assertFalse(recent_data.valid('sweep',path))
                self.assertFalse(list(folder.glob('*.png')))
                self.assertFalse((folder/'analysis.json').exists())

    def test_stop_and_failures_keep_only_completed_dwells(self):
        for failure,expected in (('stop',130),('fifo',1),('ao',1)):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temp:
                code,meta,folder,api,daq = self.acquire(temp,fail=failure)
                self.assertEqual(code,expected)
                self.assertFalse(meta['complete'])
                self.assertTrue(meta['csv'].endswith('.partial.csv'))
                self.assertTrue(meta['ao_zeroed_on_exit'])
                self.assertEqual(meta['rows'],0 if failure == 'ao' else 1)
                self.assertFalse(list(folder.glob('*_Single_EOM_Counts.csv')))
                if failure != 'ao':
                    with (folder/meta['csv']).open(newline='') as stream:
                        self.assertEqual(len(list(csv.DictReader(stream))),1)

    def test_internal_and_external_pulsed_acquisition(self):
        for source,hz in (('Laser internal 80 MHz',80_000_000),(sweep.NI,500_000)):
            with self.subTest(source=source),tempfile.TemporaryDirectory() as temp:
                code,meta,folder,api,daq = self.acquire(temp,source=source,hz=hz)
                self.assertEqual(code,0)
                self.assertEqual(api.initialized_mode,3)
                self.assertEqual([event[1] for event in daq.events if event[0] == 'clock_route'],['Dev1/ctr0'] if source == sweep.NI else [])

    def test_gui_dispatch_check_settings_and_confirmation(self):
        with patch.object(sweep,'run',return_value=0) as run,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(gui.execute(single.TOOL,values(),threading.Event()),0)
            self.assertEqual(gui.execute(single.TOOL,values(action='Run sweep',**{'manual-ready':'No'}),threading.Event()),1)
            run.assert_not_called()
            self.assertEqual(gui.execute(single.TOOL,values(action='Run sweep'),threading.Event()),0)
            run.assert_called_once()
        inherited = gui.initial_settings(single.TOOL,{sweep.TOOL:{'ch1-level-mv':'222'}})
        self.assertEqual(inherited['ch1-level-mv'],'222')
        self.assertNotIn('plot-dpi',gui.defaults(single.TOOL))
        self.assertEqual(gui.field_group('fixed-voltage',single.TOOL),'Voltages')
        self.assertEqual(gui.field_group('seconds',single.TOOL),'Recording')
        self.assertEqual(gui.field_group('repeats',single.TOOL),'Recording')
        self.assertEqual(gui.field_group('sweep-dead-seconds',single.TOOL),'Recording')

    def test_validation_rejects_invalid_inputs_without_hardware(self):
        for key,value in (('fixed-voltage','nan'),('fixed-voltage','201'),('fixed-voltage','-200.01'),
                          ('points','1'),('points','1001'),('points','2.5'),('seconds','0'),
                          ('seconds','0.1005'),('seconds','inf'),('sweep-eom','Both'),('action','Analyze'),
                          ('repeats','0'),('repeats','2.5'),('repeats','1000001'),
                          ('sweep-dead-seconds','-1'),('sweep-dead-seconds','nan'),('sweep-dead-seconds','3601')):
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                single.settings(values(**{key:value}))

    def test_old_settings_default_to_one_sweep_and_zero_dead_time_is_allowed(self):
        old = values()
        old.pop('repeats'); old.pop('sweep-dead-seconds')
        self.assertEqual(single.settings(old)['repeats'],1)
        cfg = single.settings(values(repeats='2',**{'sweep-dead-seconds':'0'}))
        self.assertEqual(cfg['sweep_dead_s'],0)
        self.assertAlmostEqual(cfg['minimum_scan_seconds'],2*101*1.01)


if __name__ == '__main__':
    unittest.main()
