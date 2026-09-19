"""Configure PH330 T3 and stream raw records; no DAQ or laser control.

Run --config FILE to validate offline, add --rates to configure/read count
rates, or --acquire to record. Trigger settings must be supplied explicitly.
"""
import argparse
import ctypes as ct
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

from ph330 import PH330, DEFAULT_DLL, INT, PINT, CHAR

# Verified against PicoQuant PH330Lib v2.0.0.0 ph330defin.h.
TTREADMAX = 1048576
BAD_FLAGS = 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0040 | 0x0080
FLAG_NAMES = {2: "FIFO_FULL", 4: "SYNC_LOST", 8: "REF_LOST",
              16: "SYSTEM_ERROR", 64: "COUNTS_DROPPED", 128: "SOFTWARE_ERROR"}


def validate(config, detector_count=2, expected_laser_hz=2_000_000):
    laser_hz = config.get("laser_hz")
    if not isinstance(laser_hz, (int, float)) or not math.isfinite(laser_hz) or not 1000 <= laser_hz <= 80_000_000:
        raise ValueError("laser_hz must be finite and between 1 kHz and 80 MHz.")
    if expected_laser_hz is not None and laser_hz != expected_laser_hz:
        raise ValueError("This initial acquisition profile requires laser_hz=2000000.")
    if config.get("sync_divider") != 1:
        raise ValueError("This acquisition requires sync_divider=1.")
    if type(config.get("binning")) is not int or not 0 <= config["binning"] < 24:
        raise ValueError("binning must be an integer 0..23; readback range is checked too.")
    if type(config.get("device_index")) is not int or not 0 <= config["device_index"] < 8:
        raise ValueError("device_index must be an integer 0..7.")
    if not isinstance(config.get("serial"), str) or not config["serial"]:
        raise ValueError("Set serial to the device's probe result.")
    channels = config.get("detectors")
    if not isinstance(channels, list) or len(channels) != detector_count:
        raise ValueError(f"Provide exactly {detector_count} detector configurations.")
    ids = [c.get("channel") for c in channels]
    if any(type(i) is not int or not 0 <= i < 4 for i in ids) or len(set(ids)) != detector_count:
        raise ValueError("Detector channels must be distinct integer SDK indices 0..3.")
    for trigger in [config.get("sync", {})] + channels:
        mode, level = trigger.get("mode"), trigger.get("level_mv")
        if mode not in ("edge", "cfd") or type(level) is not int:
            raise ValueError("Supply each input's measured mode and integer level_mv; null is not a setting.")
        if not -1500 <= level <= (0 if mode == "cfd" else 1500):
            raise ValueError("Trigger threshold outside PH330 range.")
        if mode == "edge" and trigger.get("edge") not in ("rising", "falling"):
            raise ValueError("Edge triggers require rising or falling.")
        zero = trigger.get("zero_cross_mv")
        if mode == "cfd" and (type(zero) is not int or not -100 <= zero <= 0):
            raise ValueError("CFD requires zero_cross_mv in -100..0.")
    return config


def bind_acquisition(api):
    signatures = {
        "Initialize": [INT]*3, "GetHardwareInfo": [INT, CHAR, CHAR, CHAR],
        "GetBaseResolution": [INT, ct.POINTER(ct.c_double), PINT],
        "GetResolution": [INT, ct.POINTER(ct.c_double)],
        "GetElapsedMeasTime": [INT, ct.POINTER(ct.c_double)],
        "GetSyncPeriod": [INT, ct.POINTER(ct.c_double)],
        "GetAllCountRates": [INT, PINT, PINT],
        "GetWarningsText": [INT, CHAR, INT],
        "ReadFiFo": [INT, ct.POINTER(ct.c_uint32), PINT],
    }
    for name in ("GetFeatures", "GetNumOfInputChannels", "GetFlags", "GetWarnings", "CTCStatus"):
        signatures[name] = [INT, PINT]
    for name in ("SetSyncDiv", "SetSyncTrgMode", "SetSyncChannelOffset", "SetBinning",
                 "SetOffset", "SetTriggerOutput", "EnableEventFilter", "SetFilterTestMode",
                 "SetOflCompression", "StartMeas"):
        signatures[name] = [INT]*2
    for name in ("SetSyncEdgeTrg", "SetSyncCFD", "SetInputTrgMode",
                 "SetInputChannelOffset", "SetInputChannelEnable", "SetSyncDeadTime"):
        signatures[name] = [INT]*3
    for name in ("SetInputEdgeTrg", "SetInputCFD", "SetMeasControl", "SetInputDeadTime"):
        signatures[name] = [INT]*4
    for name in ("SetMarkerEnable",):
        signatures[name] = [INT]*5
    signatures["StopMeas"] = [INT]
    for name, signature in signatures.items():
        api.bind(name, signature)


def scalar(api, index, name, kind=INT):
    value = kind()
    api.call(name, index, ct.byref(value))
    return value.value


def configure(api, cfg):
    index = cfg["device_index"]
    api.call("Initialize", index, 3, 0)  # T3, internal timebase (not external REF).
    model, part, version = [ct.create_string_buffer(n) for n in (24, 8, 8)]
    api.call("GetHardwareInfo", index, model, part, version)
    count = scalar(api, index, "GetNumOfInputChannels")
    if not 1 <= count <= 4 or max(d["channel"] for d in cfg["detectors"]) >= count:
        raise ValueError(f"Selected channels unavailable: hardware has {count} inputs.")
    features = scalar(api, index, "GetFeatures")
    if not features & 2:
        raise RuntimeError("Device does not report TTTR capability.")
    base, steps = ct.c_double(), INT()
    api.call("GetBaseResolution", index, ct.byref(base), ct.byref(steps))
    if cfg["binning"] >= steps.value:
        raise ValueError("Binning exceeds this device's supported range.")
    api.call("SetSyncDiv", index, 1)
    for spec in [cfg["sync"]] + cfg["detectors"]:
        sync = spec is cfg["sync"]
        prefix = "Sync" if sync else "Input"
        args = [index] if sync else [index, spec["channel"]]
        api.call(f"Set{prefix}TrgMode", *args, int(spec["mode"] == "cfd"))
        if spec["mode"] == "edge":
            api.call(f"Set{prefix}EdgeTrg", *args, spec["level_mv"], int(spec["edge"] == "rising"))
        else:
            api.call(f"Set{prefix}CFD", *args, spec["level_mv"], spec["zero_cross_mv"])
        api.call(f"Set{prefix}ChannelOffset", *args, 0)
        if features & 0x20:
            api.call(f"Set{prefix}DeadTime", *args, 0, 800)
    selected = {d["channel"] for d in cfg["detectors"]}
    for channel in range(count):
        api.call("SetInputChannelEnable", index, channel, int(channel in selected))
    api.call("SetBinning", index, cfg["binning"])
    api.call("SetOffset", index, 0)
    api.call("SetMeasControl", index, 0, 0, 0)
    api.call("SetMarkerEnable", index, 0, 0, 0, 0)
    api.call("SetOflCompression", index, 0)
    if features & 0x10:
        api.call("SetTriggerOutput", index, 0)
    if features & 0x100:
        api.call("EnableEventFilter", index, 0)
        api.call("SetFilterTestMode", index, 0)
    resolution = scalar(api, index, "GetResolution", ct.c_double)
    if not math.isfinite(resolution) or resolution <= 0 or resolution * 32768 < 1e12 / cfg["laser_hz"]:
        raise ValueError("T3 microtime range does not cover the laser period; increase binning.")
    return {"model": model.value.decode(), "part": part.value.decode(),
            "hardware_version": version.value.decode(), "input_count": count,
            "features": features, "base_resolution_ps": base.value,
            "resolution_ps": resolution, "channel_offsets_ps": 0,
            "histogram_offset_ns": 0, "markers_enabled": False,
            "event_filter_enabled": False, "extended_deadtime_enabled": False}


def rates(api, index, count):
    sync, inputs = INT(), (INT*count)()
    api.call("GetAllCountRates", index, ct.byref(sync), inputs)
    warnings = scalar(api, index, "GetWarnings")
    text = ct.create_string_buffer(16384)
    api.call("GetWarningsText", index, text, warnings)
    return {"host_utc": datetime.now(timezone.utc).isoformat(), "sync_hz": sync.value,
            "input_hz": list(inputs), "warnings": warnings,
            "warnings_text": text.value.decode(errors="replace")}


def drain(api, index, stream, record, duration_s, stop_event=None):
    """Read until CTC completion plus six consecutive empty reads, as in SDK."""
    buffer, actual = (ct.c_uint32*TTREADMAX)(), INT()
    empty = 0
    deadline = time.monotonic() + duration_s + 15
    progress_at = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt
        flags = scalar(api, index, "GetFlags")
        record["flags_seen"] |= flags
        if flags & BAD_FLAGS:
            raise RuntimeError("Invalid TTTR run: " + ", ".join(
                name for flag, name in FLAG_NAMES.items() if flags & flag))
        api.call("ReadFiFo", index, buffer, ct.byref(actual))
        if not 0 <= actual.value <= TTREADMAX:
            raise RuntimeError("Invalid DLL FIFO record count.")
        if actual.value:
            block = ct.string_at(buffer, actual.value*4)
            if stream.write(block) != len(block):
                raise OSError("Short write while saving TTTR data.")
            record["records"] += actual.value
            empty = 0
        else:
            if scalar(api, index, "CTCStatus"):
                empty += 1
                if empty >= 6:
                    break
            time.sleep(0.001)
        now = time.monotonic()
        if now > deadline:
            raise TimeoutError("CTC/FIFO drain exceeded acquisition duration plus 15 seconds.")
        if now >= progress_at:
            print(f"Saved {record['records']:,} raw records", flush=True)
            progress_at = now + 2


def run(api, cfg, seconds, destination, acquire, stop_event=None):
    bind_acquisition(api)
    index, serial = cfg["device_index"], ct.create_string_buffer(8)
    api.call("OpenDevice", index, serial)
    try:
        if serial.value.decode() != cfg["serial"]:
            raise RuntimeError("Device serial differs from config; re-run the probe.")
        info = configure(api, cfg)
        time.sleep(0.2)
        before = rates(api, index, info["input_count"])
        print(json.dumps({"hardware": info, "rates": before}, indent=2))
        if not acquire:
            return None
        if abs(before["sync_hz"] / cfg["laser_hz"] - 1) > 0.05:
            raise RuntimeError(f"SYNC rate is not within 5% of {cfg['laser_hz']:g} Hz; check laser and trigger settings.")
        if any(before["input_hz"][d["channel"]] <= 0 for d in cfg["detectors"]):
            raise RuntimeError("Each selected detector must have a nonzero rate before acquisition.")
        destination.mkdir(parents=True, exist_ok=True)
        folder = destination / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        folder.mkdir()
        meta = folder / "metadata.json"
        record = {"schema": "qkd-ph330-raw-t3-v1", "status": "prepared",
                  "config": cfg, "hardware": info, "rates_before": before,
                  "dll": str(api.path), "library_version": api.version,
                  "requested_duration_ms": round(seconds*1000), "records": 0,
                  "flags_seen": 0, "complete": False,
                  "record_format": "GenericT3", "record_type": "0x00010307",
                  "dtype": "little-endian uint32", "raw_file": "events.t3raw",
                  "note": "Not PTU. Raw records plus this JSON are required together."}

        def save():
            temp = meta.with_suffix(".tmp")
            temp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            temp.replace(meta)

        save()
        try:
            with (folder / "events.t3raw").open("xb") as stream:
                if stop_event is not None and stop_event.is_set():
                    raise KeyboardInterrupt
                api.call("StartMeas", index, record["requested_duration_ms"])
                try:
                    record["status"] = "running"
                    record["host_start_utc"] = datetime.now(timezone.utc).isoformat()
                    save()
                    # Only meaningful during acquisition, after >=2 sync periods.
                    time.sleep(0.001)
                    period = scalar(api, index, "GetSyncPeriod", ct.c_double)
                    if not math.isfinite(period) or abs(period*cfg["laser_hz"]-1) > 0.05:
                        raise RuntimeError("Measured SYNC period differs from the configured laser period.")
                    record["measured_sync_period_s"] = period
                    drain(api, index, stream, record, seconds, stop_event=stop_event)
                    record["elapsed_ms"] = scalar(api, index, "GetElapsedMeasTime", ct.c_double)
                finally:
                    api.call("StopMeas", index)
            final_flags = scalar(api, index, "GetFlags")
            record["flags_seen"] |= final_flags
            if final_flags & BAD_FLAGS:
                raise RuntimeError(f"Final device flags indicate invalid data: {final_flags:#x}")
            record["rates_after"] = rates(api, index, info["input_count"])
            if record["records"] == 0:
                raise RuntimeError("Empty TTTR run.")
            record.update(status="completed", complete=True)
        except BaseException as exc:
            record.update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                          error=str(exc), complete=False)
            raise
        finally:
            record["host_end_utc"] = datetime.now(timezone.utc).isoformat()
            save()
            print(f"Acquisition record: {meta.resolve()}")
        return folder
    finally:
        api.call("CloseDevice", index)


def main(argv=None, stop_event=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dll", default=DEFAULT_DLL, type=Path)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "runs" / "ph330")
    parser.add_argument("--preview", action="store_true",
                        help="After acquisition, save diagnostic plots and a data check report")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--rates", action="store_true")
    action.add_argument("--acquire", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = validate(json.loads(args.config.read_text(encoding="utf-8")))
        if not math.isfinite(args.seconds) or not 0.1 <= args.seconds <= 360000:
            raise ValueError("Duration must be 0.1 seconds through 100 hours.")
        if not (args.rates or args.acquire):
            print("Configuration valid. No hardware accessed. Use --rates before --acquire.")
            return 0
        if args.preview and not args.acquire:
            raise ValueError("--preview requires --acquire; use ph330_preview.py for an existing run.")
        folder = run(PH330(args.dll), cfg, args.seconds, args.output, args.acquire,
                     stop_event=stop_event)
        if args.preview:
            try:
                from ph330_preview import preview
                preview(folder)
            except Exception as exc:
                print(f"Raw acquisition saved at {folder}. Preview failed: {exc}")
                return 2
        return 0
    except KeyboardInterrupt:
        print("Interrupted; any saved acquisition is marked incomplete.")
        return 130
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"PH330: {exc}")
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch("G2 acquisition")
