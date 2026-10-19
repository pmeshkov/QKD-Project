"""Offline history selection, phase conventions and timestamp provenance."""
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import pandas as pd

import calibration_history as history


def report(folder, name, source='20261012_183431_784007', **updates):
    path=Path(folder)/name/'analysis.json';path.parent.mkdir(parents=True)
    data=dict(schema=history.SCHEMA,status='completed',source=dict(csv=source+'_Detector_Traces.csv'),
              fit=dict(parameter_names=['k_alice_rad_per_v','k_bob_rad_per_v','phase_rad'],
                       parameters=[np.pi/400,-np.pi/380,1.2],vpi_alice_v=200.,vpi_bob_v=190.,
                       fit_identified=True,poisson_model_check_passed=True,diagnostics=[]))
    data.update(updates);path.write_text(json.dumps(data));return path


class HistoryTests(unittest.TestCase):
    def test_all_reports_and_latest_per_sweep_have_distinct_meanings(self):
        with tempfile.TemporaryDirectory() as temp:
            report(temp,'20261012T183631_082594Z')
            latest=report(temp,'20261012T183725_525911Z')
            report(temp,'20261012T190000_000000Z',source='20261012_185000_000000')
            all_rows,excluded=history.load_history(temp)
            self.assertEqual(len(all_rows),3);self.assertTrue(excluded.empty)
            selected,missing=history.select_history(all_rows,latest_per_sweep=True,time_axis='sweep')
            self.assertEqual(len(selected),2);self.assertEqual(missing,0)
            self.assertIn(str(latest.resolve()),selected.analysis_file.tolist())
            self.assertEqual(selected.iloc[0].plot_time_utc,pd.Timestamp('2026-10-12T18:34:31.784007Z'))
            np.testing.assert_allclose(selected.phase_rad,1.2)
            np.testing.assert_allclose(selected.static_retardance_rad,2.4)
            np.testing.assert_allclose(selected.static_retardance_deg,np.degrees(2.4))

    def test_legacy_fine_incomplete_malformed_and_unknown_timestamp_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            report(temp,'20261012T183631_082594Z',schema='qkd-eom-fine-tuning-v1')
            report(temp,'20261012T183632_082594Z',schema='qkd-eom-calibration-analysis-v1')
            report(temp,'20261012T183633_082594Z',status='error')
            report(temp,'no_timestamp')
            path=report(temp,'20261012T183634_082594Z');path.write_text('{')
            data,excluded=history.load_history(temp)
            self.assertTrue(data.empty);self.assertEqual(len(excluded),5)
            with self.assertRaisesRegex(ValueError,'No completed'):history.select_history(data)

    def test_flagged_fits_stay_visible_and_plots_have_three_axes(self):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        with tempfile.TemporaryDirectory() as temp:
            path=report(temp,'20261012T183631_082594Z')
            raw=json.loads(path.read_text());raw['fit']['poisson_model_check_passed']=False
            raw['fit']['diagnostics']=['Model mismatch'];path.write_text(json.dumps(raw))
            data,_=history.load_history(temp);selected,_=history.select_history(data)
            self.assertTrue(selected.flagged.all())
            with self.assertRaisesRegex(ValueError,'No results'):history.select_history(data,only_validated=True)
            fig=history.plot_history(selected,phase_display='retardance_deg')
            self.assertEqual(len(fig.axes),3)
            self.assertIn('2φ',fig.axes[2].get_ylabel())
            plt.close(fig)


if __name__=='__main__':unittest.main()
