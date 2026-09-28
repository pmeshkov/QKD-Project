# QKD experiment controls

NI USB-6351 control and PicoHarp 330 acquisition for the polarization-encoded QKD experiment. **Open [Start QKD.cmd](<Start QKD.cmd>)** or open [launch.py](launch.py) in VS Code, select the `niDaqENV` interpreter and click **Run Python File**. Parameters are entered in the GUI; opening it does not start hardware.

Close UniHarp before connecting the PicoHarp. Stop other programs that own AO0/AO1 or Ctr0 before running combined experiment controls. Settings are remembered per mode. Use **Check settings** first, then the recording/output action. **Stop** requests hardware cleanup and preserves partial acquisition records.

## Choose a measurement

| Goal | Mode / tool | Output |
| --- | --- | --- |
| Align beams, adjust EOM biases and laser rate | Live alignment | CH1/CH2/SYNC rates, scrolling traces and adjustment logs |
| Calibrate EOM voltages without moving detector cables | EOM calibration sweep | Notebook-compatible N × N CSV; [instructions](pulsed_GUI/EOM_CALIBRATION_SWEEP.md) |
| Check EOM settling and trigger phase on the scope | EOM timing on scope | Repeating buffered AO pattern and delayed PFI12 pulses |
| Measure the static polarization baseline | Polarization matrix | Eight voltage settings, sixteen arrival histograms and count/probability matrices |
| Test pulse-by-pulse state ordering | Repeating H-V-R-L | Known Alice sequence, Bob basis sequence and raw T3 recording; bench commissioning |
| Acquire randomized protocol data | Random BB84 | Independent Alice/Bob choices, saved trial sequence and raw T3 recording; bench commissioning |
| Check connections, collect arrival data or inspect old runs | Diagnostics / existing PicoHarp tools | Device information, arrival-time plots and saved-run previews |

The pulse-by-pulse modes require bench validation of the **NI trial index to PicoHarp T3 index relationship**. A first detection or a discarded warm-up interval does not by itself establish that origin. Treat recordings without a verified origin as commissioning data. Randomized acquisition does not by itself certify a secure key; reconciliation and privacy amplification belong to the downstream processing workflow.

See [BB84 acquisition instructions](pulsed_GUI/BB84.md) for the new modes, saved sequence format, short first run and remaining synchronization check.

## Static polarization: longer runs and internal laser clock

Controls are grouped into **Run**, **Voltages**, **PicoHarp**, **Analysis**, and **Files** tabs. In Run, set **Seconds at EACH voltage pair**. The new default is 60 seconds, but your saved settings remain unchanged. For low count rates, 60 seconds per setting means approximately eight minutes total; 300 seconds means approximately forty minutes. Both detectors are recorded together. Display binning can be increased during **Analyze saved run** without changing raw records.

For static troubleshooting, select **Laser internal 2 MHz**, **Laser internal 20 MHz**, or **Laser internal 80 MHz**. **Manually disconnect PFI12 from BDL SYNC IN, select that rate on the laser controller, and confirm the change in the GUI.** Software does not switch the laser controller. This mode leaves the NI laser trigger unused while retaining static AO control. Reconnect/reselect external triggering before returning to pulse-by-pulse experiments.

The working switched profiles are **500 kHz / 1200 ns** and approximately **750 kHz / 900 ns** nominal AO-to-PFI12 delay. The EOM switching target is kilohertz, not 750 MHz. User scope tests have established synchronized voltage switching; optical fidelity and recorded trial alignment remain separate checks.

## Files and measurements

| Location | Purpose |
| --- | --- |
| [`pulsed_GUI/`](pulsed_GUI/) | Active controls, shared hardware interfaces and analysis helpers; [detailed guides](pulsed_GUI/README.md) |
| [`EOM_scripts/`](EOM_scripts/README.md) | Retained NI-detector alignment/sweep workflow and calibration notebook |
| [`data/alignment/`](data/alignment/) | Live count-rate samples and adjustment logs |
| [`data/timing/`](data/timing/) | Scope-test waveform/setting records |
| [`data/polarization/`](data/polarization/) | Static polarization sessions, ungated T3 and derived histograms/matrices |
| `data/bb84/` | Indexed sequence acquisition sessions and saved protocol choices |
| [`references/`](references/) | Hardware manuals and reference papers |
| [`plan.txt`](plan.txt) | Current measured facts and commissioning steps |
| [`archived/`](archived/) | Historical scripts; not the active experiment controls |
| [`pulsed/runs/`](pulsed/runs/), `pulsed_GUI/runs/` | Preserved older measurements/logs |

Keep each raw `events.t3raw` with its `metadata.json`, and keep complete experiment session folders together. Raw T3 is ungated: arrival gates are applied during analysis. CH2's configured **+4.5 ns** offset is applied once by the acquisition setup; do not apply it a second time in analysis. Calibration voltages and input thresholds remain editable and are recorded with each run.

The old `pulsed/` code has moved to [`archived/pulsed_legacy/`](archived/pulsed_legacy/ARCHIVE.md). Its original measurement folders have not moved. The active `pulsed_GUI` directory and existing launcher remain available for compatibility.

## Environment and offline checks

Use the existing `niDaqENV` environment; a separate repository or virtual environment is unnecessary. New environments need Python with tkinter, the packages in [requirements.txt](requirements.txt), the NI-DAQmx driver and the PicoHarp/UniHarp DLL. Python packages do not install the hardware drivers.

```powershell
conda activate niDaqENV
python launch.py
```

For development, the offline test suite uses simulated hardware:

```powershell
python -m unittest discover -s pulsed_GUI -v
```

Calibration notebooks additionally use pandas, Plotly and a Jupyter Python kernel; see [the calibration guide](EOM_scripts/README.md). No installation or device reset occurs when opening the controls.
