# EOM calibration: fit, transfer, verify

1. Finish **EOM calibration sweep**, then click **Analyze last completed sweep →**.
   Or choose **EOM calibration analysis → Fit sweep** and select the completed CSV.
2. Review the Alice/Bob absolute voltage limits under **Voltages** (default 180 V)
   and Vpi search bounds under **Analysis** (50–400 V). Limits are EOM target volts.
   They are software operating margins, not certified hardware limits. No selected
   voltage can extrapolate outside the measured range or reach ±200 V.
3. Click **Run**. Inspect both fitted responses and residuals, then the predicted
   eight-setting fractions. The app opens the fit and predicted-fraction plots.
4. If the fit is identified and all predicted fractions meet the configured
   tolerances, use **Load last completed result into Polarization matrix →**.
   Imports fill polarization and both BB84 profiles, without starting hardware.
5. Record a fresh eight-setting polarization run at these exact voltages.
   Return to **EOM calibration analysis → Verify fitted voltages**, select the
   original fitted `analysis.json` and the new polarization run folder, then Run.
   Browse / Use latest help locate both. Verification does not change the voltages.

## Model and selection

All recorded per-setting counts enter a joint Poisson maximum-likelihood fit:

    theta = kA * VA + kB * VB + phi
    R1 = C1 + A1 * sin(theta)^2
    R2 = C2 + A2 * cos(theta)^2
    expected counts = recorded dwell * R

The phase is shared; each detector has its own amplitude and constant background.
This retains complementary sinusoidal physics while allowing unequal detector
rates. Background also absorbs unmodulated light/finite contrast; it is not
identified uniquely as detector dark counts. We use actual CSV voltage columns.
FFT and multiple frequency/phase starts initialize the nonlinear fit. No measured
point is selected because its noisy value happened to be closest to a target.
There is no count splitting, training subset, or held-out subset.

The four Alice / two Bob roles have CH1-fraction targets:

| Alice role | Bob 1 | Bob 2 |
|---|---:|---:|
| S0 (provisional H) | 0 | 0.5 |
| S1 (provisional R) | 0.5 | 0 |
| S2 (provisional V) | 1 | 0.5 |
| S3 (provisional L) | 0.5 | 1 |

We minimize the sum of squared errors of **R1/(R1+R2)** over these eight cases.
Each target must also satisfy its explicit tolerance: default ±0.05 around 0.5,
and at most 0.15 wrong-detector fraction for 0/1 targets. A constrained phase
search is attempted if the least-squares solution misses a tolerance. These are
editable acceptance requirements, not statistical weights or security claims.
Detector imbalance/background can make simultaneous requirements impossible;
a high R² does not prove all targets are attainable.

After selecting phases, we enumerate periodic wraps and equivalent allocations
of phase between Alice and Bob. Within each valid interval the sum of six squared
voltages is a quadratic, whose minimum is evaluated exactly. This implements
Peter's minimum-voltage objective without a local search across wrapping jumps.
It minimizes voltage among equivalent solutions, without trading optical error
for voltage. The phase/frequency fits use numerical multistart optimization, not
a proof of a global optimum over arbitrary models. If no phase-equivalent solution
fits the operating margins and measured ranges, the analysis reports that and
exports diagnostics rather than an out-of-bounds candidate.

Periods can change voltage order/sign. S0/S1/S2/S3 denote response roles, not
ascending voltages. Reflection-equivalent solutions also cannot establish R/L
handedness. Imports reset label confirmation. Fraction balancing concerns detected
counts; it does not by itself establish equal optical power, exact quantum-state
orthogonality, or BB84 security under detector-efficiency imbalance.

## Diagnostics and uncertainty

The report saves both R² values, Poisson deviance/dof, an approximate chi-square
Poisson goodness-of-fit p-value, local parameter covariance, fit bounds, source
hashes, and the fitted predictions. A p-value below 0.001 is a diagnostic warning,
not a hardware limit or a claim that the sinusoidal model is physically wrong.
Drift or excess noise can cause it. Sparse counts and parameter boundaries also
limit the chi-square approximation. Parameter covariance is a local Poisson
approximation; it does not account for drift or model discrepancy. In particular,
the app does not present it as the uncertainty of experimentally verified voltages.

Unidentified fits, a Vpi at its search boundary, very low contrast, unsatisfied
fraction tolerances, or infeasible voltage margins prevent candidate handoff.
A residual warning remains visible on an otherwise eligible **unverified**
candidate so it can be tested independently. Do not adjust tolerances merely to
obtain a PASS. An unsuccessful new fit never silently loads an older result.

Verification reads every T3 record of a complete polarization session, matches
all eight exact voltage pairs and physical CH1/CH2, and uses ungated counts.
It compares measured fractions to the same tolerances frozen in the fit report.
Exact conditional-binomial Clopper-Pearson intervals receive a Bonferroni
correction across eight pairs (default simultaneous confidence 95%). All eight
intervals must lie wholly within their tolerances to pass. This can require more
counts when a prediction lies close to a tolerance boundary. A failed check can
mean insufficient counts or an actual deviation: inspect the value and interval.
Intervals assume stationary independent detection; they do not cover drift or
voltage errors. Fit and verification conditions are both saved; verification only
establishes performance in its own conditions. No automatic retuning follows it.

## Files

New fit outputs: `data/eoms/<sweep>/analysis/<analysis timestamp>/`.
Old flat CSVs retain per-source grouping under `<CSV parent>/analysis/<stem>/`.
Keep `_sweep.json` and `_points.jsonl` alongside PicoHarp CSVs. Cumulative CSV
counters are not per-setting counts. Legacy NI files require explicit assumed
dwell and are marked provisional. At least six voltages per axis are required;
the Vpi lower bound must exceed the largest scan spacing to avoid unresolved fringes.

- `analysis.json`: model, parameters, covariance, diagnostics, constraints,
  source provenance, predicted candidate, and transfer eligibility.
- `predicted_fractions.csv`: eight model predictions, explicitly not measurements.
- `calibration_for_app.json`: only for eligible candidates; includes diagnostics.
- PNG/PDF: response fit and residual maps, scan-order residuals, predicted
  fractions/matrix, and detector-response slices. DPI is editable in Analysis.

Verification outputs stay with the polarization run:
`data/polarization/<run>/analysis/calibration_verification/<timestamp>/`.
They include the frozen fit hash, source raw-data hashes/settings, count table,
verification plot, and an updated transfer carrying the independent check result.
No raw files or earlier results are overwritten. Nothing is applied to hardware
or to measurement profiles until an explicit calibration import.

`EOM_scripts/CalibrateEOMVoltages.ipynb` uses this same workflow. Peter's notebook
and older personal notebooks remain untouched. `eom_calibration_core.py` retains
the validated loader and legacy API for historical notebooks;
`eom_model_calibration.py` owns the current fit/optimization,
`eom_calibration_verify.py` owns independent checks, and
`eom_calibration_figures.py` renders diagnostics.
