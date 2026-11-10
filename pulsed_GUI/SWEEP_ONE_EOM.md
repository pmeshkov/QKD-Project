# Sweep one EOM

Select **Sweep one EOM** in the GUI, or run `sweep_one_eom.py` to open its form.
No prior calibration is needed.

1. In **Voltages**, select Alice (AO0) or Bob (AO1) as the swept EOM.
   Enter the other EOM's fixed target voltage within −200 to +200 V.
2. Set the number of voltage points, including both endpoints (2–1000).
   The sweep always runs from −200 to +200 V in ascending order.
   The spacing is `400 / (points - 1)` V: the default 101 points gives 4 V spacing.
3. In **Recording**, set dwell time per point (0.001–3600 s, whole milliseconds)
   and settling time. Defaults are 1 s dwell and 10 ms settling. Both detectors
   are counted simultaneously; settling time is excluded from the counts.
   Set **Number of sweeps (N)** to repeat the scan (default 1), and **Dead time
   after returning to -200 V between sweeps (s)** (default 1 s, range 0–3600 s).
   Each sweep runs upward from −200 to +200 V. Before the next sweep, the swept
   EOM is set back to −200 V, then held for the dead time **after** that reset.
   Normal per-point settling follows this extra delay. Neither wait is counted.
   No inter-sweep delay is added before the first sweep or after the last one;
   the other EOM remains at its fixed voltage throughout all repeats.
4. Review the laser source, PicoHarp configuration and output directory. CW
   and internal pulsed modes use the existing manual laser/PFI12 confirmation.
5. Choose **Check settings**, then **Run sweep**. **Stop** preserves completed
   rows in a partial CSV. Both analog outputs return to zero on completion,
   Stop or error; the NI trigger clock is also stopped when used.

Each run saves a timestamped folder under `data/eoms/single_eom/` by default.
The main file is `<timestamp>_Single_EOM_Counts.csv`, a UTF-8 CSV with one
header row and one row per completed voltage dwell. All repeats share this file.
When N > 1, an additional first column, `Sweep_Number`, identifies the sweep
(1 through N); it is omitted when N = 1. `Point` restarts at 1 within each sweep.

| Column | Meaning |
| --- | --- |
| `Point` | Voltage point number within the sweep, starting at 1 |
| `Timestamp_UTC` | Timestamp at completion of the dwell/FIFO readout, with UTC offset |
| `Elapsed_Time_s` | Elapsed scan time at row recording, including settling/readout |
| `Alice_Target_V` | Alice EOM target voltage, in V |
| `Bob_Target_V` | Bob EOM target voltage, in V |
| `Dwell_s` | Actual PicoHarp measured collection time, in s |
| `CH1_Counts` | Integer CH1 photon count for this dwell only |
| `CH2_Counts` | Integer CH2 photon count for this dwell only |

Counts are **not cumulative**. A JSON run record saves settings, measurement
notes, repeat count, inter-sweep dead time, completion status and cleanup results;
the duration estimate includes all dwells, normal settling and N−1 dead times.
Stop also interrupts an inter-sweep wait and preserves all completed rows.
The per-point JSONL retains
exact counts, dwell and device flags. Failed/stopped runs use `.partial.csv`
and exclude unfinished dwells. No raw TTTR file, fit, analysis or plot is
generated. This CSV is separate from the existing 2D calibration CSV format.
