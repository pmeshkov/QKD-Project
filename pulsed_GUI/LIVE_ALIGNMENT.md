# Laser clock + EOM + live PicoHarp counts

Open `launch.py` or `gui_app.py`, choose **Live alignment**, or run
`laser_clock_eom_counts.py` directly in VS Code. Opening the form does not
access hardware.

## Start

1. Close UniHarp and other programs using PicoHarp, AO0/AO1 or Ctr0. This single
   tool owns all three interfaces; the separate laser clock GUI is not needed.
2. Wiring stays unchanged: NI Ctr0/PFI12 to BDL SYNC IN through your tested
   interface; BDL TRG OUT to PicoHarp SYNC; PBS detectors to PicoHarp CH1/CH2;
   AO0 to Alice and AO1 to Bob. The laser must be ready for external triggering.
3. Set the initial rate (default 500000 Hz), trigger width (100 ns), and EOM
   targets (default 0 V). The supported bench range is 1 kHz–1 MHz, using the
   existing NI 100 MHz timebase. This is a software/bench limit, not a claim
   that PFI12 is fundamentally limited to 1 MHz. Internal 2/20/80 MHz operation
   still requires manual changeover and is outside this tool.
4. Check the PicoHarp serial, input modes, thresholds and trigger edges. On the
   first use, matching fields are copied from your saved **G2 acquisition**
   form. Subsequently this tool remembers its own settings. It supports both
   edge and CFD input modes. Offsets default to SYNC 0, CH1 0, CH2 +4.5 ns;
   they are configured/logged, although this display does not measure delays.
5. Select **Check settings → Run** for offline validation. Then select
   **Start live controls → Run**. A second window opens; the program connects
   to PicoHarp, reserves NI outputs, applies the initial voltages and starts
   the clock. Controls become available after hardware acknowledgement.

## Live window

- CH1 and CH2 show detector count rates, SUM their combined rate, and SYNC the
  measured laser reference rate. A status line reports whether SYNC is within
  5% of the commanded NI rate and shows PicoHarp warnings. Missing SYNC or zero
  detector counts remain visible so you can troubleshoot; they do not stop the
  alignment session. SDK errors stop the session and initiate cleanup.
- Enter **Alice** and **Bob** target voltages and press Enter or click
  **Apply both EOM voltages**. Both boxes are applied together. As in
  `EOM_scripts/EOM_gui_log.py`, DAQ volts = **-EOM target/20**. Targets outside
  ±200 V are rejected rather than clipped. The clock continues during changes.
- Enter the **laser rate** and **trigger high time**, then press Enter or click
  **Apply clock (brief restart)**. The counter stops and restarts while the
  EOM biases are held. The displayed applied frequency includes 10 ns tick
  rounding: a request for 750000 Hz gives approximately 751879.7 Hz.
  Realized trigger duty cycle must remain at or below 30%.
- The applied-command line updates only after a successful hardware write;
  it is not a measured high-voltage readback. Invalid values leave outputs
  unchanged. A hardware failure during an adjustment stops the session.
- **Log scale** and **Show combined trace** provide the same display choices
  as the original EOM alignment script. Zero counts remain zero in the numeric
  display; they are omitted from the logarithmic trace.
- **Stop outputs and counts**, the launcher's Stop button, or closing either
  window requests cleanup: stop the counter, zero AO0/AO1, release NI tasks,
  close PicoHarp. Driver calls finish before cleanup. Errors during cleanup
  are reported, and the remaining cleanup operations are still attempted.

The last successfully applied voltage/rate settings are remembered in the
launcher for your next session; stopping still commands both AOs to zero.

## Rates and files

The default display polls the PicoHarp rate meters every **200 ms** and
averages the readings over the last **0.5 seconds**, with **30 seconds** of
visible history. Refresh, smoothing and history are configurable in the
launcher before starting. Set smoothing to 0 for the latest meter reading.
This is an average of rate-meter samples, not an adjustable photon-integration
gate. SYNC always displays the latest reading.

PicoQuant specifies at least 100 ms between fresh rate-meter readings. The
code waits at least 200 ms after starting or adjusting outputs and resets
display smoothing after each change. Display timestamps are host times.
See [PH330Lib manual, rate-meter functions](https://downloads.picoquant.com/manuals/PicoHarp330_DLL_Manual.pdf).

Each session saves to `data/alignment/<UTC timestamp>/`:

- `rates.csv`: every sampled, unsmoothed SYNC/CH1/CH2/sum rate, host time,
  applied voltage/rate settings, setting epoch and warnings.
- `events.jsonl`: startup, requested/successful/rejected adjustments and input
  status changes. Rate changes intentionally introduce a pulse-train gap.
- `session.json`: initial settings, device information, last applied commands,
  sample count, exit status and cleanup results.

The plot history is bounded; the CSV continues for the full session. **Open data
folder** becomes available in the launcher after stopping. This alignment tool
does not start a TTTR measurement, save individual photons, or establish a
BB84 pulse-index mapping. It uses only NI AO0/AO1 and Ctr0/PFI12; existing NI
detector input counters/PFI pins are not used or reassigned. No device reset
is performed. `live_counts_window.py` is the display helper, not a separate
hardware-control program.
