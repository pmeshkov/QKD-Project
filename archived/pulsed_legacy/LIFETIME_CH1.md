# CH1 lifetime measurement

`ph330_lifetime.py` measures photon arrival delays from laser SYNC using
front-panel **CH1 (SDK channel 0)**. It disables the other detector channels,
so CH2 does not need to count. No NI-DAQ, EOM or laser output is controlled.
The laser must already be pulsing and its timing output connected to SYNC.

The script reuses our T3 raw acquisition, error checks and metadata recording.
It processes the **whole saved file**, using bounded memory, to build the CH1
decay histogram. `--acquire` records and plots; `--rates` only configures inputs
and reads count rates. Neither option means an offline configuration check.

## First run

Close UniHarp, use the existing `niDaqENV` Python environment, and run commands
from the repository root. Detector and SYNC amplitudes at the PicoHarp must be
within the established input limits after electrical attenuation. A trigger
threshold is not an attenuator. The current SYNC signal/polarity must be selected
explicitly, rather than inherited from the earlier negative-TRG-OUT experiment.

For a **positive attenuated TTL SYNC pulse** well above +200 mV, at **2 MHz**:

```powershell
python pulsed/ph330_lifetime.py --sync-edge rising --sync-level-mv 200 --rates
python pulsed/ph330_lifetime.py --sync-edge rising --sync-level-mv 200 --seconds 60 --acquire --show
```

For a verified **negative TRG OUT pulse near -1 V into 50 ohms**, replace the
SYNC options with `--sync-edge falling --sync-level-mv -500`.
These are starting thresholds conditional on the measured signals, not a new
calibration. CH1 uses a rising edge at +300 mV by default; adjust with
`--ch1-level-mv` to suit the attenuated SPCM waveform.

Default laser rate is 2 MHz (500 ns period). For another rate supply
`--laser-hz <Hz>`. The script checks the measured SYNC rate before recording.
If the microtime range is too short for the laser period it stops with a message
to increase `--binning`. Default binning code 6 gives 64 ps on the verified
device; actual hardware resolution is always queried and recorded.

`--show` opens the plot after acquisition. Without it, the image is still saved.
During acquisition the terminal reports progress; plotting is performed afterward.

## Where the data go

Each acquisition creates `pulsed/runs/lifetime_ch1/<UTC timestamp>/` in this
repository, regardless of the terminal's current directory:

- `events.t3raw` and `metadata.json`: original timing records and settings.
  Keep together; this is not a PTU file.
- `lifetime_ch1.csv`: full native-resolution decay histogram, with left/right
  bin edges in ns and CH1 photon counts. Includes zero-count bins.
- `lifetime_ch1.png`: linear and logarithmic views of the decay. Zero counts
  remain in the data but cannot be displayed on a logarithmic axis.
- `lifetime_ch1.json`: counts, processing details and optional fit results.

The plot combines eight native bins by default (512 ps for a 64 ps native bin).
`--rebin 1`, `--rebin 8` or `--rebin 16` changes plotting/fitting bin width;
the native CSV and raw timestamps are preserved. Processing an interrupted or
corrupted acquisition is refused; its raw prefix remains available for inspection.

## Getting a lifetime number

The first run saves the decay without automatically claiming a lifetime. Inspect
the peak, then choose an interval on the falling tail after the instrument-response
region and before the next excitation. Cable delays shift the peak along the time
axis; the delay of the peak itself is **not** the fluorescence lifetime.

Analyze a saved run without touching hardware:

```powershell
python pulsed/ph330_lifetime.py --analyze "pulsed/runs/lifetime_ch1/YOUR_RUN_FOLDER" --fit-window 40 150 --show
```

The example 40-150 ns window must be replaced with one appropriate to the observed
decay. This updates the derived plot, CSV and fit summary in that run folder,
without altering raw records or metadata. It also works on CH1 from an existing
two-detector g2 run; CH2 photons are ignored.

The optional fit uses `A exp(-(t-t0)/tau) + background` with a Poisson count
likelihood, including empty bins. It reports **tau in ns** and plots residuals.
Flat/low-count traces, optimizer failures and poorly resolved boundary solutions
do not receive a lifetime estimate. The flat-model comparison is a diagnostic
heuristic, not a formal confidence interval. A returned value is preliminary:
inspect residuals and fit-window sensitivity. Multiple decay components can
make a single exponential inadequate.

No instrument-response reconvolution, background correction of the raw data,
pile-up correction, or lifetime uncertainty analysis is performed. Short lifetimes
comparable to the instrument response need a measured IRF and a more complete fit.
See [PicoQuant's lifetime-fitting tutorial](https://www.picoquant.com/images/uploads/downloads/lifetime-fitting_using_the_flim-script_step_by_step.pdf).

## Verification

Offline tests cover CH1-only configuration, CH2 disabled/zero count rate,
raw-file integrity, histogram count conservation, recovery of a known synthetic
18 ns decay and rejection of a flat trace. All 25 PicoHarp tests passed.
The wider suite also reports existing laser-clock failures: that script currently
calculates with 1 GHz while routing a 100 MHz hardware timebase. The lifetime
script does not use it, and its contents were left unchanged in this task.
The new lifetime path has not yet
been physically acquired during development; the underlying two-channel T3
acquisition was previously tested on this PicoHarp.

```powershell
python -m unittest discover -s pulsed -p "test_*.py" -v
```
