"""Bounded T3 capture while the scope pattern runs. No full AO arrays on disk."""
import ctypes as ct
import time
from pathlib import Path
import ph330_acquire as ph
from ph330 import PH330
from record_io import save_json


def capture(folder, cfg, check_running, remaining_bytes, api_factory=None):
    folder=Path(folder)/'tttr';folder.mkdir()
    record=dict(schema='qkd-marker-test-t3-v1',status='prepared',complete=False,
                records=0,flags_seen=0,config=cfg['ph330'],record_format='GenericT3',
                record_type='0x00010307',dtype='little-endian uint32',
                requested_seconds=cfg['record_seconds'],raw_byte_limit=remaining_bytes,cleanup_errors=[])
    meta=folder/'metadata.json';save_json(meta,record)
    api=None;opened=measuring=False
    index=cfg['ph330']['device_index']
    try:
        check_running()
        api=(api_factory or PH330)(cfg['dll']);ph.bind_acquisition(api)
        serial=ct.create_string_buffer(8)
        api.call('OpenDevice',index,serial);opened=True
        if serial.value.decode() != cfg['ph330']['serial']:
            raise ValueError('PicoHarp serial differs from the selected device.')
        info=ph.configure(api,cfg['ph330'])
        api.bind('SetMarkerEdges',[ct.c_int]*5);api.bind('SetMarkerHoldoffTime',[ct.c_int]*2)
        api.call('SetMarkerEdges',index,1,1,1,1)
        api.call('SetMarkerHoldoffTime',index,0)
        api.call('SetMarkerEnable',index,1,0,0,0)
        info['markers_enabled']=True
        record.update(hardware=info,dll=str(api.path),library_version=api.version)
        # NI is already running. Allow its externally triggered laser and the
        # rate meter to settle before starting the bounded T3 acquisition.
        ready=time.monotonic()+cfg['warmup_seconds']
        while time.monotonic()<ready:
            check_running();time.sleep(.02)
        before=ph.rates(api,index,info['input_count']);record['rates_before']=before
        if abs(before['sync_hz']/cfg['ph330']['laser_hz']-1)>.05:
            raise ValueError('Measured SYNC rate differs from the NI clock by more than 5%.')
        period=ph.scalar(api,index,'GetSyncPeriod',ct.c_double)
        if not period>0 or abs(period*cfg['ph330']['laser_hz']-1)>.05:
            raise ValueError('Measured SYNC period disagrees with the NI clock.')
        record['measured_sync_period_s']=period
        buffer=(ct.c_uint32*ph.TTREADMAX)();actual=ph.INT()
        check_running()
        with (folder/'events.t3raw').open('xb') as stream:
            measuring=True
            api.call('StartMeas',index,round(cfg['record_seconds']*1000))
            record['status']='recording';save_json(meta,record)
            deadline=time.monotonic()+cfg['record_seconds']+15
            empty=0
            while True:
                check_running()
                flags=ph.scalar(api,index,'GetFlags');record['flags_seen'] |= flags
                if flags & ph.BAD_FLAGS:
                    raise RuntimeError(f'PicoHarp acquisition error flags: 0x{flags:x}')
                api.call('ReadFiFo',index,buffer,ct.byref(actual))
                if not 0<=actual.value<=ph.TTREADMAX:
                    raise RuntimeError('Invalid FIFO record count.')
                if actual.value:
                    size=actual.value*4
                    if 4*record['records']+size>remaining_bytes:
                        raise RuntimeError('Raw-data size limit reached. Partial recording retained; no calibration accepted.')
                    block=ct.string_at(buffer,size)
                    if stream.write(block)!=size:
                        raise OSError('Short raw-data write.')
                    record['records']+=actual.value;empty=0
                else:
                    if ph.scalar(api,index,'CTCStatus'):
                        empty+=1
                        if empty>=6:break
                    time.sleep(.001)
                if time.monotonic()>deadline:
                    raise TimeoutError('Timed acquisition/FIFO drain did not finish.')
            record['elapsed_ms']=ph.scalar(api,index,'GetElapsedMeasTime',ct.c_double)
        record.update(status='completed',complete=True)
    except BaseException as exc:
        record.update(status='cancelled' if isinstance(exc,KeyboardInterrupt) else 'error',error=str(exc),complete=False)
        raise
    finally:
        if api is not None:
            for name,needed in (('StopMeas',measuring),('CloseDevice',opened)):
                if needed:
                    try:api.call(name,index)
                    except Exception as exc:record['cleanup_errors'].append(f'{name}: {exc}')
        if record['cleanup_errors']:
            record.update(status='cleanup_error',complete=False)
        save_json(meta,record)
    if not record['complete']:
        raise RuntimeError('PicoHarp cleanup failed; see metadata.')
    print(f"Saved {record['records']:,} T3 records ({record['records']*4/1048576:.3f} MiB).",flush=True)
    return record
