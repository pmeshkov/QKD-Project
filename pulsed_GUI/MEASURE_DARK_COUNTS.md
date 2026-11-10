# Measure Dark Counts

Select **Measure Dark Counts** in the GUI, or run `measure_dark_counts.py`.
In **Run**, set the counting interval duration and number of intervals.
Defaults are 60 intervals of 1 second. Intervals support 0.001–3600 seconds
in whole milliseconds; the number of intervals supports 1–1000000.

Shutter the laser and set **Laser shuttered — confirm before recording** to
**Yes**, then choose **Record** and click **Run**. Recording is blocked while
the dropdown is **No**. This is a user confirmation, not an electronic shutter
sensor. The confirmation resets when reopening the tool and after submitting
a recording. **Check settings** validates without accessing hardware.

Review the CH1/CH2 trigger settings in **PicoHarp** and the output directory
in **Files**. Acquisition uses PicoHarp T2 without requiring a SYNC signal;
it does not access the NI DAQ, drive the EOMs, or control the laser/shutter.

Each timestamped run folder under `data/dark_counts/` contains a UTF-8 CSV:

| Column | Meaning |
| --- | --- |
| `Interval_Index` | Measurement interval number, starting at 1 |
| `Interval_Start_UTC` | Host UTC timestamp immediately before starting the interval |
| `Start_Elapsed_s` | Host elapsed time immediately before the interval |
| `Measured_Interval_s` | Actual collection duration reported by PicoHarp |
| `CH1_Counts` | Integer CH1 photons within this interval |
| `CH2_Counts` | Integer CH2 photons within this interval |

Both channels are counted simultaneously. Counts are not cumulative, and
zero-photon intervals are saved as zeros. Short readout/restart gaps between
intervals are excluded from the counts; total wall time therefore exceeds
interval duration × interval count. No analysis, plots or raw TTTR files are
produced. `measurement.json` records settings, shutter confirmation and status.
**Stop** or an acquisition error retains completed intervals in `.partial.csv`
and closes the PicoHarp. Incomplete intervals are not saved as full intervals.
