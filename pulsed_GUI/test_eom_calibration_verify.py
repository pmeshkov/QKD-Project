"""Synthetic TTTR verification at frozen fitted voltages, with no instruments."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
from unittest.mock import patch
import eom_calibration as workflow
import eom_calibration_verify as verify
import eom_model_calibration as model
import eom_calibration_core as core
from calibration_transfer import parse
from test_eom_calibration import PARAMS, fixture


def polarization(folder, selected, wrong=False, total=20000):
    folder.mkdir()
    blocks=[]
    for i in range(4):
        for j, basis in enumerate(('HV','RL')):
            raw=folder/f'{i}_{j}';raw.mkdir()
            p=selected['predicted_ch1'][i][j] if not wrong else .5
            counts=[round(total*p),total-round(total*p)]
            words=np.r_[np.full(counts[0],10 << 10,dtype='<u4'),np.full(counts[1],(1 << 25)|(10 << 10),dtype='<u4')]
            words.tofile(raw/'events.t3raw')
            meta=dict(schema='qkd-ph330-raw-t3-v1',record_type='0x00010307',record_format='GenericT3',
                      status='completed',complete=True,flags_seen=0,records=len(words),
                      hardware=dict(resolution_ps=64),measured_sync_period_s=2e-6,elapsed_ms=1000)
            (raw/'metadata.json').write_text(json.dumps(meta))
            blocks.append(dict(status='completed',alice=f'S{i}',bob=basis,raw_folder=raw.name,
                               target_eom_v=[selected['alice_v'][i],selected['bob_v'][j]]))
    session=dict(schema='qkd-static-polarization-v1',complete=True,status='completed',cleanup_errors=[],blocks=blocks,
                 settings=dict(alice={f'S{i}':v for i,v in enumerate(selected['alice_v'])},mapping=dict(HV='Unassigned',RL='Unassigned')))
    (folder/'session.json').write_text(json.dumps(session))
    return folder


class VerificationTests(unittest.TestCase):
    def test_exact_intervals_zero_counts_and_confidence(self):
        p,lo,hi=verify.exact_interval(0,0,.95)
        self.assertTrue(np.isnan(p));self.assertEqual((lo,hi),(0,1))
        self.assertEqual(verify.exact_interval(0,100,.95)[1],0)
        self.assertEqual(verify.exact_interval(100,0,.95)[2],1)
        a=verify.exact_interval(50,50,.95);b=verify.exact_interval(50,50,1-.05/8)
        self.assertLess(b[1],a[1]);self.assertGreater(b[2],a[2])

    def test_full_raw_verification_keeps_candidate_and_conditions(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(temp);csv=fixture(root/'sweep')
            values={k:d for k,_,d,_ in workflow.FORM};values.update(csv=str(csv),**{'plot-dpi':'72'})
            with patch('eom_calibration_figures.render'):
                fit_output=workflow.analyze(workflow.settings(values))
            original_path=fit_output/'analysis.json';original=original_path.read_bytes()
            fitted=json.loads(original);selected=fitted['selected']
            folder=polarization(root/'polarization',selected)
            values.update({'action':workflow.VERIFY,'fit-report':str(original_path),'verification-folder':str(folder)})
            # Independent data must never feed back into fitting or voltage choice.
            with patch.object(model,'fit_response',side_effect=AssertionError('Refitting')),patch.object(model,'choose_voltages',side_effect=AssertionError('Reselecting')):
                output=workflow.analyze(workflow.settings(values))
            report=json.loads((output/'analysis.json').read_text())
            self.assertTrue(report['independent_count_check_passed'])
            self.assertEqual(report['selected'],selected)
            self.assertIsNone(report['gate'])
            self.assertEqual(len(report['verification_file_hashes']),17)
            self.assertNotEqual(report['fit_acquisition_settings'],report['verification_acquisition_settings'])
            self.assertEqual(original_path.read_bytes(),original)
            transfer=parse((output/'calibration_for_app.json').read_text())
            self.assertTrue(transfer['independent_count_check_passed'])
            self.assertEqual(transfer['alice_v'],selected['alice_v'])
            self.assertEqual(transfer['analysis_report'],str(original_path.resolve()))
            self.assertTrue((output/'verification.png').exists())
            # Failed independent measurements remain failed, without moving voltages.
            wrong=polarization(root/'wrong',selected,wrong=True)
            values['verification-folder']=str(wrong)
            with patch('eom_calibration_figures.fractions_plot'):
                bad=workflow.analyze(workflow.settings(values))
            report=json.loads((bad/'analysis.json').read_text())
            self.assertFalse(report['independent_count_check_passed'])
            self.assertEqual(report['selected'],selected)
            # Changed voltage in the manifest is not a nearby-point validation.
            session=json.loads((folder/'session.json').read_text())
            session['blocks'][0]['target_eom_v'][0]+=.01
            (folder/'session.json').write_text(json.dumps(session))
            values['verification-folder']=str(folder)
            with self.assertRaisesRegex(ValueError,'voltages do not match'):
                workflow.analyze(workflow.settings(values))
            session['complete']=False
            (folder/'session.json').write_text(json.dumps(session))
            with self.assertRaisesRegex(ValueError,'complete polarization run'):
                workflow.analyze(workflow.settings(values))

    def test_operating_margin_and_target_checks_survive_transfer(self):
        from eom_calibration import model_transfer
        selected=dict(alice_v=[-150.,-50.,50.,150.],bob_v=[50.,-50.],
                      predicted_targets_pass=True,operating_bounds=dict(alice=[-180,180],bob=[-180,180]))
        report=dict(selected=selected,parameters=dict(csv='example.csv'),source=dict(csv_sha256='x'),
                    fit=dict(poisson_model_check_passed=False,diagnostics=['excess noise']))
        data=model_transfer(report,Path('analysis.json'))
        self.assertFalse(parse(json.dumps(data))['poisson_model_check_passed'])
        bad=copy.deepcopy(data);bad['alice_v'][0]=-190
        with self.assertRaisesRegex(ValueError,'operating margins'):parse(json.dumps(bad))
        bad=copy.deepcopy(data);bad['predicted_targets_passed']=False
        with self.assertRaisesRegex(ValueError,'fraction targets'):parse(json.dumps(bad))


if __name__=='__main__':unittest.main()
