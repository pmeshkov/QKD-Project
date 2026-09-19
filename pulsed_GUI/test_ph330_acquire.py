"""Offline acquisition failure-path checks; never access a physical device."""
import contextlib
import ctypes as ct
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ph330_acquire as acq


def config():
    # Synthetic thresholds for software tests, not a bench calibration.
    return {"laser_hz": 2_000_000, "sync_divider": 1, "binning": 6,
            "device_index": 0, "serial": "test",
            "sync": {"mode": "edge", "level_mv": -200, "edge": "falling"},
            "detectors": [{"channel": i, "mode": "edge", "level_mv": 200,
                           "edge": "rising"} for i in (0, 1)]}


class FakeAPI:
    path = Path("fake.dll")
    version = "2.0"

    def __init__(self, blocks=(), flags=0):
        self.blocks = iter(blocks)
        self.flags = flags
        self.calls = []
        self.signatures = {}

    def bind(self, name, signature):
        self.signatures[name] = signature

    def call(self, name, *args):
        if name in self.signatures and len(args) != len(self.signatures[name]):
            raise TypeError(f"{name}: wrong argument count")
        self.calls.append(name)
        if name == "OpenDevice":
            args[1].value = b"test"
        elif name == "GetFlags":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = self.flags
        elif name == "CTCStatus":
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 1
        elif name == "GetSyncPeriod":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 500e-9
        elif name == "GetElapsedMeasTime":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 1000
        elif name in ("GetNumOfInputChannels", "GetFeatures"):
            ct.cast(args[1], ct.POINTER(ct.c_int))[0] = 2 if name == "GetNumOfInputChannels" else 0x3FF
        elif name == "GetBaseResolution":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 1
            ct.cast(args[2], ct.POINTER(ct.c_int))[0] = 24
        elif name == "GetResolution":
            ct.cast(args[1], ct.POINTER(ct.c_double))[0] = 64
        elif name == "ReadFiFo":
            block = next(self.blocks, [])
            for i, value in enumerate(block):
                args[1][i] = value
            ct.cast(args[2], ct.POINTER(ct.c_int))[0] = len(block)
        return 0


class AcquisitionTests(unittest.TestCase):
    def test_full_configuration_call_signatures(self):
        for mode in ("edge", "cfd"):
            api = FakeAPI()
            cfg = config()
            if mode == "cfd":
                for trigger in [cfg["sync"]] + cfg["detectors"]:
                    trigger.update(mode="cfd", level_mv=-200, zero_cross_mv=-10)
            acq.bind_acquisition(api)
            result = acq.configure(api, cfg)
            self.assertEqual(result["input_count"], 2)
            self.assertIn("SetSyncDeadTime", api.calls)
            self.assertEqual(api.calls.count("SetInputDeadTime"), 2)

    def test_template_cannot_acquire_with_unknown_levels(self):
        template = Path(__file__).with_name("ph330_g2.template.json")
        with self.assertRaisesRegex(ValueError, "null"):
            acq.validate(json.loads(template.read_text()))

    def test_duplicate_channels_rejected(self):
        cfg = config()
        cfg["detectors"][1]["channel"] = 0
        with self.assertRaises(ValueError):
            acq.validate(cfg)

    def test_cfd_requires_negative_level_and_zero_cross(self):
        cfg = config()
        cfg["sync"] = {"mode": "cfd", "level_mv": -200, "zero_cross_mv": -10}
        acq.validate(cfg)
        cfg["sync"]["level_mv"] = 200
        with self.assertRaises(ValueError):
            acq.validate(cfg)

    def test_late_fifo_records_and_exact_bytes_preserved(self):
        api = FakeAPI([[0x12345678], [], [], [0xFFFFFFFF]])
        record = {"records": 0, "flags_seen": 0}
        output = io.BytesIO()
        with patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            acq.drain(api, 0, output, record, 1)
        self.assertEqual(output.getvalue(), b"\x78\x56\x34\x12\xff\xff\xff\xff")
        self.assertEqual(record["records"], 2)
        self.assertEqual(api.calls.count("ReadFiFo"), 10)

    def test_data_loss_flags_abort(self):
        for flag in acq.FLAG_NAMES:
            with self.subTest(flag=flag), self.assertRaises(RuntimeError):
                acq.drain(FakeAPI(flags=flag), 0, io.BytesIO(),
                          {"records": 0, "flags_seen": 0}, 1)

    def test_stop_close_and_incomplete_record_on_interrupt(self):
        api = FakeAPI()
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(acq, "configure", return_value={"input_count": 2}), \
                patch.object(acq, "rates", return_value={"sync_hz": 2_000_000, "input_hz": [100, 100]}), \
                patch.object(acq, "drain", side_effect=KeyboardInterrupt), \
                patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                acq.run(api, config(), 1, Path(tmp), True)
            metadata = json.loads(next(Path(tmp).glob("*/metadata.json")).read_text())
        self.assertEqual(metadata["status"], "interrupted")
        self.assertFalse(metadata["complete"])
        self.assertEqual(api.calls[-2:], ["StopMeas", "CloseDevice"])

    def test_complete_run_drains_and_closes(self):
        api = FakeAPI([[123]])
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(acq, "configure", return_value={"input_count": 2}), \
                patch.object(acq, "rates", return_value={"sync_hz": 2_000_000, "input_hz": [100, 100]}), \
                patch.object(acq.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            folder = acq.run(api, config(), 1, Path(tmp), True)
            metadata = json.loads((folder / "metadata.json").read_text())
            self.assertEqual((folder / "events.t3raw").stat().st_size, 4)
        self.assertTrue(metadata["complete"])
        self.assertIn("StopMeas", api.calls)
        self.assertEqual(api.calls[-1], "CloseDevice")

    def test_default_validation_never_loads_dll(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            path.write_text(json.dumps(config()))
            with patch.object(acq, "PH330") as factory, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acq.main(["--config", str(path)]), 0)
                factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
