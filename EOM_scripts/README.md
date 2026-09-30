# EOM calibration and legacy NI-detector controls

These files remain in place because they are part of the working calibration process. The current experiment entry point is [the root launcher](../launch.py); use its live alignment mode when the detectors are connected to the PicoHarp.

With detectors on PicoHarp, select **EOM calibration sweep** in that launcher.
It reproduces the existing grid order and CSV columns, with adjustable N (default 60), saving directly to
`data/eoms/`. See [the PicoHarp sweep guide](../pulsed_GUI/EOM_CALIBRATION_SWEEP.md).
Select the input filename in the new notebook; it detects the voltage grid
automatically. Only use a completed `Detector_Traces.csv`, not a partial scan.

## Measured-data calibration (recommended)

This workflow is now also built into the main GUI: **EOM calibration analysis**.
After a sweep, use **Analyze last completed sweep →**, then load its candidate
directly into a polarization or BB84 form. See the
[app calibration guide](../pulsed_GUI/EOM_CALIBRATION_ANALYSIS.md).
The notebook is retained. Its `calibration_analysis.py` import now forwards to
the same calculation core in `pulsed_GUI/eom_calibration_core.py`; the app does
not depend on this folder.

Open [CalibrateEOMVoltages.ipynb](CalibrateEOMVoltages.ipynb), select the
`niDaqENV` kernel, edit the CSV path and initial search windows, then **Run All**.
The companion [calibration_analysis.py](calibration_analysis.py) is required.
No hardware is accessed and no GUI voltages are overwritten.

The new notebook jointly selects four Alice and two Bob voltages from measured
grid points, optimizing both extinction and the four 50:50 detector-count cases.
It infers the grid from actual voltage columns, uses both detectors, and reserves
counts for a selection-independent check. The seed order is S0/S1/S2/S3; opposite
pairs are S0/S2 and S1/S3. These roles are not automatically physical H/V/R/L labels.

Keep the PicoHarp CSV, `_sweep.json` and `_points.jsonl` together: the CSV
`Raw_Count` fields are cumulative, while the point sidecar contains the exact
per-setting counts and dwell times. Legacy NI files without sidecars require an
explicit approximate dwell assumption and are marked exploratory. Never use
cumulative counts as the photon total for a single voltage setting.

Outputs go to `data/eoms/calibrations/<UTC timestamp>/`: candidate voltages with
source hashes, full-data and held-out checks, measured-response plots (PNG/PDF),
training-only candidate rankings and proposed **unmeasured** refinement points.
The optional independent-validation CSV must have measured the selected voltages
exactly. Failure to meet all tolerances is reported, not hidden by normalization.
The held-out test cannot establish drift stability or polarization handedness.

The final **Copy calibration into the experiment app** cell emits the six voltages
as JSON, with a copy button and selectable text fallback. The same block is saved
as `calibration_for_app.json` when export is enabled. In the main app, select
Polarization matrix or either BB84 mode, then **Voltages → Paste calibration
(six voltages)**. Pasting only fills the selected form; it never sets hardware
outputs. Check results travel with the candidate, including failed checks.
`EXPORT_DPI` controls the notebook's PNG export resolution (default 300).

The provisional display convention is S0/S1/S2/S3 = H/R/V/L. It does not certify
the absolute physical states. The notebook copied into
`data/eoms/9_28_26/BrandonsRun2/` also has the final transfer cell; at this update,
its input path and saved results still refer to **BrandonsRun**, not BrandonsRun2.
Check the CSV setting and the printed actual input path before using its output.

The existing sinusoidal-fit notebook is preserved for comparison. It derives
voltages from detector 0's phase fit and ideal spacing rather than jointly
enforcing measured fractions on both detectors.

| File | Purpose |
| --- | --- |
| [`niDAQ_gui_log.py`](niDAQ_gui_log.py) | Static AO0/AO1 adjustment while reading NI detector counters. Retained for the original detector wiring. |
| [`niDAQ_V_sweep.py`](niDAQ_V_sweep.py) | Two-dimensional Alice/Bob voltage sweep with NI detector counting. Saves a detector-trace CSV when requested at the end. |
| [`calibrateBB84Protocol.ipynb`](calibrateBB84Protocol.ipynb) | Original sinusoidal fit and ideal-phase voltage candidates; preserved for comparison. |
| [`CalibrateEOMVoltages.ipynb`](CalibrateEOMVoltages.ipynb) | Joint measured-grid calibration with extinction/balance targets and uncertainty checks. |
| Existing CSV/TXT files | Calibration inputs and recorded outputs; retained as measurement provenance. |

Do not run these AO-controlling scripts alongside experiment modes that already own AO0/AO1. The old `SHUTOFFEOMS.py` helper is now under [`archived/recovery/`](../archived/recovery/README.md): it resets the entire device and is not the normal way to stop an experiment.

## Existing calibration workflow

1. Run `niDAQ_V_sweep.py` using the NI detector wiring, or use the PicoHarp **EOM calibration sweep**. Keep PicoHarp sidecars with the CSV for exact counting uncertainties.
2. Open the chosen notebook in VS Code with the experiment Python kernel. Set its input directory/file. Only the original fit notebook requires manually setting `N`; the new notebook reads the voltage columns directly.
3. Review the fitted curves and residual behavior before using the reported voltages. Enter confirmed values into the experiment GUI and include the calibration filename in run notes.

The notebook's Alice array is a sequence of phase steps. **Opposite polarization pairs are entries 0/2 and 1/3**, so the array cannot simply be labeled H, V, R, L in its listed order. Verify physical state labels, Bob's basis labels and detector outcomes with optical measurements; the fit alone does not establish circular handedness. Unconfirmed recordings retain S0–S3, with optional provisional H/R/V/L aliases in figures.

The current `calibrateBB84Protocol.ipynb` uses the correct detector-specific mean in its R-squared calculations. An older notebook had a reporting typo; that historical issue is not the explanation for the present half-split calibration errors.

For notebook use, install pandas, Plotly and an IPython/Jupyter kernel in addition to [the active controls' requirements](../requirements.txt). The archived Vpi scripts additionally use Dash; the active controls do not need Dash.

## Old Vpi analysis

The former top-level `Vpi_sweep_analysis/` only contains a bytecode cache, not a maintained analysis script. It is left untouched during this cleanup. The actual historical scripts are in [`archived/Vpi_sweep_analysis/`](../archived/Vpi_sweep_analysis/): they inspect `Vpi_Sweep_LP_*_deg.csv` files from older polarizer-angle sweeps and are unrelated to the current two-EOM fitting workflow.
