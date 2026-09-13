# Pulsed BB84 bring-up

Current agreement recorded 2026-09-13. This directory starts with the standalone
laser-clock milestone. Hardware output has not been tested by Codex.

**Current bench plan:** DAQ-to-BDL triggering is deferred until the electrical
interface is resolved. Use the laser's internal 2 MHz setting for HBT/g2 work;
do not run `laser_clock.py --run` for this configuration. See
[PicoHarp bring-up](PH330.md). A first physical 10-second T3 recording and
diagnostic preview succeeded; full optical timing/discriminator calibration
remains to be done.

## Physical allocation

| Resource | Connection / purpose |
| --- | --- |
| NI Ctr0 / PFI12 | BDL SYNC IN, subject to electrical-interface verification |
| BDL TRG OUT | PicoHarp 330 SYNC |
| AO0 | HVA200 / EOM 1 |
| AO1 | HVA200 / EOM 2 |
| PFI1 / PFI2 | Existing detector inputs; confirmed physical wiring, preserve these pins |
| BDL TTL OUT | Unused, optional scope debugging |
| Two SPCM-AQRH-15-FC outputs | PicoHarp detector inputs |

The B&H email reported by the experimenter is the authority for this particular
BDL unit: SYNC IN accepts positive TTL/CMOS pulses, DC coupled from a 50-ohm
source, duty <=30%, and automatically selects external timing. B&H tested
some units at 1 MHz; 500 kHz remains an experimental question.

## First bench test

Run from the repository root in the existing `niDaqENV` environment (installed
Python package inspected: nidaqmx 1.4.1). These are commands for the operator;
the default command only prints a plan.

This project uses a Conda environment, so a local `.venv` is not required.
In VS Code, use **Python: Select Interpreter** and select `niDaqENV`, then open
a new terminal. If `conda` is available in that terminal, `conda activate niDaqENV`
also selects the environment. An existing interpreter selection may override
the workspace default. To bypass activation in PowerShell, use:

```powershell
& "C:\Users\nanometa\miniconda3\envs\niDaqENV\python.exe" pulsed/laser_clock.py
```

```powershell
python pulsed/laser_clock.py
python pulsed/laser_clock.py --run --seconds 60 --note "PFI12 to high impedance scope; BDL disconnected"
```

1. Close the old DAQ scripts and any NI MAX test panels using these resources.
   Several old scripts reset the entire device. Do not run them alongside this
   test. This script does not reset the device, use AO, or configure detector pins.
2. With the BDL disconnected, probe PFI12 relative to D GND using a suitable
   high-impedance probe and short ground connection. Inspect voltage levels,
   ringing, pulse widths and period. Defaults: 1 MHz, high 200 ns, low 800 ns,
   positive pulses, idle low. Confirm the actual duty is <=30%.
3. Resolve the electrical interface before connecting SYNC IN. NI specifies
   a maximum 16 mA drive for PFI. A 50-ohm load would require 100 mA at 5 V.
   Do not terminate raw PFI12 into a 50-ohm scope input. B&H's **50-ohm source**
   requirement does not establish the laser's input impedance. Confirm that
   impedance and the required source interface; a suitable line driver may be
   needed. A high-impedance scope trace alone does not prove compatibility.
4. Stop output before changing wiring. Connect the verified trigger interface
   to BDL SYNC IN. Restart at 1 MHz and observe PFI12, TRG OUT and an optical
   diagnostic. Check their repetition rates, timing relationship and stability.
   TRG OUT alone does not establish clean optical excitation or a good IRF.
5. Stop and restart at each lower rate, for example 800 kHz, 625 kHz, 500 kHz:

```powershell
python pulsed/laser_clock.py --frequency-hz 500000
python pulsed/laser_clock.py --frequency-hz 500000 --run --seconds 60 --note "BDL connected via verified interface"
```

At 500 kHz the default high time stays 200 ns (10% duty), with 1800 ns low.
The script rounds to 10 ns timebase ticks and prints the realized nominal rate;
some requested rates are not exactly representable. The proposed 200 ns width
is a starting test value, not a verified BDL minimum pulse width.

Save scope traces and record measured frequency, high/low voltages, pulse width,
termination/probe details, TRG OUT behavior, optical intensity/IRF, drift, and
pass/fail with each run's JSON. Repeat important rates independently.
The JSON contains configuration, DAQmx tick readback, device identity and host
timestamps, **not measured electrical or optical validation**.

Ctrl+C stops/closes this task. Run duration and stop are software controlled:
neither exact pulse count nor final-pulse completeness is guaranteed. Confirm
the terminal after stopping. Per the reported BDL automatic clock selection,
removing external triggers must not be treated as turning off optical emission.

## Repository inspection

- `niDAQtest.py` and `niDAQtest_justone.py`: hardware-clocked buffered AO sine
  generation already works as a starting pattern. They reset Dev1 and repeat
  a waveform; this does not establish unique Alice trials or synchronization.
- `Organized Scripts 9_2_26/EOM_V_sequence.py` and `_AVE.py`: EOM writes and
  dwell times are software controlled. Their worker-thread comments do not
  make the voltage updates hardware timed. Useful for CW sweeps, not pulsed BB84.
- Those scripts and the GUI/sweep scripts reserve Ctr0/Ctr1 for detector
  counting and use PFI1/PFI2, matching the subsequently confirmed physical wiring.
  Ctr0 cannot also generate the laser clock. A later detector-task migration
  must explicitly allocate spare counters and verify input routes, retaining
  the agreed detector pins. Existing scripts are left intact.
- Calibration sign conventions differ: the sequence uses negative HV/20;
  some sweeps use positive HV/20. Preserve calibrated **DAQ command volts**,
  channel identity, sign and calibration provenance in the eventual state map;
  do not silently clamp invalid calibration values.
- `9_11_26_EOM_Calib/calibrateBB84Protocol.ipynb` labels EOM0 as Alice and
  EOM1 as Bob and constructs a matrix from fitted/sampled sweeps. This differs
  from the current agreement that both EOMs prepare Alice's state. Do not reuse
  its voltage arrays as an H/V/D/A mapping without resolving those labels.
- No PicoHarp acquisition implementation was found in the inspected Python
  scripts/notebook. No final explicit four-state, two-command-voltage mapping
  was identified. Post-processing remains the other collaborator's scope.

## Subsequent milestones and acceptance criteria

1. Accept laser triggering at a measured usable rate before AO integration.
2. Precompute two-channel buffered AO. NI specifies 2.00 MS/s with both AO
   channels and 2 us full-scale settling to 1 LSB. Therefore a 2 us trial is
   not automatically ample settling time. Measure the actual calibrated
   transitions through the HVA200 monitor, including worst-case transitions.
3. Select a shared hardware clock/start arrangement after verifying routes in
   the installed USB-6351 NI MAX Device Routes view and committing the actual
   DAQmx tasks. Explicitly budget counters, initial delay and first-sample
   behavior. A common software start or two equal nominal rates is insufficient.
   Trigger excitation after the measured AO/HV settling interval, with margin.
   This clock-only script does not yet implement that synchronization.
4. For unique random trials, disable AO regeneration and prefill/stream the
   saved sequence without underruns. Define coordinated stop behavior so loss
   of AO data cannot leave the laser firing indefinitely on a stale state.
   Hardware FIFO is not storage for a complete 30-million-trial run. Verify
   USB throughput and abort behavior on the actual host before long runs.
5. Integrate the PicoHarp **330-specific** supported Python/DLL interface and
   event-resolved TTTR format. Verify mode, SYNC divider, input levels/polarity,
   record format, overflow handling and FIFO-loss flags against its installed
   SDK. Do not substitute a PicoHarp 300 record decoder without verification.
6. Save raw TTTR records and complete acquisition settings. Establish a hardware
   observable origin tying Alice index zero to the first accepted excitation
   cycle, with explicit start/end checks. Host timestamps or first detected
   photon are not an index origin. Handle sync division, overflow and acquisition
   boundaries. Missing excitation/sync or dropped records must invalidate or
   explicitly qualify mapping rather than silently shifting Alice indices.
7. Save Alice's complete sequence before acquisition: pulse_index, bit, basis,
   H/V/D/A state, AO0/AO1 command voltages, convention and calibration identity.
   Preserve unsuccessful trials; detections are optional. Save Bob basis
   metadata too once its physical selection scheme is established. Debug
   markers can help prove alignment; analog readback per trial is not required.
8. Verify pulsed 4x4 matrix, then randomized BB84. Acquisition should deliver
   unambiguous Alice/Bob data to the existing analysis/reconciliation software.

Dataset targets: approximately 60 s / 30 million trials at 500 kHz; attenuation
series adjusted to count rates (initial candidates 0, 3, 6, 10, 15, 20 dB);
3-5 repeats at important points; 30-60 minute stability acquisition. Keep raw
timestamps so collaborators can optimize temporal gates and report detection,
sifting, QBER and final secure-key rates, with relevant source g2(0). Running
at 1 MHz with repeated states or post-selecting photons is not, by itself, a
security justification; any fallback protocol requires separate treatment.

## References and offline checks

- [NI 6351 specifications](https://download.ni.com/support/manuals/374591d.pdf),
  AO pp. 6-7, PFI p. 10, counters pp. 12-13.
- [NI's Ctr0/PFI12 pulse example](https://github.com/ni/nidaqmx-python/blob/master/examples/counter_out/write_single_dig_pulse.py).
- [NI Python counter channel API](https://nidaqmx-python.readthedocs.io/en/stable/task_collections.html).

```powershell
python -m unittest discover -s pulsed -p "test_*.py" -v
```

Offline tests validate pulse planning and the no-output default. Only a physical
bench test can validate the installed driver routes, electrical waveform, BDL
response or settling. No hardware task was started during repository inspection.
