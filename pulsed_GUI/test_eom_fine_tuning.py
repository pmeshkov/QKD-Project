"""Offline single-EOM recovery, constraints, workflow and hardware-boundary tests."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np

import eom_fine_tuning as fine
import eom_fine_model as model
import eom_voltage_sweep as sweep
import eom_model_calibration as response
from eom_calibration_core import load_sweep
from calibration_transfer import parse
import gui_app as gui
import recent_data
from test_eom_voltage_sweep import SweepAPI, FastStop
from test_laser_clock_eom import FakeDAQ
from test_pulsed_polarization import dependencies


def prior_report(sign=1):
    return dict(schema='qkd-eom-model-calibration-v2', status='completed', transfer_available=True,
                fit=dict(fit_identified=True, parameters=[np.pi/400, sign*np.pi/380, np.pi/4, 3000,20,3000,20]),
                selected=dict(alice_v=[-150.,-50.,50.,150.], bob_v=[sign*47.5, -sign*47.5],
                              operating_bounds=dict(alice=[-180.,180.],bob=[-180.,180.])),
                parameters=dict(balance_tolerance=.05,extinction_tolerance=.15))


def fake_runner(parameters, *, fail_verify=False, stop_verify=False, zero_verify=False, variable_dwell=False):
    calls = []
    def run(cfg, stop, completed_result):
        calls.append(cfg)
        folder = Path(cfg['output'])/'mock_run'; folder.mkdir(parents=True)
        if len(calls) == 2 and (fail_verify or stop_verify):
            return 130 if stop_verify else 1
        records, cumulative, rows = [], np.zeros(2, dtype=int), []
        rng = np.random.default_rng(930)
        for n,(alice,bob) in enumerate(sweep.grid(cfg)):
            seconds = cfg['duration_ms']/1000
            if variable_dwell:
                seconds *= 1 + n % 3
            counts = rng.poisson(response.rates(parameters,alice,bob)*seconds)
            if len(calls) == 2 and zero_verify:
                counts[:] = 0
            cumulative += counts
            rows.append([n,n,alice,bob,*cumulative,*(counts/seconds)])
            records.append(dict(step=n,counts=counts.tolist(),seconds=seconds,flags=0))
        csv_path = folder/'test_Detector_Traces.csv'
        with csv_path.open('w',newline='') as f:
            writer=csv.writer(f); writer.writerow(sweep.HEADER); writer.writerows(rows)
        pts = folder/'test_points.jsonl'
        pts.write_text('\n'.join(json.dumps(r) for r in records))
        manifest = folder/'test_sweep.json'
        manifest.write_text(json.dumps(dict(schema='qkd-eom-picoharp-sweep-v1', status='completed',complete=True,
                                           cleanup_errors=[],rows=len(rows),expected_rows=len(rows),settings=cfg)))
        completed_result.update(csv=str(csv_path),points=str(pts),manifest=str(manifest))
        return 0
    return run,calls


def config(root, prior=None, **extra):
    prior = prior or prior_report()
    source = Path(root)/'global_analysis.json'; source.write_text(json.dumps(prior))
    values = dict(gui.defaults(fine.TOOL), **{'fit-report':str(source), 'manual-ready':'Yes',
                                            'output':str(Path(root)/'fine'), 'plot-dpi':'72'})
    values.update(extra)
    return fine.settings(values)


class FineTuningTests(unittest.TestCase):
    def test_joint_curve_fit_recovers_five_parameters_from_prior_seed(self):
        prior = prior_report()
        actual = np.array(prior['fit']['parameters'],float)
        actual[2:] = [actual[2]+.04,4200,35,3900,45]
        alice, bob = np.array([-150.]), np.linspace(-80.,-15.,33)
        av,bv = np.meshgrid(alice,bob)
        seconds = np.linspace(10,30,len(bob))[:,None]
        grid = SimpleNamespace(alice=alice,bob=bob,seconds=seconds,
                               counts=response.rates(actual,av,bv)*seconds[...,None])
        with patch.object(model,'curve_fit',wraps=model.curve_fit) as optimizer:
            fit = model.fit_local(prior,grid)
        self.assertEqual(optimizer.call_count,1)
        np.testing.assert_equal(optimizer.call_args.kwargs['p0'],prior['fit']['parameters'][2:])
        np.testing.assert_allclose(fit['parameters'],actual,rtol=1e-6,atol=1e-6)
        self.assertEqual(np.asarray(fit['covariance']).shape,(5,5))
        self.assertTrue(fit['usable'])
        # An alternating disturbance cannot be absorbed by the sinusoidal model.
        grid.counts[::2,:,0] *= 1.5
        self.assertFalse(model.fit_local(prior,grid)['usable'])

    def test_offline_refit_preserves_original_and_clears_old_verification(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()),patch('eom_fine_figures.render'):
            cfg = config(temp)
            runner,_ = fake_runner(cfg['prior']['fit']['parameters'])
            folder = fine.run(cfg,threading.Event(),runner)
            originals = {p:p.read_bytes() for p in folder.rglob('*') if p.is_file()}
            values = dict(gui.defaults(fine.TOOL),action=fine.REFIT,folder=str(folder),**{'plot-dpi':'72'})
            with patch.object(sweep,'run',side_effect=AssertionError('No acquisition')):
                self.assertEqual(gui.execute(fine.TOOL,values,threading.Event()),0)
            path = next((folder/'analysis').rglob('refit_report.json'))
            refit = json.loads(path.read_text())
            self.assertEqual(refit['status'],'refit_only')
            self.assertEqual(refit['fit']['optimizer'],'scipy.optimize.curve_fit')
            self.assertIn('selected',refit)
            self.assertNotIn('verification',refit)
            self.assertFalse(refit['transfer_available'])
            self.assertFalse((path.parent/'calibration_for_app.json').exists())
            for p,contents in originals.items():
                self.assertEqual(p.read_bytes(),contents)

    def test_rejected_candidate_requires_explicit_override_and_keeps_provenance(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            prior = prior_report()
            prior.update(transfer_available=False,rejection_reason='Predicted fractions fail target tolerances.')
            with self.assertRaisesRegex(ValueError,'Override: allow rejected'):
                config(temp,prior)
            cfg = config(temp,prior,**{'allow-rejected-prior':'Yes'})
            self.assertTrue(cfg['allow_rejected_prior'])
            self.assertTrue(cfg['prior_source']['rejected_candidate_override'])
            self.assertFalse(cfg['prior_source']['candidate_accepted'])
            self.assertEqual(cfg['prior_source']['rejection_reason'],prior['rejection_reason'])
            source = Path(cfg['prior_source']['path']); frozen = source.read_bytes()
            runner,_ = fake_runner(prior['fit']['parameters'])
            with patch('eom_fine_figures.render'):
                output = fine.run(cfg,threading.Event(),runner)
            report = json.loads((output/'analysis.json').read_text())
            self.assertTrue(report['prior_source']['rejected_candidate_override'])
            transfer = parse((output/'calibration_for_app.json').read_text())
            self.assertTrue(transfer['rejected_candidate_override'])
            self.assertFalse(transfer['parent_candidate_accepted'])
            self.assertEqual(transfer['parent_rejection_reason'],prior['rejection_reason'])
            self.assertEqual(source.read_bytes(),frozen)
            self.assertEqual(json.loads((output/'prior_analysis.json').read_text()),prior)

    def test_override_keeps_source_fit_and_voltage_requirements(self):
        cases = [dict(status='error'),dict(schema='other'),dict(selected=None),
                 dict(fit=dict(prior_report()['fit'],fit_identified=False)),
                 dict(selected=dict(prior_report()['selected'],alice_v=[-190.,-50.,50.,150.])),
                 dict(fit=dict(prior_report()['fit'],parameters=[float('nan')]*7))]
        for changes in cases:
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as temp:
                prior = prior_report(); prior.update(transfer_available=False,**changes)
                with self.assertRaises(ValueError):
                    config(temp,prior,**{'allow-rejected-prior':'Yes'})

    def test_override_does_not_bypass_new_target_or_verification_checks(self):
        for failed_check in ('targets','verification'):
            with self.subTest(check=failed_check),tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
                prior = prior_report(); prior['transfer_available'] = False
                if failed_check == 'targets':
                    prior['parameters']['extinction_tolerance'] = .001
                cfg = config(temp,prior,**{'allow-rejected-prior':'Yes'})
                runner,_ = fake_runner(prior['fit']['parameters'],zero_verify=failed_check == 'verification')
                with patch('eom_fine_figures.render'):
                    output = fine.run(cfg,threading.Event(),runner)
                report = json.loads((output/'analysis.json').read_text())
                self.assertEqual(report['status'],'completed')
                self.assertFalse(report['transfer_available'])
                self.assertFalse((output/'calibration_for_app.json').exists())

    def test_phase_recovery_and_continuous_six_voltage_solution_for_both_slope_signs(self):
        for sign in (-1,1):
            for eom,role in fine.EOMS.items():
                for pair in (list(fine.PAIRS)[0], list(fine.PAIRS)[1], list(fine.PAIRS)[4]):
                    with self.subTest(sign=sign,pair=pair,eom=role), tempfile.TemporaryDirectory() as temp:
                        cfg = config(temp,prior_report(sign), pair=pair, **{'sweep-eom':eom})
                        changed = np.asarray(cfg['prior']['fit']['parameters']); changed[2] += .045; changed[3:] *= 1.4
                        runner,_ = fake_runner(changed, variable_dwell=True)
                        result = fine.acquire(cfg,Path(temp)/'scan', {r+'_axis':cfg[r+'_axis'] for r in ('alice','bob')},cfg['duration_ms'],threading.Event(),runner)
                        grid = load_sweep(result['csv'], single_eom=role)
                        self.assertEqual(grid.counts.shape, (1,11,2) if role == 'alice' else (11,1,2))
                        fit = model.fit_local(cfg['prior'],grid)
                        self.assertTrue(fit['usable'],fit['diagnostics'])
                        self.assertAlmostEqual(fit['phase_correction_rad'],.045,
                                               delta=3*fit['parameter_standard_errors'][0])
                        self.assertEqual(fit['optimizer'],'scipy.optimize.curve_fit')
                        np.testing.assert_equal(fit['parameters'][:2],changed[:2])
                        i,j = cfg['pair_indices']
                        selected = model.refine_voltages(cfg['prior'],fit,cfg,i,j,180,role)
                        self.assertTrue(selected['predicted_targets_pass'])
                        fixed = 'bob' if role == 'alice' else 'alice'
                        self.assertEqual(selected[fixed+'_v'], cfg['prior']['selected'][fixed+'_v'])
                        self.assertEqual(selected[fixed+'_common_shift_v'], 0.)
                        index, slope = (i,changed[0]) if role == 'alice' else (j,changed[1])
                        actual = response.rates(changed,selected['alice_v'][i],selected['bob_v'][j])
                        self.assertAlmostEqual(actual[0]/actual.sum(),fine.TARGET[i,j],delta=.015)
                        self.assertFalse(any(np.isclose(selected[role+'_v'][index],cfg[role+'_axis'],atol=1e-6)))
                        np.testing.assert_allclose(np.diff(selected['alice_v']),[100,100,100],atol=1e-10)
                        np.testing.assert_allclose(np.diff(selected['bob_v']),[-95*sign],atol=1e-10)
                        if j == 1:
                            self.assertAlmostEqual(selected['predicted_ch1'][i][j],.5,places=10)

    def test_scan_limits_and_no_extrapolation(self):
        prior = prior_report()
        with self.assertRaisesRegex(ValueError,'exceeds'):
            model.scan_axes(prior,0,0,10,5,180,'alice')
        # Alice can stay at the boundary when Bob is the only swept EOM.
        axes = model.scan_axes(prior,0,1,2,5,150,'bob')
        self.assertEqual(axes['alice_axis'], [-150.])
        self.assertEqual(len(axes['bob_axis']), 11)
        p = np.asarray(prior['fit']['parameters']); p[2]+=.5
        with self.assertRaisesRegex(ValueError,'outside'):
            model.refine_voltages(prior,dict(parameters=p),axes,0,1,180,'bob')
        with self.assertRaisesRegex(ValueError,'Unchanged'):
            model.scan_axes(prior,1,0,2,5,100,'bob')

    def test_compensator_shift_respects_every_state_bound(self):
        prior = prior_report()
        axes = model.scan_axes(prior,1,0,2,5,150,'alice')
        for delta in (-.04,.04):
            p = np.array(prior['fit']['parameters']); p[2] += delta
            # The selected pair is interior, but another Alice state is at a limit.
            with self.assertRaisesRegex(ValueError,'outside'):
                model.refine_voltages(prior,dict(parameters=p),axes,1,0,150,'alice')

    def test_exact_scan_endpoints_are_accepted_but_extrapolation_is_not(self):
        for sign in (-1,1):
            prior = prior_report(sign)
            for role in ('alice','bob'):
                slope = prior['fit']['parameters'][0 if role == 'alice' else 1]
                for i,j in fine.PAIRS.values():
                    axes = model.scan_axes(prior,i,j,2,5,180,role)
                    for shift in (-10.,0.,10.):
                        with self.subTest(sign=sign,eom=role,pair=(i,j),shift=shift):
                            p = np.array(prior['fit']['parameters']); p[2] -= slope*shift
                            selected = model.refine_voltages(prior,dict(parameters=p),axes,i,j,180,role)
                            self.assertAlmostEqual(selected[role+'_common_shift_v'],shift,places=10)
                            index = i if role == 'alice' else j
                            self.assertGreaterEqual(selected[role+'_v'][index],min(axes[role+'_axis']))
                            self.assertLessEqual(selected[role+'_v'][index],max(axes[role+'_axis']))
                    p = np.array(prior['fit']['parameters']); p[2] -= slope*10.0001
                    with self.assertRaisesRegex(ValueError,'outside'):
                        model.refine_voltages(prior,dict(parameters=p),axes,i,j,180,role)

    def test_loader_requires_explicit_single_eom_and_validates_fixed_axis(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = config(temp)
            runner,_ = fake_runner(cfg['prior']['fit']['parameters'])
            result = fine.acquire(cfg,Path(temp)/'scan',cfg,1000,threading.Event(),runner)
            with self.assertRaisesRegex(ValueError,'four Alice and two Bob'):
                load_sweep(result['csv'])
            with self.assertRaisesRegex(ValueError,'exactly one fixed'):
                load_sweep(result['csv'],single_eom='alice')
            self.assertEqual(load_sweep(result['csv'],single_eom='bob').counts.shape,(11,1,2))

    def test_one_folder_workflow_fresh_verification_and_scoped_transfer(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            cfg=config(temp); p=np.array(cfg['prior']['fit']['parameters']);p[2]+=.045
            runner,calls=fake_runner(p)
            output=fine.run(cfg,threading.Event(),runner)
            report=json.loads((output/'analysis.json').read_text())
            self.assertEqual(report['status'],'completed')
            self.assertEqual(len(calls),2)
            self.assertEqual(report['schema'],fine.SCHEMA)
            self.assertEqual(len(list(sweep.grid(calls[0]))),11)
            self.assertEqual(calls[0]['scan_points'],11)
            self.assertAlmostEqual(calls[0]['minimum_scan_seconds'],110.11)
            self.assertEqual(len(list(sweep.grid(calls[1]))),1)
            self.assertEqual(calls[1]['scan_points'],1)
            self.assertAlmostEqual(calls[1]['minimum_scan_seconds'],30.01)
            self.assertEqual(calls[1]['duration_ms'],30000)
            self.assertTrue(report['verification']['passed'])
            transfer=parse((output/'calibration_for_app.json').read_text())
            self.assertTrue(transfer['single_pair_check_passed'])
            self.assertFalse(transfer['independent_count_check_passed'])
            self.assertEqual(transfer['compensation_eom'],'bob')
            self.assertEqual(transfer['alice_v'],cfg['prior']['selected']['alice_v'])
            self.assertTrue((output/'fine_tuning.png').is_file())
            self.assertTrue((output/'fine_tuning.pdf').is_file())
            self.assertTrue((output/'prior_analysis.json').is_file())
            for stage in ('local_sweep','verification'):
                for path in report[stage].get('files',report[stage]).values():
                    if isinstance(path,str) and path.endswith(('.csv','.jsonl')):
                        self.assertTrue(Path(path).is_relative_to(output))
            self.assertTrue(recent_data.valid('fit_report',output/'analysis.json'))
            self.assertFalse(recent_data.valid('global_fit_report',output/'analysis.json'))
            self.assertTrue(recent_data.valid('calibration',output/'calibration_for_app.json'))
            self.assertFalse(recent_data.valid('sweep',Path(report['local_sweep']['csv'])))
            self.assertEqual(recent_data.latest('fine_tuning',{fine.TOOL:{'output':cfg['output']}},Path(temp)),output)

    def test_verification_failure_stop_and_zero_counts_never_export(self):
        for options,exception in ((dict(fail_verify=True),RuntimeError), (dict(stop_verify=True),KeyboardInterrupt), (dict(zero_verify=True),None)):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()), patch('eom_fine_figures.render'):
                cfg=config(temp);p=np.array(cfg['prior']['fit']['parameters']);p[2]+=.045
                runner,calls=fake_runner(p,**options)
                if exception:
                    with self.assertRaises(exception):fine.run(cfg,threading.Event(),runner)
                else:
                    fine.run(cfg,threading.Event(),runner)
                output=next(Path(cfg['output']).iterdir())
                report=json.loads((output/'analysis.json').read_text())
                self.assertFalse(report['transfer_available'])
                self.assertFalse((output/'calibration_for_app.json').exists())
                self.assertEqual(len(calls),2)

    def test_changed_amplitude_is_fitted_but_outside_target_rejected(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()), patch('eom_fine_figures.render'):
            cfg=config(temp);p=np.array(cfg['prior']['fit']['parameters']);p[3]*=2
            runner,calls=fake_runner(p)
            with self.assertRaisesRegex(ValueError,'fitted target lies outside'):
                fine.run(cfg,threading.Event(),runner)
            self.assertEqual(len(calls),1)
            self.assertFalse(list(Path(temp).rglob('calibration_for_app.json')))
            folder=next(Path(cfg['output']).iterdir())
            source=folder/'analysis.json';frozen=source.read_bytes()
            report=json.loads(frozen);csv_path=Path(report['local_sweep']['csv']);counts=csv_path.read_bytes()
            self.assertTrue(report['fit']['usable'])
            self.assertTrue(recent_data.valid('fine_tuning',folder))
            values=dict(gui.defaults(fine.TOOL), action=fine.VIEW, folder=str(folder), **{'plot-dpi':'72'})
            # Saved analysis must not acquire, refit, or require the old prior file.
            with patch.object(sweep,'run',side_effect=AssertionError('No acquisition')), \
                 patch.object(model,'fit_local',side_effect=AssertionError('No refit')):
                self.assertEqual(gui.execute(fine.TOOL,values,threading.Event()),0)
            self.assertEqual(source.read_bytes(),frozen)
            self.assertEqual(csv_path.read_bytes(),counts)
            self.assertEqual(len(list((folder/'analysis').rglob('plot_record.json'))),1)

    def test_low_information_counts_do_not_produce_confident_phase(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg=config(temp)
            runner,_=fake_runner(cfg['prior']['fit']['parameters'])
            result=fine.acquire(cfg,Path(temp)/'scan',cfg,1000,threading.Event(),runner)
            grid=load_sweep(result['csv'],single_eom=cfg['sweep_eom'])
            grid.counts[:]=0;grid.counts[5,0,0]=1
            fit=model.fit_local(cfg['prior'],grid)
            self.assertFalse(fit['usable'])
            self.assertFalse(fit['fit_identified'])
            grid.counts[:]=0
            with self.assertRaisesRegex(ValueError,'No photons'):model.fit_local(cfg['prior'],grid)

    def test_saved_legacy_mesh_can_still_be_redrawn_without_refitting(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            cfg = config(temp)
            folder = Path(temp)/'legacy'; folder.mkdir()
            axes = dict(alice_axis=np.linspace(-160,-140,11).tolist(),
                        bob_axis=np.linspace(-57.5,-37.5,11).tolist())
            runner,_ = fake_runner(cfg['prior']['fit']['parameters'])
            result = fine.acquire(cfg,folder/'local_sweep',axes,1000,threading.Event(),runner)
            grid = load_sweep(result['csv'])
            report = dict(schema='qkd-eom-fine-tuning-v1',status='completed',
                          parameters=dict(pair_indices=[0,1]),local_sweep=result,
                          source=grid.provenance,fit=model.fit_local(cfg['prior'],grid))
            source = folder/'analysis.json'; source.write_text(json.dumps(report))
            original = source.read_bytes()
            self.assertTrue(recent_data.valid('fine_tuning',folder))
            with patch.object(sweep,'run',side_effect=AssertionError('No acquisition')), \
                 patch.object(model,'fit_local',side_effect=AssertionError('No refit')):
                output = fine.analyze_saved(dict(folder=str(folder),plot_dpi=72))
            self.assertEqual(source.read_bytes(),original)
            self.assertTrue((output/'fine_tuning.png').is_file())
            self.assertTrue((output/'fine_tuning.pdf').is_file())

    def test_plots_use_swept_voltage_axis_for_either_eom(self):
        from eom_fine_figures import render
        for eom,role in fine.EOMS.items():
            with self.subTest(eom=role),tempfile.TemporaryDirectory() as temp:
                cfg = config(temp, **{'sweep-eom':eom})
                runner,_ = fake_runner(cfg['prior']['fit']['parameters'])
                result = fine.acquire(cfg,Path(temp)/'scan',cfg,10000,threading.Event(),runner)
                grid = load_sweep(result['csv'],single_eom=role)
                fit = model.fit_local(cfg['prior'],grid)
                report = dict(parameters=cfg,fit=fit)
                with patch('eom_fine_figures.save') as save:
                    render(grid,report,Path(temp),72)
                figure = save.call_args.args[0]
                self.assertEqual(len(figure.axes),4)
                for ax in figure.axes[:2]:
                    self.assertEqual(ax.get_xlabel(),f'{role.title()} target voltage (V)')
                # One measured fraction marker per dwell, no fabricated 2D cells.
                measured = figure.axes[0].containers[0].lines[0]
                np.testing.assert_allclose(measured.get_xdata(),cfg[role+'_axis'])

    def test_custom_hardware_sweep_uses_requested_volts_and_cleans_up(self):
        for eom in fine.EOMS:
            with self.subTest(eom=eom),tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
                cfg=config(temp, **{'sweep-eom':eom})
                cfg['ph330']['serial']='test'
                stop=FastStop();api=SweepAPI(stop);daq=FakeDAQ();constants,system=dependencies()
                result={}
                self.assertEqual(sweep.run(cfg,stop,daq=daq,api=api,constants=constants,system=system,completed_result=result),0)
                self.assertEqual(api.starts,11)
                writes = [value for event,value in daq.events if event == 'write']
                expected = [[-alice/20,-bob/20] for alice,bob in sweep.grid(cfg)]
                np.testing.assert_allclose(writes,expected+[[0.,0.]])
                self.assertEqual(daq.events[-2:],[('write',[0.,0.]),('close','ao')])
                self.assertEqual(json.loads(Path(result['manifest']).read_text())['expected_rows'],11)

    def test_gui_check_and_confirmation_never_access_hardware(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            cfg=config(temp)
            values=dict(gui.defaults(fine.TOOL), **{'fit-report':cfg['prior_source']['path']})
            with patch.object(sweep,'run') as hardware:
                self.assertEqual(gui.execute(fine.TOOL,values,threading.Event()),0)
                self.assertEqual(gui.execute(fine.TOOL,dict(values,action=fine.RUN),threading.Event()),1)
                hardware.assert_not_called()
            saved=gui.initial_settings(fine.TOOL,{sweep.TOOL:{'ch1-level-mv':'222'}})
            self.assertEqual(saved['ch1-level-mv'],'222')
            for key,val in (('allow-rejected-prior','Maybe'),('sweep-half-steps','0'),('sweep-half-steps','2.5'),('sweep-step','nan'),('sweep-eom','Both'),('verify-seconds','0.1005'),('voltage-limit','200')):
                with self.assertRaises(ValueError):fine.settings(dict(values,**{key:val}))
            old = {'mesh-step':'5','mesh-half-steps':'7','seconds':'0.1','action':'View saved result'}
            migrated = gui.initial_settings(fine.TOOL,{fine.TOOL:old})
            self.assertEqual(migrated['sweep-step'],'5')
            self.assertEqual(migrated['sweep-half-steps'],'7')
            self.assertEqual(migrated['seconds'],'0.1')
            self.assertEqual(migrated['action'],fine.VIEW)
            self.assertNotIn('mesh-step',migrated)
            self.assertEqual(old['mesh-step'],'5')
            for key in ('allow-rejected-prior','sweep-eom','sweep-step','sweep-half-steps'):
                self.assertEqual(gui.field_group(key,fine.TOOL),'Voltages')

    def test_gui_runs_and_imports_fine_result_instead_of_older_global_candidate(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(gui,'SETTINGS',Path(temp)/'gui.json'):
            cfg=config(temp);p=np.array(cfg['prior']['fit']['parameters']);p[2]+=.045
            runner,_=fake_runner(p)
            root=tk.Tk();root.withdraw();app=gui.App(root,fine.TOOL)
            try:
                app.vars['fit-report'].set(cfg['prior_source']['path'])
                app.vars['output'].set(cfg['output']);app.vars['plot-dpi'].set('72')
                app.vars['manual-ready'].set('Yes')
                app.vars['action'].set(fine.VIEW)
                self.assertEqual(str(app.fields['pair'][0].cget('state')),'disabled')
                self.assertEqual(str(app.fields['allow-rejected-prior'][0].cget('state')),'disabled')
                self.assertEqual(str(app.fields['plot-dpi'][0].cget('state')),'normal')
                app.vars['action'].set(fine.RUN)
                self.assertEqual(str(app.fields['pair'][0].cget('state')),'readonly')
                self.assertEqual(str(app.fields['allow-rejected-prior'][0].cget('state')),'readonly')
                with patch.object(sweep,'run',runner),patch.object(app,'open_plots'):
                    app.start();deadline=time.monotonic()+30
                    while app.running and time.monotonic()<deadline:
                        root.update();time.sleep(.01)
                self.assertFalse(app.running)
                self.assertEqual(app.status.get(),'Complete',app.log.get('1.0','end'))
                path=Path(app.settings[recent_data.REGISTRY]['calibration'])
                transfer=parse(path.read_text())
                app.load_calibration(gui.bb84.RANDOM)
                self.assertEqual(float(app.vars['alice-h-v'].get()),transfer['alice_v'][0])
                for tool in ('Pulsed polarization',gui.bb84.ORDERED,gui.bb84.RANDOM):
                    data=json.loads(app.settings[tool]['calibration-transfer'])
                    self.assertEqual(data['method'],'local-curve-fit-v1')
                    self.assertFalse(data['independent_count_check_passed'])
            finally:
                app.close()


if __name__ == '__main__':
    unittest.main()
