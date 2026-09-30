"""GUI profiles for finite ordered/random BB84 commissioning acquisitions."""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from measurement_metadata import FORM as MEASUREMENT_FORM, read as measurement_context

from ph330 import DEFAULT_DLL
from ph330_acquire import validate
from bb84_sequence import build_sequence, SequenceCancelled
from calibration_transfer import context as calibration_context

ORDERED = "Ordered BB84"
RANDOM = "Randomized BB84"
LEGACY_NAMES = {ORDERED: "Repeating H-V-R-L", RANDOM: "Random BB84"}
PREPARE = "Prepare only (offline test; not used by Record)"
MODES = {ORDERED: "ordered", RANDOM: "random"}
DATA = Path(__file__).resolve().parents[1] / "data" / "bb84"
PRESETS = {"500 kHz / 1200 ns": (500000, 1200), "750 kHz / 900 ns": (750000, 900)}
HELP = ("Finite pulse-by-pulse acquisition: one Alice state and one Bob basis per excitation, saved before output. "
        "Commissioning: absolute NI-to-PicoHarp pulse alignment is UNVERIFIED; no key or QBER is reported. "
        "Record creates and saves the sequence AND photons together in one run folder; no Prepare step is needed. "
        "Prepare only is an independent offline test, never a file queued for Record. "
        "Analyze saved run uses saved voltages; without a trial origin, ordered results show all eight phase hypotheses. "
        "Defaults are provisional voltage labels.")


def form(mode):
    return [
        ("action", "Action (Record includes preparation)", "Check settings", ["Check settings", "Record", "Analyze saved run", PREPARE]),
        ("seconds", "Experimental recording time (s; excludes warm-up)", "1", None),
        ("timing-profile", "Tested EOM timing profile", "500 kHz / 1200 ns", [*PRESETS, "Custom"]),
        ("frequency-hz", "External laser / AO update rate (Hz)", "500000", None),
        ("delay-ns", "Laser trigger delay after voltage update (ns)", "1200", None),
        ("high-ns", "NI PFI12 trigger width (ns; function-generator output width is manual)", "100", None),
        ("function-generator-note", "Function generator model / output pulse width / delay (record only; set manually)", "", None),
        ("warmup-s", "Laser warm-up at fixed H/HV voltages (s)", "1", None),
        ("device", "NI USB-6351 device", "Dev1", None),
        ("labels-confirmed", "Physical state and Bob basis voltage assignments verified?", "No", ["No", "Yes"]),
        *[(f"alice-{state.lower()}-v", f"Alice {state} target (V; provisional until verified)", str(voltage), None)
          for state, voltage in (("H", -152), ("V", 50.90), ("R", -50.87), ("L", 152.69))],
        ("bob-hv-v", "Bob H/V basis target (V)", "47.52", None),
        ("bob-rl-v", "Bob R/L basis target (V)", "-47.44", None),
        ("dll", "PicoHarp DLL", str(DEFAULT_DLL), "file"),
        ("device-index", "PicoHarp device index", "0", None),
        ("serial", "PicoHarp serial", "1050578", None),
        ("binning", "T3 binning (6 = 64 ps on this device)", "6", None),
        *[item for prefix, level, edge, offset in (("sync", "-60", "falling", "0"),
                                                 ("ch1", "200", "rising", "0"), ("ch2", "200", "rising", "4.5"))
          for item in ((f"{prefix}-mode", f"{prefix.upper()} trigger mode", "edge", ["edge", "cfd"]),
                       (f"{prefix}-level-mv", f"{prefix.upper()} threshold (signed mV)", level, None),
                       (f"{prefix}-edge", f"{prefix.upper()} edge (edge mode)", edge, ["rising", "falling"]),
                       (f"{prefix}-zero", f"{prefix.upper()} CFD zero crossing (mV)", "-10", None),
                       (f"{prefix}-offset-ns", f"{prefix.upper()} channel offset (ns)", offset, None))],
        ("output", "Saved sequences and raw T3 directory", str(DATA / mode), "directory"),
        ("folder", "Saved BB84 run folder (contains session.json)", "", "directory"),
        ("analysis-origin", "First experimental trial: unwrapped T3 SYNC index (blank = diagnostics only)", "", None),
        ("analysis-origin-note", "Origin reference / evidence (required when supplying an index)", "", None),
        ("analysis-start", "Diagnostic minimum SYNC index (blank = warm-up count + 0.5 s margin)", "", None),
        ("analysis-stop", "Diagnostic exclusive maximum SYNC index (blank = all remaining)", "", None),
        ("analysis-hv-ch1", "Analysis: CH1 outcome at Bob H/V voltage (provisional)", "V", ["H", "V"]),
        ("analysis-rl-ch1", "Analysis: CH1 outcome at Bob R/L voltage (provisional)", "L", ["R", "L"]),
        ("gate", "Analysis: arrival-time gate enabled?", "Yes", ["Yes", "No"]),
        ("gate-start-ns", "Arrival gate start (ns; inclusive)", "50", None),
        ("gate-stop-ns", "Arrival gate end (ns; exclusive)", "60", None),
        ("plot-dpi", "Saved figure resolution (DPI)", "300", None),
        *MEASUREMENT_FORM,
        ("note", "Emitter / calibration / optical settings / run notes", "", None),
    ]


def settings(values, mode):
    def number(key):
        value = float(values[key])
        if not math.isfinite(value):
            raise ValueError(f"{key} must be finite.")
        return value
    if mode not in ("ordered", "random"):
        raise ValueError("Unknown sequence mode.")
    profile = values["timing-profile"]
    if profile not in (*PRESETS, "Custom"):
        raise ValueError("Select a timing profile.")
    hz, delay = PRESETS[profile] if profile in PRESETS else (number("frequency-hz"), number("delay-ns"))
    if not 1000 <= hz <= 1000000:
        raise ValueError("Use an external repetition rate from 1 kHz to 1 MHz.")
    actual_rate = 100000000 / round(100000000 / hz)
    seconds, warmup = number("seconds"), number("warmup-s")
    if not .001 <= seconds <= 3600 or not 0 <= warmup <= 3600:
        raise ValueError("Experimental time must be .001–3600 s; warm-up 0–3600 s.")
    def trigger(prefix):
        result = dict(mode=values[prefix + "-mode"], edge=values[prefix + "-edge"],
                      level_mv=int(values[prefix + "-level-mv"]),
                      offset_ps=round(number(prefix + "-offset-ns") * 1000))
        if result["mode"] == "cfd":
            result["zero_cross_mv"] = int(values[prefix + "-zero"])
        return result
    ph = dict(device_index=int(values["device-index"]), serial=values["serial"], binning=int(values["binning"]),
              laser_hz=actual_rate, sync_divider=1, sync=trigger("sync"),
              detectors=[dict(trigger("ch1"), channel=0), dict(trigger("ch2"), channel=1)])
    validate(ph, expected_laser_hz=None)
    if values["labels-confirmed"] not in ("No", "Yes"):
        raise ValueError("Select whether the physical labels have been verified.")
    return dict(mode=mode, device=values["device"], frequency_hz=hz, delay_ns=delay, high_ns=number("high-ns"),
                trial_count=max(1, round(seconds * actual_rate)), warmup_count=round(warmup * actual_rate),
                alice_voltages={s: number(f"alice-{s.lower()}-v") for s in ("H", "V", "R", "L")},
                bob_voltages={b: number(f"bob-{b.lower()}-v") for b in ("HV", "RL")},
                ph330=ph, dll=values["dll"], output=values["output"], note=values["note"], measurement=measurement_context(values),
                trigger_chain={"route": "NI PFI12 -> externally triggered function generator -> BDL SYNC IN; BDL TRG OUT -> PicoHarp SYNC",
                               "function_generator_settings": values.get("function-generator-note", ""),
                               "external_pulse_width_control": "manual, not controlled by this application"},
                calibration_transfer=calibration_context(values),
                labels_confirmed=values["labels-confirmed"] == "Yes")


def main(values, mode, stop_event=None):
    try:
        if values["action"] == "Analyze saved run":
            from bb84_analysis import analyze
            analyze(values["folder"], origin=int(values["analysis-origin"]) if values["analysis-origin"].strip() else None,
                    origin_note=values["analysis-origin-note"],
                    sync_start=int(values["analysis-start"]) if values["analysis-start"].strip() else None,
                    sync_stop=int(values["analysis-stop"]) if values["analysis-stop"].strip() else None,
                    mapping=(values["analysis-hv-ch1"], values["analysis-rl-ch1"]),
                    gate=(float(values["gate-start-ns"]), float(values["gate-stop-ns"])) if values["gate"] == "Yes" else None,
                    dpi=int(values["plot-dpi"]), stop_event=stop_event)
            return 0
        from bb84_run import prepare_plan, run
        cfg = settings(values, mode)
        plan = prepare_plan(cfg)
        print(json.dumps(plan, indent=2, default=str))
        print("Pulse alignment UNVERIFIED. This recording is for commissioning, not accepted key generation.")
        if values["action"] == "Check settings":
            print("Settings checked offline. No outputs started.")
            return 0
        if values["action"] in (PREPARE, "Prepare sequence only"):
            print("OFFLINE TEST ONLY: Record will create a fresh sequence in its own complete run folder.")
            folder = Path(cfg["output"]) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ_prepared")
            build_sequence(folder, mode, cfg["trial_count"], cfg["warmup_count"],
                           cfg["alice_voltages"], cfg["bob_voltages"], stop_event)
            (folder / "requested_settings.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            print(f"Run record: {(folder / 'sequence.json').resolve()}", flush=True)
            return 0
        if values["action"] != "Record":
            raise ValueError("Unknown action.")
        return run(cfg, stop_event=stop_event)
    except (KeyboardInterrupt, SequenceCancelled):
        print("Sequence operation stopped. Partial files remain marked incomplete.")
        return 130
    except Exception as exc:
        print(f"BB84: {exc}", flush=True)
        return 1
