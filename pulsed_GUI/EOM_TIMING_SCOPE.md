# EOM timing scope test

Open `python pulsed_GUI/eom_timing_scope.py` in the existing niDaqENV environment,
or choose **EOM timing scope** in `python pulsed_GUI/launch.py`.
Opening the window and **Check settings** do not access hardware.

## Connect and run

1. Close other scripts that own AO0/AO1 or Ctr0 (including the alignment GUI and
   the separate laser-clock GUI). This tool uses **USB-6351 AO0/AO1 and Ctr0/PFI12**.
   It neither resets the device nor uses detector counters/PFI0/PFI1/PFI2.
2. Keep the existing PFI12-to-BDL interface and EOM loads connected. Scope CH1:
   HVA200 **HV MONITOR**, DC coupled, high impedance (normally 1 Mohm), no external
   50-ohm termination. Nominal monitor voltage is HV/20. Use the monitor connector,
   not the HV drive output. Keep the amplifier bias as used in calibration.
3. Scope CH2: BDL **TTL OUT**, using a suitable probe/input voltage range. A
   high-impedance probe is a useful starting point; record the load and cabling.
   TRG OUT remains the PicoHarp reference. TTL OUT is useful for this scope test,
   but its delay relative to TRG OUT and the optical pulse has not been measured.
4. Enter two **calibrated EOM voltages** for Alice, levels A and B. These are HV
   targets, not NI voltages: the existing convention is NI voltage = -target/20.
   Set Bob A = Bob B at a suitable fixed voltage to test Alice alone. Reverse
   roles to test Bob, then give both channels different A/B values to test joint
   switching. Defaults are all zero; output requires at least one nonzero step.
5. Begin with 1,000,000 Hz, nominal delay 500 ns, trigger width 100 ns, and
   **20 pulses per voltage level**. At 1 MHz this produces a 20 us plateau.
   Duration 0 runs until Stop. Use Check settings first; inspect realized values.
6. Select **Output timing pattern**, then Run. Trigger the scope on a rising or
   falling **monitor step**, with its threshold between the two plateaus. A capture
   covering at least two transitions shows both directions. Triggering solely on
   TTL OUT can overlay different voltage states and make the trace confusing.
7. Measure the final levels, overshoot and time needed to enter and stay near the
   target. If needed, add the AO command as a third scope channel to locate the
   command transition directly. The monitor plus TTL traces alone measure when
   laser timing falls relative to the actual voltage response.
8. Stop, change **pulses per voltage level to 1**, and Run again. At 1 MHz there
   is now one voltage level per 1 us excitation cycle. The A/B pattern itself
   repeats at 500 kHz. Adjust delay between runs (for example 50 ns increments)
   to move TTL OUT across the monitor waveform. Confirm motion on the scope.
9. If the useful settled interval is too short, try 750,000 or 500,000 Hz. Rates
   and delays are quantized to 10 ns ticks: a 750,000 Hz request becomes roughly
   751,880 Hz. The displayed/read-back values, not the requested number, apply.

Stop ends NI generation and returns AO0/AO1 to 0 V. It is not a laser-power-off
control. Changing settings requires Stop, edit, Run; this first version does not
modify a live waveform. Abruptly killing Python bypasses cooperative cleanup.

## Timing implementation and limits

The two AO channels share one buffered task, clocked from the 100 MHz timebase.
Ctr0 uses the same timebase and exactly the same integer period divisor. Its
digital start trigger is `/Dev1/ao/SampleClock`; it arms before AO starts and waits
for that first edge. Its initial delay sets the nominal laser-trigger phase.
Later AO edges do not retrigger the continuously running counter.

The complete A/B cycle regenerates from onboard memory, so Python/USB does not
schedule individual steps. A maximum of 2,000 pulses per level keeps the two
channel waveform within the specified shared AO FIFO. The counter pulse is
positive and checked for <=30% duty; delay plus pulse width must leave at least
20 ns before the next AO clock. Check settings reports those limits.

DAQmx verifies/commits both tasks and checks timing readback before starting.
Unsupported routes or reserved resources produce an error and a saved log;
there is no fallback to software timing or reassignment of another pin/counter.
This code has offline tests, but **the exact route and output phase still require
verification on the physical USB-6351**. Driver routing delays, DAC response and
laser TTL-output delay are not subtracted from the nominal delay in advance.

This is a repeating scope diagnostic, not a finite BB84 trial acquisition. It
does not use PicoHarp, locate a T3 sequence origin, or measure settling itself.
The approximate overall duration is host controlled; individual output timing
is hardware controlled. Avoid inferring optical fidelity solely from HV settling.

## Saved files

Each output run creates `data/timing/<UTC timestamp>_scope/`:

- `metadata.json`: requested settings, realized timing, NI readback, notes and
  completion/cleanup status, including an error if hardware setup fails.
- `command_cycle.csv`: one complete repeating cycle, both command voltages and
  nominal AO-clock/NI-trigger times. These are not measured HV waveforms.
- `command_preview.png`: ideal requested commands and trigger timing.

The GUI's **Open data folder** opens this directory. Save scope screenshots/CSV
there manually, naming the observed EOM and noting probe/termination, measured
settling, and TTL-to-TRG offset when later measured. No scope-control driver is
used. No photon or TTTR data is collected by this tool.

## Technical references

- [NI X Series manual](https://docs-be.ni.com/bundle/pcie-pxie-usb-63xx-features/raw/resource/enus/370784k.pdf):
  AO clock/timebase in chapter 5; continuous pulse train and AO SampleClock-to-
  counter Gate/start-trigger routing in chapter 7.
- [USB-6351 specifications](https://www.ni.com/docs/en-US/bundle/pcie-usb-6351-specs/page/specs.html).
- [B&H BDL-SMN manual](https://www.becker-hickl.com/wp-content/uploads/2019/11/opm-bdl-smn-v05.pdf):
  TTL OUT and the preferred TCSPC timing output, Figure 13.
- [HVA200 monitor scaling](https://www.thorlabs.com/catalogpages/V21/981.PDF).
