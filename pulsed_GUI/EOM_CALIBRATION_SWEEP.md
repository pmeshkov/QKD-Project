# EOM calibration sweep using PicoHarp

Open the root `launch.py` / **Start QKD.cmd** and select **EOM calibration sweep**.
You can also run `pulsed_GUI/eom_voltage_sweep.py` directly in VS Code; it opens the same GUI.

NI drives AO0/Alice and AO1/Bob. Detector signals stay on PicoHarp CH1/CH2.
No NI detector counters or PFI1/PFI2 are used. Close UniHarp and other programs
using AO0/AO1, PicoHarp or (for external laser mode) Ctr0.

## First run

1. **Run:** leave the source as **Laser CW (manual)**. Manually disconnect PFI12
   from the laser's SYNC IN and select CW on the laser controller. Set the
   manual confirmation to Yes. Software does not change the laser controller.
2. Set **N — voltage values per EOM** in the Run tab (default 60; range 2–1000).
   There are N² voltage pairs. Set the notebook's `N` to the same value.
   Default collection is **0.1 seconds per voltage pair**, preceded by a separate
   **10 ms settling pause**. At N=60, the 3,600 settings take at least 6.6 minutes, plus
   USB/acquisition overhead. Increase collection time for sparse counts.
3. **PicoHarp:** review serial/DLL and input settings. CH1 and CH2 default to
   **+200 mV rising**, SYNC to falling. Saved polarization input settings seed
   this profile on its first use. CW does not require SYNC or a binning choice.
4. **Files:** output defaults to `data/eoms`, where the notebook already looks.
   Add notes describing illumination, power and optical configuration.
5. Select **Check settings → Run**, then **Run sweep → Run**. Opening the window
   and checking settings do not access hardware. The log reports progress,
   commanded voltages, both count rates and file locations. A two-panel rate
   heatmap opens after a successful scan.

CW uses timed T2 acquisitions, counting detector events while ignoring special
records. Timestamps are processed in bounded blocks and discarded; this does
not create large raw T2 files. If CW mode still produces a periodic signal at
PicoHarp SYNC, the startup check stops before counting. If the laser's timing
output remains active in CW, disconnect **BDL TRG OUT → PicoHarp SYNC** for this
measurement. Detector cables stay in place. Reconnect SYNC for pulsed work.

For pulsed illumination select **Laser internal 2/20/80 MHz** (manual controller
changeover), or **NI external** (software generates Ctr0/PFI12). Pulsed counting
uses T3 and checks the input SYNC rate. Binning must cover the laser period.
External rate and pulse-width fields are enabled only for NI external mode.
This is a static calibration; it does not test pulse-by-pulse settling.

## CSV compatibility

The scan preserves the existing notebook's grid construction and order:

- N voltages per EOM, inclusive `linspace(-200, 200, N)`; N defaults to 60.
- Alice runs from -200 to +200 V at each fixed Bob voltage; Bob varies slowly.
- DAQ commands use the original sign/gain: `-target_voltage / 20`.
- CSV header, column order and numerical row format are unchanged:

```text
Unix_Timestamp,Elapsed_Time_s,EOM0_Target_V,EOM1_Target_V,Raw_Count0,Raw_Count1,Rate0_Hz,Rate1_Hz
```

`Rate0_Hz` is CH1 and `Rate1_Hz` is CH2. Both are actual detected photons divided
by the PicoHarp-reported integration time. These are total singles rates, without
an arrival gate. No estimates are derived from live rate-meter readings.

`Raw_Count0/1` are cumulative integer counts across completed counting windows.
Unlike the old free-running NI counters, they exclude settling and gaps between
acquisitions. They are not per-point counts. The existing notebook uses only
the rate columns, so its analysis needs no changes. Per-point counts and measured
integration times are also saved separately in the points JSONL file.

Each new sweep has its own folder, `data/eoms/<sweep timestamp>/` (or beneath
your configured output directory). For a successful run, that folder contains:

- `<timestamp>_Detector_Traces.csv`: exactly N² numerical rows, ready for the notebook.
- `<timestamp>_Detector_Traces.png`: measured CH1/CH2 rate surfaces.
- `<timestamp>_sweep.json`: settings, grid, completion/cleanup status and file names.
- `<timestamp>_points.jsonl`: per-point counts, actual exposure and acquisition flags.

Running calibration analysis adds `analysis/<analysis timestamp>/` inside the
same sweep folder. Reanalysis creates another analysis folder without replacing
previous results. CSV names, columns and sidecar conventions are unchanged, so
the app handoff and existing notebooks remain compatible. Existing data files
are left in place.

To process within the app, click **Analyze last completed sweep →**, review the
search settings, and click Run. **EOM calibration analysis** reads N from the CSV
and can load the resulting six voltages directly into polarization/BB84 forms.
See [calibration analysis](EOM_CALIBRATION_ANALYSIS.md).

The notebooks still work. In the original fit notebook, select the new CSV and
set `N` to the GUI value. `CalibrateEOMVoltages.ipynb` detects the grid size itself.
The saved sweep JSON records N as `settings.points`. Existing CSV files are never overwritten.

During acquisition the file is named `.partial.csv` and flushed after every
completed voltage pair. Stop/failure retains that file; a partial dwell is not
exported as a full point. Do not load partial scans into the existing fixed-grid
notebook. The normal filename is assigned only after the full scan and successful
cleanup. Stop and errors attempt to stop the counter (if used), zero both AOs and
close PicoHarp. A manually controlled laser remains under manual control.

This implementation has offline simulated-device tests. Verify a short dwell
and the displayed counts on the bench before relying on a calibration fit.
The original NI-counting workflow remains available as `EOM_scripts/niDAQ_V_sweep.py`.
