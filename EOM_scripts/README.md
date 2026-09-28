# EOM calibration and legacy NI-detector controls

These files remain in place because they are part of the working calibration process. The current experiment entry point is [the root launcher](../launch.py); use its live alignment mode when the detectors are connected to the PicoHarp.

With detectors on PicoHarp, select **EOM calibration sweep** in that launcher.
It reproduces the existing grid order and CSV columns, with adjustable N (default 60), saving directly to
`data/eoms/`. See [the PicoHarp sweep guide](../pulsed_GUI/EOM_CALIBRATION_SWEEP.md).
Select the new input filename and set the notebook's `N` to the GUI value. The
analysis code remains unchanged. Only use a completed `Detector_Traces.csv`, not a partial scan.

| File | Purpose |
| --- | --- |
| [`EOM_gui_log.py`](EOM_gui_log.py) | Static AO0/AO1 adjustment while reading the NI detector counters on PFI1/PFI2. Retained for the original detector wiring. |
| [`EOM_V_sweep.py`](EOM_V_sweep.py) | Two-dimensional Alice/Bob voltage sweep with NI detector counting. Saves a detector-trace CSV when requested at the end. |
| [`GeminiSolvedcalibrateBB84Protocol.ipynb`](GeminiSolvedcalibrateBB84Protocol.ipynb) | Fits the sweep data and calculates four Alice and two Bob voltage candidates. |
| Existing CSV/TXT files | Calibration inputs and recorded outputs; retained as measurement provenance. |

Do not run these AO-controlling scripts alongside experiment modes that already own AO0/AO1. The old `SHUTOFFEOMS.py` helper is now under [`archived/recovery/`](../archived/recovery/README.md): it resets the entire device and is not the normal way to stop an experiment.

## Existing calibration workflow

1. Run `EOM_V_sweep.py` using the NI detector wiring. Its current settings are a 60-by-60 grid from -200 V to +200 V and 0.1 seconds per point. The script requests permission to save a CSV after the sweep, and writes into the process's working directory; move the saved result into `data/eoms/` deliberately.
2. Open the notebook in VS Code with the experiment Python kernel. Set its input directory/file and `N` to match the acquired sweep. The notebook currently names `data/eoms/Detector_Traces_9_30_pm_Tint_0.1.csv` explicitly; it does not automatically select the newest run.
3. Review the fitted curves and residual behavior before using the reported voltages. Enter confirmed values into the experiment GUI and include the calibration filename in run notes.

The notebook's Alice array is a sequence of phase steps. **Opposite polarization pairs are entries 0/2 and 1/3**, so the array cannot simply be labeled H, V, R, L in its listed order. Verify physical state labels, Bob's basis labels and detector outcomes with the optical measurements; the fit alone does not establish circular handedness. Keep unconfirmed measurements labeled S0-S3.

The notebook currently has a reporting typo in the detector-1 R-squared calculation: its total sum of squares uses `mean(z0)` instead of `mean(z1)`. This affects that displayed goodness-of-fit statistic, not the voltages produced by `curve_fit`. The notebook and its saved outputs were preserved during repository cleanup.

For notebook use, install pandas, Plotly and an IPython/Jupyter kernel in addition to [the active controls' requirements](../requirements.txt). The archived Vpi scripts additionally use Dash; the active controls do not need Dash.

## Old Vpi analysis

The former top-level `Vpi_sweep_analysis/` only contains a bytecode cache, not a maintained analysis script. It is left untouched during this cleanup. The actual historical scripts are in [`archived/Vpi_sweep_analysis/`](../archived/Vpi_sweep_analysis/): they inspect `Vpi_Sweep_LP_*_deg.csv` files from older polarizer-angle sweeps and are unrelated to the current two-EOM fitting workflow.
