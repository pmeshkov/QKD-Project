"""Offline dark-count acquisition, confirmation and partial CSV checks."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import gui_app as gui
import measure_dark_counts as dark
from test_eom_voltage_sweep import FastStop, SweepAPI


def values(**extra):
    result = gui.defaults(dark.TOOL)
    result.update(serial='test', intervals='3', **{'laser-shuttered':'Yes'})
    result.update(extra)
    return result


class DarkCountTests(unittest.TestCase):
    def acquire(self, temp, fail=None):
        cfg = dark.settings(values(output=temp))
        stop = FastStop(); api = SweepAPI(stop,fail)
        with contextlib.redirect_stdout(io.StringIO()):
            code = dark.run(cfg,stop,api=api)
        manifest = next(Path(temp).rglob('measurement.json'))
        record = json.loads(manifest.read_text())
        with (manifest.parent/record['csv']).open(newline='') as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames,dark.HEADER)
            rows = list(reader)
        return code,record,rows,api

    def test_records_per_interval_counts_and_actual_exposure_without_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            code,record,rows,api = self.acquire(temp)
            self.assertEqual(code,0)
            self.assertTrue(record['complete'])
            self.assertEqual([int(r['Interval_Index']) for r in rows],[1,2,3])
            self.assertEqual([int(r['CH1_Counts']) for r in rows],[2,2,2])
            self.assertEqual([int(r['CH2_Counts']) for r in rows],[1,1,1])
            self.assertEqual([float(r['Measured_Interval_s']) for r in rows],[.1]*3)
            self.assertTrue(all(r['Interval_Start_UTC'].endswith('+00:00') for r in rows))
            self.assertEqual(api.initialized_mode,2)
            self.assertEqual(api.starts,3)
            self.assertEqual(api.calls[-1],'CloseDevice')
            self.assertNotIn('SetBinning',api.calls)
            self.assertFalse(list(Path(temp).rglob('*.png')))

    def test_stop_and_fifo_error_keep_completed_intervals(self):
        for fail,expected in (('stop',130),('fifo',1)):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as temp:
                code,record,rows,api = self.acquire(temp,fail)
                self.assertEqual(code,expected)
                self.assertFalse(record['complete'])
                self.assertTrue(record['csv'].endswith('.partial.csv'))
                self.assertEqual(len(rows),1)
                self.assertEqual(record['rows'],1)
                self.assertEqual(api.calls[-1],'CloseDevice')

    def test_confirmation_is_required_before_hardware_or_output(self):
        with tempfile.TemporaryDirectory() as temp,patch.object(dark,'PH330') as hardware,contextlib.redirect_stdout(io.StringIO()):
            opts = values(output=temp,action='Record',**{'laser-shuttered':'No'})
            self.assertEqual(gui.execute(dark.TOOL,opts,threading.Event()),1)
            hardware.assert_not_called()
            self.assertEqual(list(Path(temp).iterdir()),[])
            self.assertEqual(gui.execute(dark.TOOL,dict(opts,action='Check settings'),threading.Event()),0)
            hardware.assert_not_called()
            stop = threading.Event(); stop.set()
            self.assertEqual(gui.execute(dark.TOOL,values(action='Record'),stop),130)
            hardware.assert_not_called()

    def test_validation_and_profile_confirmation_reset(self):
        for key,value in (('seconds','0'),('seconds','nan'),('seconds','0.1005'),('intervals','0'),
                          ('intervals','2.5'),('intervals','1000001'),('laser-shuttered','Maybe')):
            with self.subTest(key=key),self.assertRaises(ValueError):
                dark.settings(values(**{key:value}))
        cfg = dark.settings(values(seconds='2',intervals='5'))
        self.assertEqual(cfg['total_collection_seconds'],10)
        self.assertEqual(gui.field_group('laser-shuttered',dark.TOOL),'Run')
        saved = gui.initial_settings(dark.TOOL,{dark.TOOL:values()})
        self.assertEqual(saved['laser-shuttered'],'No')
        self.assertEqual(gui.defaults(dark.TOOL)['laser-shuttered'],'No')

    def test_zero_counts_are_saved_as_zero(self):
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            cfg = dark.settings(values(output=temp,intervals='1'))
            stop = FastStop(); api = SweepAPI(stop)
            with patch.object(dark.sweep,'collect',return_value=([0,0],1.,0,0)):
                self.assertEqual(dark.run(cfg,stop,api=api),0)
            path = next(Path(temp).rglob('*_Dark_Counts.csv'))
            with path.open(newline='') as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual((row['CH1_Counts'],row['CH2_Counts']),('0','0'))


if __name__ == '__main__':
    unittest.main()
