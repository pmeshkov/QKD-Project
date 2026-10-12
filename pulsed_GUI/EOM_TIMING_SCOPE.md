# EOM / timing marker test

Open `python pulsed_GUI/launch.py`, select **EOM / timing marker test**.
`python pulsed_GUI/timing_marker_controls.py` opens the same form.
The old `eom_timing_scope.py` entry point also opens this form.
Opening it and **Check settings** do not access instruments.
Close other NI output tools; close UniHarp before any TCSPC recording.

## First: use your existing scope wiring

1. Keep EOM Alice monitor on scope CH1, Bob monitor on CH2, BDL TTL OUT on CH3,
   and Marker 1 breakout signal on CH4. Use high-impedance inputs for the
   HVA monitors and digital marker; no 50-ohm termination on these lines.
   Use the HVA monitor connectors, never the high-voltage drive connectors.
   Monitor scaling is nominally HV/20. TTL OUT is suitable because you already
   checked its alignment with TRG OUT; TRG OUT stays connected to PicoHarp SYNC.
2. Select **Scope only (no TCSPC recording)**. Start with **500000 Hz**, **1200 ns
   delay**, **100 ns PFI12 width**, **1 pulse per voltage level**, marker spacing
   **32 trials**. Review the A/B EOM targets: new defaults are Alice -150/+150 V
   and Bob -50/+50 V. Saved values are preserved. NI command = -target/20.
3. Click Run and trigger the scope on **CH4 rising**. This edge belongs to an
   **A→B voltage command**, not the later settled voltage. With these defaults
   the marker repeats every 64 µs and stays high for 2 µs. Zoom to a few µs
   around its rising edge; both monitor transitions should be reproducible.
4. Check that the laser pulse associated with that command falls inside both
   settled voltage plateaus. Measure the preceding and following laser edges,
   not just the nearest one. The entered delay is AO-clock-to-PFI12 delay;
   function-generator, laser, DAC and cable delays must be observed on scope.
5. Use persistence to look for phase changes or extra edges. Stop, edit delay,
   rate or voltages, then Run again. Test both step directions and the largest
   voltage changes you will use. Set a longer hold to inspect plateaus.
   Requested 750000 Hz realizes about 751880 Hz; the tested delay was 900 ns.

Duration **0** runs until Stop. Scope-only never opens PicoHarp and saves **no
TTTR and no full-duration AO array**. It saves one compact command cycle,
an ideal command preview and metadata, regardless of observation duration.
The preview is commanded timing, not measured settling. Save scope captures
beside the run and enter their identifiers in the notes.

Stop halts the counter, releases tasks, returns the marker low and both AO
channels to zero. It is not a laser power-off command; the BDL may revert to
its internal clock when external triggering stops.

## Second: short recordings with your detectors connected

Select **Record short TCSPC test**, wiring **Normal detectors (marker spacing
only)**. Keep the current detector and SYNC connections. Review the PicoHarp
tab. Defaults: **1 second per start**, **3 separate NI starts**, **1 second
warm-up before each recording**, and **16 MiB total raw-data cap**.

Only the capture interval writes TTTR. NI runs while each timed capture
finishes and drains. The cap covers all starts combined. Reaching it, Stop,
device faults or failed cleanup preserve partial files and prevent a reference
being accepted. For prolonged scope observation, use scope-only instead.
At 500 kHz with marker spacing 32, marker words alone contribute about
0.18 MiB over the default three seconds of recording.

Analysis then opens a figure showing markers per start, reference status,
and detector arrival histograms. Every adjacent marker interval must equal
the planned number of SYNC cycles. Missing/extra markers or nonconstant
spacing fail the check. **Normal detector data does not determine the absolute
integer offset between a marker tag and its trial's laser SYNC.**
Use **Analyze latest recording →**, or select a saved folder, to analyze later.

## Third: calibrate the absolute integer offset

This optional commissioning stage needs an electronic reference. Temporarily
disconnect the detector from **PicoHarp CH1** and provide a suitably conditioned
copy of the **same marker rising edge** to that input; retain the original
signal on rear Marker 1 and laser TRG OUT on SYNC. CH2 is disabled in this mode.
**Do not directly connect NI TTL to the 50-ohm photon input.** Check loaded
amplitude, edge and conditioning against the
[PicoHarp input specifications](https://www.picoquant.com/images/uploads/downloads/datasheet_picoharp330.pdf).

On the scope, measure from the conditioned CH1-input rising edge to the laser
SYNC belonging to the **same AO command**. The copy must lie after the preceding
SYNC and before that command's following SYNC, with margin from both boundaries.
Account for cable/conditioning delays. TTL OUT may be used only with its
verified relationship to TRG OUT accounted for. Save the scope capture proving
this relationship; the GUI's nominal delay is not a substitute for a measurement.

Select wiring **Conditioned marker copy on CH1**, enter the measured lead in ns,
allowed discrepancy (default 100 ns), and scope/cable evidence. Use zero SYNC
and CH1 offsets and CH1 rising-edge triggering. Enter a descriptive
**Function generator settings / cable configuration ID** and reuse that exact
description in BB84. Confirm the conditioned input has been checked.

Analysis requires one electronic edge per interior marker, no extra CH1 events
between them, the expected fine-time lead and exact marker spacing.
First/last markers are capture-boundary guards. For an edge at photon-input
SYNC index p and marker tag m:

    offset = (p + 1) - m

The +1 follows the scope-established fact that the reference edge precedes the
laser SYNC for the same AO command. It is not guessed from optical fidelity.
Offset must be constant within and across starts. At least **three clean
independent starts** are required. Once passed, select **Save verified reference
for BB84**, choose this completed test folder and click Run. This rechecks the
recording and explicitly publishes its reference.

Restore the detector and its saved settings afterward. In both BB84 tools,
**Use timing marker: Yes** loads a matching reference; otherwise it records
markers with an explicitly unverified absolute offset. **No** disables markers.
Matching includes NI device/serial, PicoHarp serial/SYNC settings, rate,
trigger delay/width, binning and recorded cable configuration ID.
Changing physical cabling requires updating that ID and repeating calibration;
software cannot detect an unrecorded wiring change. Calibrate 500 kHz and
750 kHz separately.

The scope tool regenerates a compact pattern. BB84 uses finite streaming and
sparse start/checkpoint/end markers. After calibration, run short Ordered BB84,
then a longer acquisition crossing USB refills. Each BB84 analysis still
requires the full expected schedule and exact spacing; the scope test does
not waive those checks. Compensating errors between checkpoints are not
excluded by net spacing alone.

## Files and implementation

- Scope-only: `data/timing/<timestamp>_scope/` with `metadata.json`,
  `command_cycle.csv` and `command_preview.png`.
- TCSPC: `data/timing/<timestamp>_marker_test/session.json`; a subfolder
  per start with the same small NI records plus `tttr/events.t3raw`
  and `tttr/metadata.json`.
- Analysis: `analysis/<timestamp>/analysis.json`, `marker_test.png` and PDF
  inside that test folder. Raw files remain unchanged.
- Published references: `data/timing/marker_references/<profile hash>.json`,
  including report path/hash, scope evidence and measured offset.

AO0/AO1 use the NI 100 MHz timebase. Ctr0 uses the same integer divisor and
starts on the first AO SampleClock. P0.0 uses AO SampleClock directly. Counter
and DO arm before AO starts. Scope mode regenerates at most 1024 onboard
samples; Python controls overall duration, never individual edges.
DAQmx verifies/commits routes and checks readback before starting.
PFI0/PFI1 are untouched. Unsupported routes fail without software-timing fallback.

References: [NI X-Series manual](https://docs-be.ni.com/bundle/pcie-pxie-usb-63xx-features/raw/resource/enus/370784k.pdf),
[USB-6351 specifications](https://www.ni.com/docs/en-US/bundle/pcie-usb-6351-specs/page/specs.html),
[PicoHarp 330 manual](https://downloads.picoquant.com/manuals/PicoHarp330_Manual.pdf).
