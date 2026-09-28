# Experiment controls

Open **Start QKD.cmd** in the repository root, or run the root `launch.py` in VS Code using `niDaqENV`. The launcher in this directory still works. Opening the GUI does not start instruments.

Choose a mode, review its **Run**, **Voltages**, **PicoHarp**, **Analysis** and **Files** tabs, select an action and click **Run**. Only applicable tabs appear. Each mode remembers its settings in `gui_settings.json`. Existing saved values take precedence over factory defaults. Close UniHarp and other programs owning AO0/AO1 or Ctr0 before acquisition.

| GUI mode | Purpose | Guide |
| --- | --- | --- |
| Live alignment | Live PicoHarp rates with adjustable Alice/Bob biases and laser clock | [Live alignment](LIVE_ALIGNMENT.md) |
| EOM calibration sweep | Adjustable N × N voltage grid (default 60 × 60) with PicoHarp counts and notebook-compatible CSV | [Calibration sweep](EOM_CALIBRATION_SWEEP.md) |
| Polarization matrix | Eight static voltage pairs; both detectors; arrival histograms and matrices | [Polarization](PULSED_POLARIZATION.md) |
| Repeating H-V-R-L | Finite pulse-by-pulse timing and optical test | [BB84 acquisition](BB84.md) |
| Random BB84 | Independently randomized Alice states and Bob bases | [BB84 acquisition](BB84.md) |
| Arrival time / lifetime (CH1) | CH1 arrival histogram and optional preliminary decay fit | [CH1 guide](LIFETIME_CH1.md) |
| EOM timing on scope | Buffered repeating voltage steps with delayed laser triggers | [Scope guide](EOM_TIMING_SCOPE.md) |
| Device connection | Find PicoHarp or inspect DLL without acquiring | [PicoHarp guide](PH330.md) |

The **Advanced** entries retain clock-only, static clock/voltage, two-channel T3 and raw-preview tools for diagnostics. Paper-quality source g2/lifetime measurements remain on the separate quantum emitter setup. A PBS output coincidence plot alone is not automatically a source HBT characterization.

For polarization, **Seconds at EACH voltage pair** defaults to 60 seconds: eight settings take about eight minutes plus warm-up and settling. Increase it for low counts. Internal 2/20/80 MHz choices require manually disconnecting PFI12 and selecting the laser controller rate; confirm this in the Run tab. The software then uses no NI counter clock. Reconnect for NI-triggered operation.

The two pulse-by-pulse modes are ready for **commissioning**, with complete saved command sequences and raw T3. They do not yet establish the absolute NI-to-PicoHarp trial origin or produce an accepted key/QBER. See the BB84 guide before interpreting those recordings.

## Code organization

| Files | Responsibility |
| --- | --- |
| `gui_app.py`, `launch.py` | Shared forms, saved settings, action dispatch and worker lifecycle |
| `laser_clock_eom_counts.py`, `live_counts_window.py` | Live alignment hardware session and live display |
| `pulsed_polarization.py`, `polarization_analysis.py` | Static matrix acquisition and offline arrival/matrix analysis |
| `bb84_controls.py`, `bb84_sequence.py`, `bb84_run.py` | Protocol forms, precomputed sequences, finite NI + T3 acquisition |
| `eom_timing_scope.py` | Scope timing patterns |
| `eom_voltage_sweep.py` | Timed PicoHarp counting across the notebook-compatible static calibration grid |
| `laser_clock.py`, `laser_clock_eom.py` | Shared clock/voltage validation and diagnostic backends |
| `ph330.py`, `ph330_acquire.py` | PicoHarp DLL interface and raw T3 acquisition |
| `ph330_preview.py`, `ph330_lifetime.py` | Saved-data diagnostics and CH1 arrival measurements |
| `test_*.py` | Offline tests with simulated hardware |

The older duplicated `pulsed/` code is in `archived/pulsed_legacy/`. Existing measurement files remain at their original locations. Calibration sweeps and notebooks remain in `EOM_scripts/`.

New primary workflows save under `data/alignment`, `data/timing`, `data/polarization`, and `data/bb84`. Existing diagnostic profiles may retain their saved output folders. The log prints full paths; **Open data folder** opens the latest output. Raw T3 files are not PTU files: keep them with their JSON metadata. Gates and histogram rebinning are applied offline; input channel offsets are applied once during acquisition.

Developer verification from the repository root:

```powershell
python -m unittest discover -s pulsed_GUI -q
```

These checks simulate the instruments. They do not replace scope validation, USB streaming tests or optical measurements.
