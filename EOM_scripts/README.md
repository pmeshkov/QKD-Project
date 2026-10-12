# EOM calibration and legacy NI-detector controls

These files remain in place because they are part of the working calibration process. The current experiment entry point is [the root launcher](../launch.py); use its live alignment mode when the detectors are connected to the PicoHarp.

With detectors on PicoHarp, select **EOM calibration sweep** in that launcher.
It reproduces the existing grid order and CSV columns, with adjustable N (default 60), saving directly to
`data/eoms/`. See [the PicoHarp sweep guide](../pulsed_GUI/EOM_CALIBRATION_SWEEP.md).
Select the input filename in the new notebook; it detects the voltage grid
automatically. Only use a completed `Detector_Traces.csv`, not a partial scan.

## Physical-response calibration (recommended)

Use **EOM calibration analysis → Fit sweep** in the main app, or open
[CalibrateEOMVoltages.ipynb](CalibrateEOMVoltages.ipynb). Both use the same joint
Poisson fit of complementary sinusoidal detector responses, minimize the six
voltage magnitudes using periodic equivalents, and provide an independent
verification step using a fresh eight-setting polarization run.

See [the calibration guide](../pulsed_GUI/EOM_CALIBRATION_ANALYSIS.md) for the
model, explicit limits/tolerances, residual diagnostics and verification intervals.
Outputs stay in the source sweep's `analysis/<timestamp>/` folder. Candidate JSON
can be pasted or loaded into polarization and both BB84 profiles without starting
hardware. The notebook prints that same JSON block.

Keep the CSV and PicoHarp sidecars together for true per-setting counts and dwell.
Legacy NI CSVs require an explicit approximate dwell. Grid columns determine the
actual voltages; no manual N or reconstruction of voltage coordinates is needed.

Peter's and Brandon's notebooks are historical alternatives, retained unchanged.
`calibration_analysis.py` and the measured-grid APIs in `eom_calibration_core.py`
remain only for historical notebooks; the current workflow uses
`pulsed_GUI/eom_model_calibration.py`. There is no training/held-out count split
in the recommended calibration.

| File | Purpose |
| --- | --- |
| [`niDAQ_gui_log.py`](niDAQ_gui_log.py) | Static AO0/AO1 adjustment while reading NI detector counters. Retained for the original detector wiring. |
| [`niDAQ_V_sweep.py`](niDAQ_V_sweep.py) | Two-dimensional Alice/Bob voltage sweep with NI detector counting. Saves a detector-trace CSV when requested at the end. |
| [`CalibrateEOMVoltages(Peter).ipynb`](CalibrateEOMVoltages(Peter).ipynb) | Original independent detector fits and CH1-based minimum-voltage extraction; preserved for comparison. |
| [`CalibrateEOMVoltages.ipynb`](CalibrateEOMVoltages.ipynb) | Shared physical-fit workflow, minimum-voltage selection and independent verification. |
| Existing CSV/TXT files | Calibration inputs and recorded outputs; retained as measurement provenance. |

Do not run these AO-controlling scripts alongside experiment modes that already own AO0/AO1. The old `SHUTOFFEOMS.py` helper is now under [`archived/recovery/`](../archived/recovery/README.md): it resets the entire device and is not the normal way to stop an experiment.

## Existing calibration workflow

1. Run `niDAQ_V_sweep.py` using the NI detector wiring, or use the PicoHarp **EOM calibration sweep**. Keep PicoHarp sidecars with the CSV for exact counting uncertainties.
2. Open the chosen notebook in VS Code with the experiment Python kernel. Set its input directory/file. Only the original fit notebook requires manually setting `N`; the new notebook reads the voltage columns directly.
3. Review the fitted curves and residual behavior before using the reported voltages. Enter confirmed values into the experiment GUI and include the calibration filename in run notes.

The notebook's Alice array is a sequence of phase steps. **Opposite polarization pairs are entries 0/2 and 1/3**, so the array cannot simply be labeled H, V, R, L in its listed order. Verify physical state labels, Bob's basis labels and detector outcomes with optical measurements; the fit alone does not establish circular handedness. Unconfirmed recordings retain S0–S3, with optional provisional H/R/V/L aliases in figures.

The current joint fit reports a separate R-squared for each detector, using that detector's own mean. Residuals and independent fraction checks are also required; R-squared alone does not validate selected voltages.

For notebook use, install pandas, Plotly and an IPython/Jupyter kernel in addition to [the active controls' requirements](../requirements.txt). The archived Vpi scripts additionally use Dash; the active controls do not need Dash.

## Old Vpi analysis

The former top-level `Vpi_sweep_analysis/` only contains a bytecode cache, not a maintained analysis script. It is left untouched during this cleanup. The actual historical scripts are in [`archived/Vpi_sweep_analysis/`](../archived/Vpi_sweep_analysis/): they inspect `Vpi_Sweep_LP_*_deg.csv` files from older polarizer-angle sweeps and are unrelated to the current two-EOM fitting workflow.
