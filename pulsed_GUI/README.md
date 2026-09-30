# Experiment controls

Open **Start QKD.cmd** in the repository root, or run the root `launch.py` in VS Code using `niDaqENV`. The launcher in this directory still works. Opening the GUI does not start instruments.

Choose a mode, review its **Run**, **Voltages**, **PicoHarp**, **Analysis** and **Files** tabs, select an action and click **Run**. Only applicable tabs appear. Each mode remembers its settings in `gui_settings.json`. Existing saved values take precedence over factory defaults. Close UniHarp and other programs owning AO0/AO1 or Ctr0 before acquisition.

In **Files**, photon-acquisition modes save sample type
(Emitter / Bright spot for testing / Unspecified), bandpass filter, laser power
(include units and where measured), and free-form notes in their JSON run records.
These entries are remembered per mode: review them when changing samples.
The calibration sweep's eight-column CSV is unchanged; its context is in the
adjacent `_sweep.json` file. Live alignment is display-only and saves no run data.

The final cell of `EOM_scripts/CalibrateEOMVoltages.ipynb` provides **Copy calibration**
and a JSON block. In Polarization matrix or either BB84 mode, open **Voltages →
Paste calibration (six voltages)**. Only that mode's form is filled; hardware
outputs do not change. Repeat the paste in another mode when needed. Candidate
provenance and check results are saved, and subsequent manual voltage edits are
flagged. Pasting resets physical-label confirmation and detector outcome labels.

In **Analysis**, **Saved figure resolution (DPI)** defaults to 300; use 600 for a
larger PNG (supported range 72–1200). It changes image resolution, not data bins,
counts, or fit results. Polarization also exports vector PDF/SVG. Both its matrix
and arrival histogram open after analysis; **Open plots** reopens them.

| GUI mode | Purpose | Guide |
| --- | --- | --- |
| Live alignment | Live PicoHarp rates with adjustable Alice/Bob biases and laser clock | [Live alignment](LIVE_ALIGNMENT.md) |
| EOM calibration sweep | Adjustable N × N voltage grid (default 60 × 60) with PicoHarp counts and notebook-compatible CSV | [Calibration sweep](EOM_CALIBRATION_SWEEP.md) |
| EOM calibration analysis | Measured-grid voltage selection, held-out checks, and direct handoff to measurement forms | [Calibration analysis](EOM_CALIBRATION_ANALYSIS.md) |
| Polarization matrix | Eight static voltage pairs; both detectors; arrival histograms and matrices | [Polarization](PULSED_POLARIZATION.md) |
| Ordered BB84 | Finite pulse-by-pulse timing and optical test | [BB84 acquisition](BB84.md) |
| Randomized BB84 | Independently randomized Alice states and Bob bases | [BB84 acquisition](BB84.md) |
| Arrival time / lifetime (CH1) | CH1 arrival histogram and optional preliminary decay fit | [CH1 guide](LIFETIME_CH1.md) |
| Pulsed / CW g2 | Two-detector correlations from 500 kHz through 80 MHz pulsed, plus CW | [g2 guide](G2.md) |
| EOM timing on scope | Buffered repeating voltage steps with delayed laser triggers | [Scope guide](EOM_TIMING_SCOPE.md) |
| Device connection | Find PicoHarp or inspect DLL without acquiring | [PicoHarp guide](PH330.md) |

The **Advanced** entries retain clock-only, static clock/voltage, two-channel T3 tools for diagnostics; BB84 diagnostics are integrated into the BB84 modes. The new g2 mode supports the QKD setup's longer correlation windows; its PBS-output result is polarization-resolved cross-correlation. The separate quantum emitter setup remains available for independent source characterization. A PBS output coincidence plot alone is not automatically a source HBT characterization.

For polarization, **Seconds at EACH voltage pair** defaults to 60 seconds: eight settings take about eight minutes plus warm-up and settling. Increase it for low counts. Internal 2/20/80 MHz choices require manually disconnecting PFI12 and selecting the laser controller rate; confirm this in the Run tab. The software then uses no NI counter clock. Reconnect for NI-triggered operation.

The two pulse-by-pulse modes are ready for **commissioning**, with complete saved command sequences and raw T3. They do not yet establish the absolute NI-to-PicoHarp trial origin or produce an accepted key/QBER. See the BB84 guide before interpreting those recordings.

For either BB84 mode, choose **Record** directly: preparation and acquisition save
together in one run folder. **Prepare only** is an optional independent offline test,
not a prerequisite. **Analyze latest recording →** opens the integrated diagnostic
workflow. Ordered BB84 displays all eight phase hypotheses while trial origin remains
unknown. Analyses save under the source run's `analysis` folder; the former standalone
raw T3 preview selector has been removed.

## Code organization

After a sweep, click **Analyze last completed sweep →**, review/run calibration,
then use a **Load last completed result into…** button to fill a polarization or
BB84 form. No notebook or clipboard is needed for this path. The notebook remains
available and uses the same calculation core. See the calibration-analysis guide.

Analysis file pickers start at their current or recent compatible data location.
Blank inputs are filled from recent runs; **Use latest** explicitly replaces an
existing selection. Polarization, lifetime, g² and both BB84 modes have similar
shortcuts. Source filenames remain visible, and no shortcut starts hardware.

| Files | Responsibility |
| --- | --- |
| `gui_app.py`, `launch.py` | Shared forms, saved settings, action dispatch and worker lifecycle |
| `laser_clock_eom_counts.py`, `live_counts_window.py` | Live alignment hardware session and live display |
| `pulsed_polarization.py`, `polarization_analysis.py` | Static matrix acquisition and offline arrival/matrix analysis |
| `polarization_figures.py` | Diagonal figure layout, output-channel colors and PNG/PDF/SVG publication exports |
| `bb84_controls.py`, `bb84_sequence.py`, `bb84_run.py` | Protocol forms, precomputed sequences, finite NI + T3 acquisition |
| `eom_timing_scope.py` | Scope timing patterns |
| `eom_voltage_sweep.py` | Timed PicoHarp counting across the notebook-compatible static calibration grid |
| `eom_calibration.py`, `eom_calibration_core.py`, `eom_calibration_figures.py` | Notebook-equivalent calibration workflow, shared calculations and diagnostics |
| `recent_data.py` | Compatible recent-run locations and file-picker defaults |
| `bb84_analysis.py` | Full-file ordered phase diagnostics and supplied-origin saved-choice comparisons |
| `g2_measurement.py`, `g2_analysis.py` | Pulsed T3 / CW T2 acquisition and full-file two-detector correlation |
| `laser_clock.py`, `laser_clock_eom.py` | Shared clock/voltage validation and diagnostic backends |
| `ph330.py`, `ph330_acquire.py` | PicoHarp DLL interface and raw T3 acquisition |
| `ph330_preview.py`, `ph330_lifetime.py` | Saved-data diagnostics and CH1 arrival measurements |
| `test_*.py` | Offline tests with simulated hardware |

The older duplicated `pulsed/` code is in `archived/pulsed_legacy/`. Existing measurement files remain at their original locations. Calibration sweeps and notebooks remain in `EOM_scripts/`.

New primary workflows save under `data/timing`, `data/polarization`, `data/eoms`, `data/g2`, and `data/bb84`. Existing diagnostic profiles may retain their saved output folders. The log prints full paths; **Open data folder** opens the latest output. Raw T2/T3 files are not PTU files: keep them with their JSON metadata. Gates and histogram rebinning are applied offline; input channel offsets are applied once during acquisition.

Developer verification from the repository root:

```powershell
python -m unittest discover -s pulsed_GUI -q
```

These checks simulate the instruments. They do not replace scope validation, USB streaming tests or optical measurements.
