# BB84 timing markers and pre-acquisition checks

Implemented for Ordered BB84 and Randomized BB84. Software tests use synthetic
events and simulated hardware. The new DO route and marker convention still need
testing on your actual USB-6351 / function generator / BDL / PicoHarp chain.

## What was corrected in the October 7 audit

The September 29 randomized run `20260929T235636_841003Z_random` saved the old
voltage defaults AND CH1 +200 mV **falling** edge. CH2 was rising. These differ
from the selected polarization calibration and your confirmed detector wiring.
Old files have not been altered. In particular, do not relabel that recording
as having used the newer voltages, or assume its old arrival gate is optimal.

The current profiles now use CH1/CH2 +200 mV rising, SYNC -60 mV falling,
CH2 +4.5 ns, and the calibration already selected in the polarization tool:

| Provisional state/basis | EOM target V |
| --- | ---: |
| Alice H (S0) | -159.18367346938777 |
| Alice R (S1) | -69.38775510204081 |
| Alice V (S2) | 36.734693877551024 |
| Alice L (S3) | 151.0204081632653 |
| Bob HV (setting 1) | 44.89795918367346 |
| Bob RL (setting 2) | -53.061224489795904 |

Source: `data/eoms/20260929_192128_263454/analysis/20260929T195639_080569Z/`.
Its held-out and independent checks are recorded as false. Consistent use does
not establish that these are optimal voltages or confirmed physical labels.
The PBS alone maps transmitted H to CH1, but Bob's EOM can swap the outcomes.
Analysis CH1 assumptions remain V/L under this calibration convention; verify
these with the optical test, rather than forcing H/R simply from the bare PBS.

An explicit calibration import now fills polarization and BOTH BB84 profiles.
Manual edits remain local. BB84 Record requires the six fields to match the
import, unless you explicitly select **Manual voltages**. It saves the source,
check results, and actual values. A one-time profile migration repairs known
legacy defaults and positive-200-mV falling-edge settings, retaining other
custom values. Settings imported thereafter are never marked bench-validated.

## Wiring (breakout now available)

- NI **P0.0 / port0/line0** -> PicoHarp rear CTRL **pin 1 / Marker 1**.
- NI **DGND** -> CTRL **pin 14 / ground**.
- Keep PFI12 -> function generator -> BDL SYNC IN, and BDL TRG OUT -> SYNC.
- Use the numbered terminals on the male DB25 breakout; verify its orientation.
  Pin 24 is a power output, not a marker input. P0.0 must be otherwise unused.

The PH330 manual specifies 3.3-V TTL-compatible marker inputs, maximum 5 V,
high/low durations over 50 ns and rise/fall times under 50 ns. Check the waveform
at the breakout with a high-impedance scope before connection. Do not assume a
50-ohm termination belongs on this digital input.
See [PicoHarp manual, CTRL connector](https://downloads.picoquant.com/manuals/PicoHarp330_Manual.pdf).

Port 0 supports buffered digital output; the X-Series manual lists AO Sample
Clock as a DO clock source. USB-6351 streaming DO is specified up to 1 MHz,
dependent on system/bus activity. This implementation reserves only P0.0,
plus the existing AO engine and Ctr0; it does not reassign PFI0/PFI1 or ctr1/ctr2.
DAQmx verifies/commits the route and checks readback before starting output.
See [NI specifications](https://www.ni.com/docs/en-US/bundle/pcie-usb-6351-specs/page/specs.html)
and [X-Series manual, Digital I/O](https://docs-be.ni.com/bundle/pcie-pxie-usb-63xx-features/raw/resource/enus/370784k.pdf).

## What the software generates

The **Timing markers** tab now has **Use timing marker: Yes/No**, default No.
Yes loads a matching reference published by **EOM / timing marker test**.
Without one, it records markers but keeps absolute trial origin unverified.
With markers enabled, a finite, nonregenerating DO waveform uses the SAME
AO sample clock. DO and the laser counter are armed before AO starts. The
prefilled buffers and subsequent chunks contain both AO commands and markers;
Python does not time individual marker edges.

Let W be the warm-up sample count and N the experimental trial count. Marker 1
rises at sample W-4, W (trial zero), then approximately every 0.1 s, and at
W+N-1. Short runs also get an interior reference. Each high lasts one sample,
except the final high, which holds until cleanup returns the line low. The
final trial is an end-reference guard, excluded from marker-indexed analysis.
At 500 kHz that costs one trial, or 2 microseconds, per acquisition.

`marker_schedule.json`, session plan, DO readback, generated sample count,
cleanup status, and marker-enabled T3 metadata all stay in the run folder.
T3 records retain all photon/marker/overflow words, with no acquisition gate.

## First commissioning run

1. Use **EOM / timing marker test → Scope only (no TCSPC recording)** at
   **500 kHz / 1200 ns**. Trigger on the marker rising edge; it accompanies an
   A→B voltage command. Confirm both monitors settle before the associated
   laser pulse. TTL OUT is usable with its verified TRG OUT relationship.
2. Select **Record short TCSPC test**, **Normal detectors** wiring: defaults are
   3 independent starts, 1 s captured per start, 1 s warm-up and a 16 MiB total
   raw cap. Analysis tests exact marker spacing, not the absolute offset.
3. For that offset, use the scope-backed **Conditioned marker copy on CH1**
   procedure in the [timing tool guide](../pulsed_GUI/EOM_TIMING_SCOPE.md).
   Three clean starts with identical offset permit **Save verified reference
   for BB84**. Keep the scope evidence and configuration ID with the test.
4. Restore the detector. Use **Ordered BB84**, 1 s warm-up, 2 s experiment and
   **Use timing marker: Yes**. Match the timing, SYNC settings and configuration
   ID to the saved reference. The GUI reports whether a reference matches.
   Record and Analyze must still pass every expected start/interior/end marker.
5. Test at least 10 s to cross USB refills, then use the same procedure for
   Randomized BB84. Changing timing or cabling requires the corresponding
   calibration. Earlier unverified recordings can use a later matching saved
   reference during analysis, without rewriting their acquisition metadata.

## Precisely what needs to be calibrated

Define delta = (T3 SYNC index of the laser pulse belonging to sample k)
               - (T3 SYNC index of the marker rising at sample k).

For any marker at NI sample k with recorded count m:

    first experimental SYNC = m - k + W + delta
    trial index             = photon SYNC - first experimental SYNC

The marker edge coincides with the voltage command and precedes the delayed
laser trigger. Do not assume delta=0 or +1 from that fact alone. PH330 T3 marker
accuracy is limited by SYNC quantization (specified error < Tsync + 100 ns),
and the actual cable/function-generator delays and capture convention matter.
The scope establishes phase and margin; it alone does not expose the T3 integer
tag assigned inside the PicoHarp.

For an independent electronic check, temporarily replace one detector input
with a suitably conditioned copy of the marker signal while retaining laser
SYNC. A fine-time photon-input tag of that electronic edge, compared with its
Marker 1 record and scope timing to the following SYNC, establishes the integer
convention. Verify levels/termination against the detector-input specification
before this loopback; do not connect an unconditioned digital output to CH1/CH2.
Raw T3 is retained and `ph330_preview.decode_t3_events` exposes both event types.
Use the dedicated timing tool for this short loopback; restore the detector
and reconfirm its arrival distribution afterwards. Never choose delta solely
because one ordered phase gives the lowest optical error.

The software demands exact marker-to-marker SYNC counts. Passing them detects
NET missing/extra SYNCs across each block, not compensating faults inside one
block. Confirm one laser pulse per trigger and stable waveforms as well.
No timing result automatically certifies a secure key. Acquisition files keep
their original unverified status; analysis records the bench-reference result
separately without rewriting raw data or the original session.

## Before gathering the next scientific dataset

- Recheck both arrival curves after the CH1 edge correction; CH2 +4.5 ns is
  applied ONCE by the SDK. Choose the offline gate from the corrected data.
- Confirm static eight-setting performance at the chosen six voltages, then
  compare Ordered and Randomized BB84 at the same rate, gate and source.
  Random order includes transitions that the fixed four-state cycle may miss.
- Scope both HVA monitors for the largest actual voltage transitions. The
  earlier ~300 V step settled in slightly under 1 us; start at the tested
  500 kHz / 1200 ns setting before reassessing 750 kHz / 900 ns.
- Save sample type, filter, power with location/units, and function-generator
  settings. A bright spot run is not source antibunching validation.
- SDK elapsed time was zero in earlier software-stop runs. Use stated NI trial
  count/rate for planned exposure, not that zero as an exposure measurement.
- Run longer only after clean completion, correct marker spacing, stable offset
  and acceptable optical results. Repeatability and downstream source/security
  assumptions remain separate from this acquisition-code audit.
