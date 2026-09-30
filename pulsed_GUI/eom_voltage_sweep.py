"""Notebook-compatible static EOM sweep with timed PicoHarp photon counting.

CW uses T2; pulsed illumination uses T3. Records are counted in bounded FIFO
blocks and discarded, not saved as a large timestamp file. No pulse index is
claimed. The complete scan preserves the original notebook's exact grid/order.
"""
import csv
import ctypes as ct
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from figure_options import FIELD as DPI_FIELD, validated_dpi
from measurement_metadata import FORM as MEASUREMENT_FORM, read as measurement_context
import threading
import time

import numpy as np

from laser_clock import clock_plan
from laser_clock_eom import prepared_clock
from ph330 import PH330
import ph330_acquire as ph
from pulsed_polarization import FORM as POL_FORM, INTERNAL_SOURCES
from record_io import save_json

TOOL = "EOM calibration sweep"
CW = "Laser CW (manual)"
NI = "NI external"
HEADER = ["Unix_Timestamp", "Elapsed_Time_s", "EOM0_Target_V", "EOM1_Target_V",
          "Raw_Count0", "Raw_Count1", "Rate0_Hz", "Rate1_Hz"]
DATA = Path(__file__).resolve().parents[1] / "data" / "eoms"
HELP = ("N × N grid, -200 to +200 V on both EOMs; Alice varies fastest. Set the notebook's N to the same value. "
        "Actual photon counts per timed dwell. CW uses T2; pulsed modes use T3. "
        "No arrival gate or timestamp file. Use only a completed N²-row Detector_Traces.csv in the notebook. "
        "Close UniHarp and other AO/clock tools. Stop saves partial data and zeroes both outputs.")
FORM = [
    ("action", "Action", "Check settings", ["Check settings", "Run sweep"]),
    ("points", "N — voltage values per EOM (2–1000; set notebook N to match)", "60", None),
    ("seconds", "Photon collection time at EACH voltage pair (s)", "0.1", None),
    ("settle-ms", "Wait after voltage change, before counting (ms)", "10", None),
    ("source", "Illumination / laser source", CW, [CW, NI, *INTERNAL_SOURCES]),
    ("laser-hz", "NI external laser rate (Hz; external mode only)", "500000", None),
    ("high-ns", "NI trigger width (ns; external mode only)", "100", None),
    ("manual-ready", "Manual mode: PFI12 disconnected and selected laser mode set", "No", ["No", "Yes"]),
    ("device", "NI USB-6351 device (AO0 Alice, AO1 Bob)", "Dev1", None),
    *[item for item in POL_FORM if item[0] in ("dll", "device-index", "serial", "binning")
      or item[0].startswith(("sync-", "ch1-", "ch2-"))],
    ("output", "Calibration CSV directory", str(DATA), "directory"),
    DPI_FIELD,
    *MEASUREMENT_FORM,
    ("note", "Source / power / emitter / calibration notes", "", None),
]


def settings(values):
    def number(key, low, high):
        value = float(values[key])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be between {low:g} and {high:g}.")
        return value
    source = values["source"]
    if source not in (CW, NI, *INTERNAL_SOURCES):
        raise ValueError("Select CW, an internal pulsed rate, or NI external.")
    device = values["device"].strip()
    if not device or any(c in device for c in "/\\,:"):
        raise ValueError("Enter a NI device name such as Dev1.")
    try:
        points = int(values.get("points", "60"))
    except (ValueError, TypeError):
        raise ValueError("N must be a whole number from 2 to 1000.") from None
    if not 2 <= points <= 1000:
        raise ValueError("N must be a whole number from 2 to 1000.")
    duration = number("seconds", .001, 3600)
    duration_ms = round(duration * 1000)
    if not math.isclose(duration * 1000, duration_ms, abs_tol=1e-6):
        raise ValueError("Collection time must be a whole number of milliseconds.")
    clock = None
    hz = INTERNAL_SOURCES.get(source, 2_000_000)  # CW placeholder for shared validator only.
    if source == NI:
        clock = clock_plan(number("laser-hz", 1000, 1_000_000), number("high-ns", 20, 300000))
        hz = clock["realized_nominal_frequency_hz"]
    def trigger(prefix):
        value = dict(mode=values[prefix + "-mode"], edge=values[prefix + "-edge"],
                     level_mv=int(values[prefix + "-level-mv"]),
                     offset_ps=round(number(prefix + "-offset-ns", -99, 99) * 1000))
        if value["mode"] == "cfd":
            value["zero_cross_mv"] = int(values[prefix + "-zero"])
        return value
    config = dict(device_index=int(values["device-index"]), serial=values["serial"].strip(),
                  laser_hz=hz, sync_divider=1, binning=int(values["binning"]), sync=trigger("sync"),
                  detectors=[dict(trigger("ch1"), channel=0), dict(trigger("ch2"), channel=1)])
    ph.validate(config, expected_laser_hz=None)
    if not values["output"].strip():
        raise ValueError("Select an output directory.")
    return dict(source=source, device=device, clock=clock, ph330=config, dll=values["dll"],
                mode=2 if source == CW else 3, duration_ms=duration_ms,
                settle_s=number("settle-ms", 1, 60000) / 1000,
                output=values["output"], note=values["note"], measurement=measurement_context(values), points=points, plot_dpi=validated_dpi(values.get("plot-dpi", "300")),
                voltage_min=-200., voltage_max=200.,
                minimum_scan_seconds=points**2 * (duration_ms / 1000 + float(values["settle-ms"]) / 1000))


def grid(cfg):
    levels = np.linspace(cfg["voltage_min"], cfg["voltage_max"], cfg["points"])
    for bob in levels:
        for alice in levels:
            yield float(alice), float(bob)


def collect(api, index, cfg, stop, buffer, clock=None):
    """One hardware-CTC dwell. Never turn live rate-meter estimates into counts."""
    counts = np.zeros(2, dtype=np.int64)
    actual = ct.c_int()
    seen_flags = records = empty = 0
    deadline = time.monotonic() + cfg["duration_ms"] / 1000 + 15
    api.call("StartMeas", index, cfg["duration_ms"])
    try:
        while True:
            if stop.is_set():
                raise KeyboardInterrupt
            if clock is not None and clock.is_task_done():
                raise RuntimeError("NI laser counter stopped during the sweep.")
            flags = ph.scalar(api, index, "GetFlags")
            seen_flags |= flags
            bad = ph.BAD_FLAGS if cfg["mode"] == 3 else ph.BAD_FLAGS & ~4  # No SYNC required in T2.
            if flags & bad:
                raise RuntimeError(f"PicoHarp data-loss/system flags: 0x{flags:x}; point discarded.")
            api.call("ReadFiFo", index, buffer, ct.byref(actual))
            if not 0 <= actual.value <= ph.TTREADMAX:
                raise RuntimeError("Invalid PicoHarp FIFO record count.")
            if actual.value:
                words = np.ctypeslib.as_array(buffer)[:actual.value]
                channels = (words >> 25) & 63
                regular = (words >> 31) == 0
                if np.any(regular & (channels > 1)):
                    raise RuntimeError("Unexpected detector channel in FIFO.")
                counts += [np.count_nonzero(regular & (channels == ch)) for ch in (0, 1)]
                records += actual.value
                empty = 0
            elif ph.scalar(api, index, "CTCStatus"):
                empty += 1
                if empty >= 6:
                    break
            if time.monotonic() > deadline:
                raise TimeoutError("PicoHarp dwell/FIFO drain exceeded its deadline.")
            if not actual.value:
                stop.wait(.001)
        elapsed_ms = ph.scalar(api, index, "GetElapsedMeasTime", ct.c_double)
        if not math.isfinite(elapsed_ms) or elapsed_ms <= 0:
            raise RuntimeError("Invalid PicoHarp measured integration time.")
        return [int(x) for x in counts], elapsed_ms / 1000, records, seen_flags
    finally:
        api.call("StopMeas", index)


def plot_completed(csv_path, cfg):
    """Display both measured rate surfaces; no fitting or changes to the CSV."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    data = np.loadtxt(csv_path, delimiter=",", skiprows=1, ndmin=2)
    if data.shape != (cfg["points"]**2, len(HEADER)):
        raise ValueError("Only a complete sweep can be plotted on this fixed grid.")
    fig = Figure(figsize=(11, 4.5), constrained_layout=True)
    FigureCanvasAgg(fig)
    for axis, column, name in zip(fig.subplots(1, 2), (6, 7), ("CH1", "CH2")):
        graph = axis.imshow(data[:, column].reshape(cfg["points"], cfg["points"]), origin="lower",
                            extent=[cfg["voltage_min"], cfg["voltage_max"]] * 2, aspect="auto")
        axis.set(title=f"{name} measured rate", xlabel="Alice target (V)", ylabel="Bob target (V)")
        fig.colorbar(graph, ax=axis, label="Counts/s")
    image = csv_path.with_suffix(".png")
    fig.savefig(image, dpi=cfg.get("plot_dpi", 300))
    return image


def run(cfg, stop, *, daq=None, system=None, constants=None, api=None):
    if stop.is_set():
        return 130
    if daq is None:
        import nidaqmx as daq
        from nidaqmx import constants
        from nidaqmx.system import System
        system = System.local()
    device = system.devices[cfg["device"]]
    if device.product_type != "USB-6351" or device.dev_is_simulated:
        raise RuntimeError("Sweep requires a physical USB-6351.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    folder = Path(cfg["output"]) / run_id
    folder.mkdir(parents=True, exist_ok=False)
    partial = folder / f"{run_id}_Detector_Traces.partial.csv"
    completed = folder / f"{run_id}_Detector_Traces.csv"
    meta = folder / f"{run_id}_sweep.json"
    record = dict(schema="qkd-eom-picoharp-sweep-v1", settings=cfg, status="preparing", complete=False,
                  layout="one-folder-per-sweep-v1",
                  rows=0, expected_rows=cfg["points"]**2, csv=partial.name, cleanup_errors=[],
                  raw_counts_meaning="Cumulative detected photons in completed acquisition windows; excludes settling/gaps.",
                  rate_meaning="Actual photons per channel divided by PicoHarp measured dwell seconds.",
                  raw_timestamps_saved=False, ao_zeroed_on_exit=False)
    save_json(meta, record)
    print(f"Run record: {meta.resolve()}", flush=True)
    ao = clock = stream = details = None
    opened = reserved = clock_started = False
    code, cumulative = 0, [0, 0]
    index = cfg["ph330"]["device_index"]
    try:
        api = api if api is not None else PH330(Path(cfg["dll"]))
        ph.bind_acquisition(api)
        serial = ct.create_string_buffer(8)
        api.call("OpenDevice", index, serial)
        opened = True
        if serial.value.decode() != cfg["ph330"]["serial"]:
            raise RuntimeError("PicoHarp serial differs from configured device.")
        record["hardware"] = ph.configure(api, cfg["ph330"], tttr_mode=cfg["mode"])
        record["dll_version"] = api.version
        ao = daq.Task()
        ao.ao_channels.add_ao_voltage_chan(f"{cfg['device']}/ao0:1", min_val=-10, max_val=10)
        ao.control(constants.TaskMode.TASK_VERIFY)
        ao.control(constants.TaskMode.TASK_COMMIT)
        reserved = True
        if cfg["clock"] is not None:
            clock = prepared_clock(daq, constants, cfg["device"], cfg["clock"])
            if stop.is_set():
                raise KeyboardInterrupt
            clock.start()
            clock_started = True
        if stop.wait(.2):
            raise KeyboardInterrupt
        rates = ph.rates(api, index, record["hardware"]["input_count"])
        record["rates_before"] = rates
        if cfg["mode"] == 2 and rates["sync_hz"] > 1000:
            raise RuntimeError("CW selected but PicoHarp sees a periodic SYNC signal. Select CW on the laser; "
                               "if its timing output still pulses, disconnect TRG OUT from PicoHarp SYNC for this CW sweep.")
        if cfg["mode"] == 3 and abs(rates["sync_hz"] / cfg["ph330"]["laser_hz"] - 1) > .05:
            raise RuntimeError("PicoHarp SYNC rate does not match selected pulsed laser rate.")
        stream = partial.open("x", newline="", encoding="utf-8")
        writer = csv.writer(stream)
        writer.writerow(HEADER)
        stream.flush()
        details = (folder / f"{run_id}_points.jsonl").open("x", encoding="utf-8")
        record["point_details"] = Path(details.name).name
        record["status"] = "running"
        save_json(meta, record)
        start = time.perf_counter()
        buffer = (ct.c_uint32 * ph.TTREADMAX)()
        for step, (alice, bob) in enumerate(grid(cfg)):
            if stop.is_set():
                raise KeyboardInterrupt
            ao.write([-alice / 20, -bob / 20], auto_start=True)
            if stop.wait(cfg["settle_s"]):
                raise KeyboardInterrupt
            count, duration, nrecords, flags = collect(api, index, cfg, stop, buffer, clock)
            cumulative = [old + new for old, new in zip(cumulative, count)]
            rate = [n / duration for n in count]
            writer.writerow([time.time(), time.perf_counter() - start, alice, bob, *cumulative, *rate])
            stream.flush()
            details.write(json.dumps(dict(step=step, counts=count, seconds=duration, records=nrecords, flags=flags)) + "\n")
            details.flush()
            record["rows"] += 1
            print(f"{step+1}/{record['expected_rows']} | Alice {alice:.2f} V, Bob {bob:.2f} V | "
                  f"CH1 {rate[0]:.1f}, CH2 {rate[1]:.1f} counts/s", flush=True)
            if record["rows"] % cfg["points"] == 0:
                save_json(meta, record)
        record.update(status="completed", complete=True)
    except KeyboardInterrupt:
        record.update(status="interrupted", complete=False)
        code = 130
    except Exception as exc:
        record.update(status="error", complete=False, error=str(exc))
        print(f"Sweep failed: {exc}", flush=True)
        code = 1
    finally:
        def cleanup(label, operation):
            try:
                operation()
                return True
            except Exception as exc:
                record["cleanup_errors"].append(f"{label}: {exc}")
                return False
        if clock is not None:
            if clock_started:
                cleanup("stop counter", clock.stop)
            cleanup("close counter", clock.close)
        if ao is not None:
            if reserved:
                record["ao_zeroed_on_exit"] = cleanup("zero AO", lambda: ao.write([0., 0.], auto_start=True))
            cleanup("close AO", ao.close)
        if opened:
            cleanup("close PicoHarp", lambda: api.call("CloseDevice", index))
        for file in (stream, details):
            if file is not None:
                cleanup("close output", file.close)
        if record["cleanup_errors"]:
            code = 1
            record.update(status="cleanup_error", complete=False)
        if record["complete"]:
            if cleanup("finalize CSV", lambda: partial.rename(completed)):
                record["csv"] = completed.name
            else:
                code = 1
                record.update(status="error", complete=False)
        record["host_end_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(meta, record)
        print(f"CSV: {(folder / record['csv']).resolve()}", flush=True)
        print(f"Run record: {meta.resolve()}", flush=True)
        print("Complete CSV is ready: click Analyze last completed sweep in the app." if record["complete"] else
              "Sweep incomplete: do not load its partial CSV into the fixed-grid notebook.", flush=True)
    if record["complete"]:
        try:
            image = plot_completed(completed, cfg)
            print(f"Preview image: {image.resolve()}", flush=True)
        except Exception as exc:
            print(f"CSV saved successfully; optional preview could not be created: {exc}", flush=True)
    return code


def main(values, stop_event=None):
    try:
        cfg = settings(values)
        if values["action"] == "Check settings":
            print(json.dumps(cfg, indent=2))
            print(f"N = {cfg['points']}: {cfg['points']**2} settings; at least {cfg['minimum_scan_seconds']/60:.1f} minutes plus acquisition overhead. "
                  "Calibration analysis reads the grid size automatically. No hardware accessed.")
            return 0
        if values["action"] != "Run sweep":
            raise ValueError("Unknown action.")
        if cfg["source"] != NI and values["manual-ready"] != "Yes":
            raise ValueError("Disconnect PFI12 and select the requested laser mode manually, then confirm in the Run tab.")
        return run(cfg, stop_event if stop_event is not None else threading.Event())
    except Exception as exc:
        print(f"EOM calibration sweep: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    from gui_app import launch
    launch(TOOL)
