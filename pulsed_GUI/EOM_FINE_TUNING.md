# EOM calibration fine-tuning

This optional second sweep refines one selected pair after a completed **EOM
calibration analysis**. It sweeps **one EOM** while holding the other at its
selected pair voltage, then uses the swept EOM to compensate the combined
phase drift. It uses the same static AO/PicoHarp counting backend as
the full sweep. Both detectors are counted together over hardware-timed dwell
windows; settling time is excluded. It saves counts, not large TTTR files.

## Run it

1. Open **EOM calibration fine-tuning**, or click **Fine-tune one pair from the
   last global fit →** in calibration analysis.
2. In **Voltages**, select the previous global `analysis.json` (not the CSV or
   `calibration_for_app.json`). **Use latest** finds the latest global fit. It
   supplies the six voltages, phase, half-wave voltages, response amplitudes and
   backgrounds, target tolerances and operating bounds.
   To start from a rejected candidate, set **Override: allow rejected global
   voltage candidate** to **Yes** in **Voltages** (default **No**). The source
   analysis must still be completed, contain a selected voltage set and have
   an identified fit with valid parameters and voltage bounds. The log and
   saved provenance record the override and original rejection reason. This
   permits acquisition; the new fit, all eight predicted targets and fresh
   selected-pair verification must still pass before an app transfer is exported.
3. Select an Alice role S0–S3 and Bob setting 1–2. The dropdown explicitly shows
   the expected **CH1 / total** fraction; physical H/R/V/L labels remain
   provisional. Default: S0 / Bob 2, a 50:50 target.
4. Select **Bob (AO1)** (default) or **Alice (AO0)** as the swept EOM / compensator.
   Choose voltage step and steps each side. Defaults: **2 V steps, five each
   side**: **11 points, ±10 V** around that EOM's selected voltage. The other
   EOM stays fixed during the scan. Every scan voltage must remain inside the
   prior operating/measured range and the new absolute limit (default 180 V).
   The tool rejects an oversized scan instead of clipping it.
5. In **Recording**, choose per-point collection and fresh verification time.
   Defaults: **10 s per point**, **10 ms settling**, **30 s verification**, 95%
   confidence. This is roughly 2.3 minutes plus device/FIFO/setup overhead.
   Existing saved profiles retain their chosen dwell; increase it in Recording
   to spend the saved scan time averaging noise. Old mesh step/span settings
   migrate to the new single-EOM controls.
   Increase dwell for low counts. A matched-basis extremum has less local phase
   sensitivity than a 50:50 crossing, so it can need more span or exposure.
6. Select the illumination source and review PicoHarp settings. CW and internal
   2/20/80 MHz operation require the normal manual laser setting and PFI12
   disconnection confirmation. NI external mode supplies the trigger clock.
   Keep the optical path/detector configuration consistent with the prior fit.
7. Run **Check settings**, then **Run fine-tuning + verification**. One click
   performs the local sweep, fit, continuous voltage selection, and fresh
   measurement at the frozen optimized pair. Stop preserves partial results.
8. Inspect the automatically opened figure and log. If both the selected-pair
   verification and all eight model predictions pass their tolerances, use a
   **Load last completed result into…** button to import all six voltages into
   the polarization and both BB84 profiles. This never changes hardware outputs.

AO0/AO1 return to zero and the NI clock stops between acquisition stages and on
exit. The verification stage reopens the instruments and reapplies the candidate.
No fresh verification counts are used to refit or select it.

For `n` voltage values, the local acquisition now uses `n` dwells instead of
`n²`. At equal dwell this is an **n-fold scan speedup**, or permits about **n
times longer integration per voltage** for the same scan budget. The separate
verification dwell and device setup overhead are unchanged. For the default
11 values, 121 short dwells become 11 longer dwells.

## What is fitted and inferred

The previous global fit is

`CH1 = c1 + a1 sin²(kA VA + kB VB + phi)`

`CH2 = c2 + a2 cos²(kA VA + kB VB + phi)`.

The fine sweep keeps `kA`, `kB` (hence both Vpi values) fixed. One call to
`scipy.optimize.curve_fit` jointly refits **phi, a1, c1, a2, c2**, using their
previous global values as `p0`. Thus each detector's amplitude and background
can change independently. It fits rates with estimated counting uncertainties
`sigma = sqrt(max(counts, 1)) / actual_dwell_seconds`, using
`absolute_sigma=True`. Amplitudes and backgrounds are constrained nonnegative.
Both detectors contribute to the same fit; no phase-grid search or profile
likelihood is used. Residuals and the fresh check assess agreement locally;
they cannot establish that the model remains valid outside the scanned region.

The phase correction is restricted to the local branch ±pi/4. Parameter
uncertainties come from the covariance matrix returned by `curve_fit`; an
approximate Gaussian 95% phase interval must fit inside that branch, with
finite, full-rank covariance. The weighted residual statistic has `2*n - 5`
degrees of freedom; fits with approximate chi-squared goodness-of-fit p < 0.001
are rejected. Covariance and chi-squared checks are approximations, especially
with sparse counts or fitted parameters at bounds. They do not propagate the
prior Vpi uncertainty or account for drift/voltage errors. A narrow scan can
leave phase, amplitudes and backgrounds strongly correlated; increase span
or counts if the resulting uncertainty is too large.

For 50:50, selection solves equality of the **two fitted detector rates**, not
half of one detector's peak. For a 0/1 target it finds the appropriate fitted
extremum; nonzero backgrounds can prevent an exactly zero unwanted fraction.
The target must occur within the measured voltage interval at the other EOM's
fixed voltage. Voltages are continuous model solutions, not restricted to
sampled values.

A single scan determines the combined phase drift, not which EOM caused it.
The tool corrects it using **only the swept EOM**: a common voltage shift is
applied to Bob's two values or Alice's four values. The other EOM's settings
stay unchanged. For an otherwise unchanged model the shift is `-delta / k`
for the selected compensator. If several target solutions lie inside the scan,
the smallest absolute common shift is chosen. Every translated setting must
remain within the saved operating bounds and current absolute voltage limit;
the selected pair must also remain inside the measured interval.
This preserves the prior relative phase separations and state roles.
This is a local refinement, not a new global phase-wrap search. It reports all
eight predicted fractions and refuses an app transfer if any misses the saved
target tolerance. If detector imbalance changes the best relative separations,
repeat the full global sweep/analysis.

The fresh check reports integer counts, actual exposure, CH1 fraction and an
exact conditional-binomial Clopper–Pearson confidence interval. **PASS** means
the entire interval falls within the prior target tolerance. Low counts can
leave it inconclusive; “NOT PASSED” does not necessarily mean the voltage is
wrong. Passing verifies **only the selected pair**. The exported calibration
retains `independent_count_check_passed=false` for the full eight-setting check.
Run a polarization matrix at the new six voltages to test the remaining pairs.

## Results

Everything for one run is inside `data/eoms/fine_tuning/<UTC timestamp>/`:

- `prior_analysis.json`: frozen copy of the source calibration; its original
  path/hash is saved in the run report.
- `local_sweep/<timestamp>/`: familiar eight-column detector CSV with `n` rows
  and one constant EOM voltage column, exact per-point count/dwell JSONL, and
  acquisition manifest. These are single-EOM data, unsuitable for full 2D
  calibration analysis.
- `verification/<timestamp>/`: separate fresh single-pair counts and manifest.
- `analysis.json`: v2 run schema, swept EOM, voltage axes, linear scan-time
  estimate, sample/filter/power/notes, phase fit and uncertainty, compensation
  shifts, new voltages, predicted fractions and verification results/provenance.
- `predicted_fractions.csv`: all eight model predictions.
- `fine_tuning.png` and `.pdf`: measured CH1 fractions with 95% Wilson intervals
  and fitted response versus swept voltage, both detector rates versus voltage,
  standardized fit residuals and independent verification (DPI is adjustable).
  The plot identifies the held EOM's voltage and selected compensator voltage;
  zero-photon dwells have no measured fraction marker.
- `calibration_for_app.json`: exported only when the checks above pass.

**Analyze saved result** regenerates PNG/PDF plots from the saved local counts
and frozen fit, using the current plotting code and selected DPI. Select a run
folder or click **Analyze latest saved fine-tuning run →**, then Run. It also
works when a fit check failed, provided the local sweep and fit were saved.
New plots go into that run's `analysis/<timestamp>/` folder and open automatically.
The original counts, fit, voltage candidate and verification remain unchanged;
this action does not refit or access hardware. Old saved “View saved result”
settings are migrated to this action. Existing v1 mesh runs remain readable
and redraw as legacy 2D plots; they are not relabeled as single-EOM measurements.
Local scans and verification CSVs are excluded from the full-sweep “latest”
shortcut, so they do not accidentally replace a global calibration sweep.

**Refit saved data (curve_fit)** applies the new fit to an existing run. Select
the run folder in the same Analyze field, choose this action and Run. It uses
the saved counts and frozen `prior_analysis.json`; no hardware is accessed.
It writes `refit_report.json`, plots and any predicted-fraction table into a new
`analysis/<timestamp>/` folder. Original files remain unchanged. A previous
verification does not verify a newly selected voltage, so the refit report
does not reuse it or export an app calibration. Legacy 2D scans can be refitted
and plotted, but no single-EOM compensation is assumed for those scans.
