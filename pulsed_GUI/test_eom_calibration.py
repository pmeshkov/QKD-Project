"""Offline physical calibration and GUI workflow regression tests."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
import eom_calibration as workflow
import eom_calibration_core as core
import eom_model_calibration as model
import gui_app as gui
import recent_data as recent
from calibration_transfer import parse

PARAMS = np.array([np.pi/400, np.pi/380, .8, 3000., 35., 2700., 25.])


def fixture(folder, stem='test', parameters=PARAMS, random=True):
    folder = Path(folder); folder.mkdir(parents=True, exist_ok=True)
    csv = folder/f'{stem}_Detector_Traces.csv'
    rows, points, cumulative = [], [], np.zeros(2, dtype=int)
    rng = np.random.default_rng(284)
    for bob in np.linspace(-200, 200, 13):
        for alice in np.linspace(-200, 200, 13):
            seconds = float(rng.uniform(.8, 1.2))
            mu = model.rates(parameters, alice, bob)*seconds
            counts = rng.poisson(mu) if random else np.rint(mu).astype(int)
            cumulative += counts
            rows.append([alice, bob, *cumulative, *(counts/seconds)])
            points.append(dict(step=len(points), counts=counts.tolist(), seconds=seconds, flags=0))
    pd.DataFrame(rows, columns=['EOM0_Target_V','EOM1_Target_V','Raw_Count0','Raw_Count1','Rate0_Hz','Rate1_Hz']).to_csv(csv,index=False)
    csv.with_name(f'{stem}_points.jsonl').write_text('\n'.join(json.dumps(p) for p in points))
    csv.with_name(f'{stem}_sweep.json').write_text(json.dumps(dict(schema='qkd-eom-picoharp-sweep-v1',
        status='completed',complete=True,cleanup_errors=[],rows=len(rows),expected_rows=len(rows),
        settings=dict(source='NI external',mode=3))))
    return csv


class CalibrationWorkflowTests(unittest.TestCase):
    def test_default_analysis_stays_with_run_and_legacy_sources_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); csv = fixture(root/'custom-sweeps'/'run1')
            manifest = csv.with_name('test_sweep.json')
            meta=json.loads(manifest.read_text());meta['layout']='one-folder-per-sweep-v1';manifest.write_text(json.dumps(meta))
            self.assertEqual(workflow.analysis_directory(csv),csv.parent/'analysis')
            with contextlib.redirect_stdout(io.StringIO()),patch('eom_calibration_figures.render'):
                output=workflow.analyze(workflow.settings(dict(gui.defaults(workflow.TOOL),csv=str(csv))))
            self.assertEqual(output.parent,csv.parent/'analysis')
            settings={gui.sweep.TOOL:{'output':str(root/'custom-sweeps')}}
            self.assertEqual(recent.latest('calibration',settings,root),output/'calibration_for_app.json')
            self.assertEqual(recent.latest('fit_report',settings,root),output/'analysis.json')
            flat=fixture(root/'old-flat','old')
            self.assertEqual(workflow.analysis_directory(flat),flat.parent/'analysis'/'old')
            saved={workflow.TOOL:{'action':'Analyze sweep','output':str(gui.ROOT.parent/'data/eoms/calibrations')}}
            migrated=gui.initial_settings(workflow.TOOL,saved)
            self.assertEqual(migrated['output'],'');self.assertEqual(migrated['action'],workflow.FIT)

    def test_workflow_fits_all_counts_and_exports_predictions(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            path=fixture(temp)
            cfg=workflow.settings(dict(gui.defaults(workflow.TOOL),csv=str(path),output=str(Path(temp)/'analysis'),**{'plot-dpi':'72'}))
            with patch.object(core,'split_counts',side_effect=AssertionError('No training split')):
                output=workflow.analyze(cfg)
            report=json.loads((output/'analysis.json').read_text())
            transfer=parse((output/'calibration_for_app.json').read_text())
            self.assertEqual(transfer['alice_v'],report['selected']['alice_v'])
            self.assertTrue(transfer['predicted_targets_passed'])
            self.assertNotIn('heldout_count_check_passed',transfer)
            self.assertFalse(transfer['independent_count_check_passed'])
            self.assertEqual(len(list(output.glob('*.png'))),5)
            self.assertEqual(len(list(output.glob('*.pdf'))),5)
            self.assertTrue(recent.valid('calibration',output/'calibration_for_app.json'))
            table=pd.read_csv(output/'predicted_fractions.csv')
            self.assertTrue(table.measurement_status.str.contains('MODEL PREDICTION').all())

    def test_rejected_targets_no_transfer_and_stop(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()),patch('eom_calibration_figures.render'):
            path=fixture(temp,parameters=np.r_[PARAMS[:3],3000.,1500.,2700.,1500.])
            cfg=workflow.settings(dict(gui.defaults(workflow.TOOL),csv=str(path)))
            output=workflow.analyze(cfg)
            report=json.loads((output/'analysis.json').read_text())
            self.assertFalse(report['transfer_available'])
            self.assertFalse((output/'calibration_for_app.json').exists())
            stop=threading.Event();stop.set()
            with self.assertRaises(KeyboardInterrupt):workflow.analyze(cfg,stop)

    def test_recent_sweep_skips_partial_and_deleted_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);good=fixture(root/'custom','good');bad=fixture(root/'custom','bad')
            manifest=bad.with_name('bad_sweep.json');m=json.loads(manifest.read_text());m['complete']=False;manifest.write_text(json.dumps(m))
            os.utime(bad,(time.time()+10,)*2)
            settings={gui.sweep.TOOL:{'output':str(root/'custom')},recent.REGISTRY:{'sweep':str(root/'deleted.csv')}}
            self.assertEqual(recent.latest('sweep',settings,root),good)
            options=recent.dialog_options('','file','csv',settings,root,workflow.TOOL)
            self.assertEqual(Path(options['initialdir']),good.parent)

    def test_sweep_analysis_and_both_bb84_profiles_receive_same_voltages(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(gui,'SETTINGS',Path(temp)/'gui.json'):
            source=fixture(temp);root=tk.Tk();root.withdraw();app=gui.App(root,gui.sweep.TOOL)
            try:
                app.track_artifact('CSV: ',source);app.events.put(('done',0))
                root.after_cancel(app.poll_id);app.poll();app.open_calibration_analysis()
                self.assertEqual(Path(app.vars['csv'].get()),source)
                app.vars['output'].set(str(Path(temp)/'analysis'));app.vars['plot-dpi'].set('72')
                app.vars['action'].set(workflow.VERIFY)
                self.assertEqual(str(app.fields['csv'][0].cget('state')),'disabled')
                app.vars['action'].set(workflow.FIT)
                with patch.object(app,'open_plots'):
                    app.start();deadline=time.monotonic()+45
                    while app.running and time.monotonic()<deadline:root.update();time.sleep(.01)
                self.assertFalse(app.running)
                self.assertEqual(app.status.get(),'Complete',app.log.get('1.0','end'))
                result=parse(Path(app.settings[recent.REGISTRY]['calibration']).read_text())
                app.load_calibration('Pulsed polarization')
                self.assertEqual(float(app.vars['alice-0-v'].get()),result['alice_v'][0])
                self.assertEqual(app.vars['alice-labels'].get(),'Voltage index only')
                app.load_calibration(gui.bb84.RANDOM)
                self.assertEqual(float(app.vars['alice-h-v'].get()),result['alice_v'][0])
                self.assertEqual(app.vars['labels-confirmed'].get(),'No')
                # A newer rejected fit must not silently reload this good old result.
                rejected=Path(temp)/'rejected';rejected.mkdir()
                (rejected/'analysis.json').write_text(json.dumps(dict(schema=workflow.SCHEMA,status='completed',transfer_available=False)))
                app.settings[recent.REGISTRY]['fit_report']=str(rejected/'analysis.json')
                app.load_calibration(gui.bb84.RANDOM)
                self.assertIn('older result was not loaded',app.log.get('1.0','end'))
            finally:app.close()


class PhysicalModelTests(unittest.TestCase):
    def test_poisson_parameter_recovery_and_nonuniform_voltage_axes(self):
        with tempfile.TemporaryDirectory() as temp:
            grid=core.load_sweep(fixture(temp))
            fit=model.fit_response(grid)
            np.testing.assert_allclose(fit['parameters'][:2],PARAMS[:2],rtol=.01)
            self.assertAlmostEqual(fit['parameters'][2],PARAMS[2],delta=.02)
            np.testing.assert_allclose(fit['parameters'][3:],PARAMS[3:],rtol=.2)
            self.assertTrue(fit['fit_identified'])
            self.assertTrue(fit['poisson_model_check_passed'])
            # Actual CSV coordinates, including a nonuniform grid, determine phase.
            grid.alice=np.array([-195,-174,-132,-103,-65,-31,1,34,69,104,131,165,192.])
            grid.bob=grid.alice+2
            a,b=np.meshgrid(grid.alice,grid.bob)
            grid.counts=np.rint(model.rates(PARAMS,a,b)*grid.seconds[...,None]).astype(int)
            fit=model.fit_response(grid)
            np.testing.assert_allclose(fit['parameters'][:2],PARAMS[:2],rtol=.001)

    def test_fraction_targets_use_both_detectors_and_preserve_boundaries(self):
        with tempfile.TemporaryDirectory() as temp:
            grid=core.load_sweep(fixture(temp))
            p=PARAMS.copy();p[3:]=[3000.,20.,1800.,20.]
            selected=model.choose_voltages({'parameters':p},grid,balance_tolerance=.10)
            q=np.array(selected['predicted_ch1'])
            # Half of CH1's amplitude would imply a clearly unbalanced 0.624.
            naive=model.fraction(p,np.pi/4)
            self.assertLess(np.max(abs(q[core.TARGET==.5]-.5)),abs(naive-.5))
            self.assertLessEqual(max(abs(np.r_[selected['alice_v'],selected['bob_v']])),180+1e-8)
            self.assertTrue(selected['predicted_targets_pass'])
            with self.assertRaisesRegex(ValueError,'strictly below'):model.choose_voltages({'parameters':p},grid,alice_limit=200)
            with self.assertRaisesRegex(ValueError,'No periodic equivalent'):model.choose_voltages({'parameters':p},grid,alice_limit=30,bob_limit=30)

    def test_periodic_minimum_matches_dense_search_and_keeps_same_fractions(self):
        theta=np.arange(4)*np.pi/4+np.array([.01,-.02,.01,0]);delta=-.77
        for sign in (1,-1):
            p=PARAMS.copy();p[1]*=sign
            norm,a,b=model.minimum_norm_equivalent(p,theta,delta,(-175,170),(-140,150))
            np.testing.assert_allclose(model.fraction(p,p[0]*a[:,None]+p[1]*b[None,:]+p[2]),
                                       model.fraction(p,theta[:,None]+[0,delta]),atol=1e-12)
            brute=float('inf')
            for reflect in (1,-1):
                for m in range(-2,3):
                    db=(reflect*delta+m*np.pi)/p[1]
                    left,right=max(-140,-140-db),min(150,150-db)
                    if left>right:continue
                    b0=np.linspace(left,right,20001)
                    v=(reflect*theta[:,None,None]-p[2]+np.pi*np.arange(-3,4)[None,:,None]-p[1]*b0)/p[0]
                    v=np.where((v>=-175)&(v<=170),v*v,np.inf)
                    objective=v.min(axis=1).sum(axis=0)+b0*b0+(b0+db)**2
                    brute=min(brute,float(objective.min()))
            self.assertLessEqual(norm,brute+1e-6)
            self.assertAlmostEqual(norm,brute,delta=.02)

    def test_exact_voltage_span_boundary_is_not_lost(self):
        p=[np.pi/400,np.pi/400,150*np.pi/400,1,0,1,0]
        theta=np.arange(4)*np.pi/4
        norm,a,b=model.minimum_norm_equivalent(p,theta,-np.pi/4,(-150,150),(-110,110))
        self.assertAlmostEqual(norm,60000.,places=5)
        np.testing.assert_allclose(model.fraction(p,p[0]*a[:,None]+p[1]*b[None,:]+p[2]),core.TARGET,atol=1e-12)

    def test_flat_data_and_cancel(self):
        with tempfile.TemporaryDirectory() as temp:
            grid=core.load_sweep(fixture(temp,parameters=np.r_[PARAMS[:3],0.,1000.,0.,1000.],random=False))
            fit=model.fit_response(grid)
            self.assertFalse(fit['fit_identified'])
            stop=threading.Event();stop.set()
            with self.assertRaises(KeyboardInterrupt):model.fit_response(grid,stop=stop)


if __name__=='__main__':unittest.main()
