# EOM calibration analysis in the main app

1. In **EOM calibration sweep**, finish a sweep. Click **Analyze last completed
   sweep →** below the form. The app switches to **EOM calibration analysis**
   and selects that CSV. For existing data, choose this mode directly and use
   **Browse** or **Use latest** beside the CSV field.
2. Review the search centers/radii under **Voltages**, and the statistical
   settings under **Run**. Defaults match `CalibrateEOMVoltages.ipynb`:
   Alice centers −165, −61, 43, 147 V; Bob centers 30, −68 V; radii 35 V;
   0.05 balance tolerance, 0.15 extinction tolerance, at least 50 training
   photons per pair, 70% training counts, 95% simultaneous confidence.
3. Click **Run**. The app reads the grid size from the voltage columns, selects
   six measured voltages jointly, checks held-out counts, saves the results,
   and opens the held-out check and selected-matrix figures. The complete
   output folder also contains response surfaces, total-rate history, and
   local response curves, all in PNG/PDF. **Analysis → Saved figure resolution**
   controls PNG DPI. **Open data folder** takes you to these exports.
4. Review the voltages, fractions, and check results. Click **Load last completed
   result into Polarization matrix →**, **Repeating H-V-R-L →**, or **Random BB84 →**.
   This switches to that form and fills the six EOM target voltages. It does not
   start instruments. Those modes also have **Load latest app calibration**
   under Voltages, so the handoff works after an app restart.

For new sweeps, results are saved inside their source run:
`data/eoms/<sweep timestamp>/analysis/<analysis timestamp>/`.
Leave **Optional analysis directory override** blank for this default; enter a
directory only when intentionally exporting elsewhere. The former factory
`data/eoms/calibrations` setting automatically changes to the new default.
Custom overrides are preserved. Each analysis creates its own folder:

- `analysis.json`: source hashes/settings, parameters, selected candidate,
  full-data/held-out checks, independent-check status, and calculation-core hash.
- `calibration_for_app.json`: six-voltage handoff with provenance/check status.
- `training_candidates.csv`, `selected_measured_points.csv`, `heldout_checks.csv`.
- Optional `independent_checks.csv` and proposed unmeasured refinement points.
- Five diagnostic figures in PNG/PDF.

Existing measurements are not moved. When analyzing an older flat CSV, results
go under `<CSV parent>/analysis/<CSV timestamp or stem>/<analysis timestamp>/`
so separate old sweeps cannot mix. Recent-file selection and calibration loading
support both layouts, including historical results in `calibrations/`.

The algorithm is the notebook's measured-grid algorithm, not a new fit. Training
counts alone select/rank the candidate. Do not adjust the split seed or select
candidates by their held-out result. A completed analysis may still report
**NOT VALIDATED** or a compromise with no training-feasible set. Loading a
candidate preserves those results and resets physical-label confirmation.
H/R/V/L remain provisional aliases for S0/S1/S2/S3; optical verification is
still needed. A 50:50 detected fraction is not automatically equal optical power.

Keep PicoHarp `_sweep.json` and `_points.jsonl` beside the completed CSV. Partial
sweeps are refused. For old NI CSVs without sidecars, explicitly enter an assumed
dwell in **Legacy NI CSV only**; uncertainties are then approximate and cannot
pass the exact-count validation. The optional independent sweep must measure
the same selected voltage pairs exactly and must not reuse the same data.

## Recent file selections

The app remembers completed run locations across restarts. Blank analysis inputs
are initialized from the last compatible run. Existing nonempty selections are
preserved; **Use latest** explicitly replaces them. When no usable remembered run
exists, the app searches the configured output directory and the normal data
directory. Deleted files, incomplete sweeps and incompatible recording types are
skipped. A polarization run with completed blocks can be selected even if later
blocks were interrupted.

Polarization, lifetime, g² and raw T3 preview modes also offer recent-recording
shortcuts. Browse starts from the current selection, recent compatible data, or
its containing data folder. The optional independent sweep is never automatically
filled. Automatic selection never starts analysis or hardware; review the visible
path and click Run.

## Code ownership

`eom_calibration_core.py` holds the calculations; `eom_calibration.py` manages
forms/reports, and `eom_calibration_figures.py` renders diagnostics. The app has no
runtime dependency on `EOM_scripts/`. Its existing `calibration_analysis.py`
forwards notebook imports to the shared core, so the notebook remains usable.
