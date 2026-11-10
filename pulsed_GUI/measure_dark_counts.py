"""Repeated two-detector dark-count windows; PicoHarp only, no laser or DAQ control."""
import csv
import ctypes as ct
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import eom_voltage_sweep as sweep
import ph330_acquire as ph
from ph330 import PH330
from record_io import save_json

TOOL = 'Measure Dark Counts'
HEADER = ['Interval_Index', 'Interval_Start_UTC', 'Start_Elapsed_s',
          'Measured_Interval_s', 'CH1_Counts', 'CH2_Counts']
HELP = ('Shutter the laser, then select Laser shuttered = Yes before Record. '
        'Records both detector counts in each user-defined interval using PicoHarp T2; no SYNC required. '
        'Intervals have short readout/restart gaps; those gaps are not counted. '
        'No EOM/laser outputs are changed. CSV counts are per interval, not cumulative. '
        'Stop preserves completed intervals. No analysis or plots.')
FORM = [
    ('action', 'Action', 'Check settings', ['Check settings', 'Record']),
    ('laser-shuttered', 'Laser shuttered — confirm before recording', 'No', ['No', 'Yes']),
    ('seconds', 'Counting interval duration (s)', '1', None),
    ('intervals', 'Number of counting intervals', '60', None),
    *[item for item in sweep.FORM if item[0] in ('dll', 'device-index', 'serial', 'note')
      or item[0].startswith(('ch1-', 'ch2-'))],
    ('output', 'Dark-count CSV runs directory', str(sweep.DATA.parent/'dark_counts'), 'directory'),
]


def settings(values):
    if values.get('action') not in ('Check settings', 'Record'):
        raise ValueError('Select Check settings or Record.')
    if values.get('laser-shuttered') not in ('No', 'Yes'):
        raise ValueError('Select Yes or No for Laser shuttered.')
    intervals = int(values['intervals'])
    if not 1 <= intervals <= 1_000_000:
        raise ValueError('Number of intervals must be between 1 and 1000000.')
    # Reuse detector/dwell validation. T2 ignores the internal SYNC/binning defaults.
    defaults = {key: value for key, _, value, _ in sweep.FORM}
    defaults.update(values)
    defaults.update(source=sweep.CW, points='2')
    validated = sweep.settings(defaults)
    return dict(mode=2, duration_ms=validated['duration_ms'], intervals=intervals,
                ph330=validated['ph330'], dll=validated['dll'], output=validated['output'],
                note=validated['note'], laser_shuttered=values['laser-shuttered'] == 'Yes',
                total_collection_seconds=intervals*validated['duration_ms']/1000)


def run(cfg, stop, *, api=None):
    if stop.is_set():
        return 130
    if cfg.get('laser_shuttered') is not True:
        raise ValueError('Shutter the laser and set Laser shuttered to Yes before recording.')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')
    folder = Path(cfg['output'])/run_id
    folder.mkdir(parents=True, exist_ok=False)
    partial = folder/f'{run_id}_Dark_Counts.partial.csv'
    complete = folder/f'{run_id}_Dark_Counts.csv'
    manifest = folder/'measurement.json'
    record = dict(schema='qkd-dark-counts-v1', settings=cfg, status='preparing', complete=False,
                  rows=0, expected_rows=cfg['intervals'], csv=partial.name, csv_columns=HEADER,
                  count_meaning='Integer photons per detector within each completed interval; excludes readout/restart gaps.',
                  shutter_confirmation='User confirmation; no electronic shutter readback.', cleanup_errors=[])
    save_json(manifest, record)
    print(f'Run record: {manifest.resolve()}', flush=True)
    opened = False
    code = 0
    index = cfg['ph330']['device_index']
    try:
        with partial.open('x', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(HEADER)
            stream.flush()
            api = api if api is not None else PH330(Path(cfg['dll']))
            ph.bind_acquisition(api)
            serial = ct.create_string_buffer(8)
            api.call('OpenDevice', index, serial)
            opened = True
            if serial.value.decode() != cfg['ph330']['serial']:
                raise RuntimeError('PicoHarp serial differs from configured device.')
            record['hardware'] = ph.configure(api, cfg['ph330'], tttr_mode=2)
            record.update(dll_version=api.version, status='running')
            save_json(manifest, record)
            buffer = (ct.c_uint32*ph.TTREADMAX)()
            start = time.perf_counter()
            for interval in range(1, cfg['intervals']+1):
                if stop.is_set():
                    raise KeyboardInterrupt
                timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
                elapsed = time.perf_counter()-start
                counts, seconds, _, flags = sweep.collect(api, index, cfg, stop, buffer)
                writer.writerow([interval, timestamp, f'{elapsed:.6f}', f'{seconds:.9f}', *counts])
                stream.flush()
                record['rows'] += 1
                record['flags_seen'] = record.get('flags_seen', 0) | flags
                print(f'{interval}/{cfg["intervals"]} | {seconds:g} s | CH1 {counts[0]}, CH2 {counts[1]} counts', flush=True)
            record['status'] = 'completed'
    except KeyboardInterrupt:
        code = 130
        record['status'] = 'interrupted'
    except Exception as exc:
        code = 1
        record.update(status='error', error=str(exc))
        print(f'Dark-count measurement failed: {exc}', flush=True)
    finally:
        if opened:
            try:
                api.call('CloseDevice', index)
            except Exception as exc:
                code = 1
                record.update(status='cleanup_error')
                record['cleanup_errors'].append(f'close PicoHarp: {exc}')
        if code == 0:
            try:
                partial.rename(complete)
                record.update(csv=complete.name, complete=True)
            except Exception as exc:
                code = 1
                record.update(status='error', error=f'Finalize CSV: {exc}')
        record['host_end_utc'] = datetime.now(timezone.utc).isoformat()
        save_json(manifest, record)
        print(f'CSV: {(folder/record["csv"]).resolve()}', flush=True)
        print(f'Run record: {manifest.resolve()}', flush=True)
    return code


def main(values, stop_event=None):
    stop = stop_event if stop_event is not None else threading.Event()
    try:
        if stop.is_set():
            return 130
        cfg = settings(values)
        print(f'{cfg["intervals"]} intervals × {cfg["duration_ms"]/1000:g} s = '
              f'{cfg["total_collection_seconds"]:g} s collection, plus readout/restart gaps.', flush=True)
        if values['action'] == 'Check settings':
            print('Settings valid; no hardware accessed. Recording requires Laser shuttered = Yes.')
            return 0
        return run(cfg, stop)
    except Exception as exc:
        print(f'{TOOL}: {exc}', flush=True)
        return 1


if __name__ == '__main__':
    from gui_app import launch
    launch(TOOL)
