# Pulsed experiment GUI

These are separate copies of the experiment tools. The original `pulsed/` scripts and data are unchanged. No terminal parameters are needed.

## Open

Double-click **Start Pulsed GUI.cmd** in this directory. It uses the existing `niDaqENV` Python environment on this computer. Alternatively, open `launch.py` in VS Code, select the **niDaqENV** interpreter, and click **Run Python File**.

Choose a tool, edit the fields, choose the **Action**, and click **Run**. Settings are remembered separately for each tool in `gui_settings.json` when you run, switch tools, or close the window. Nothing starts automatically on opening. Close UniHarp before using the PicoHarp tools.

You can also run any of these Python files directly with VS Code's Run button; each opens its own tool in the same GUI:

| Script | Purpose |
| --- | --- |
| `ph330.py` | Check the DLL or find connected PicoHarp devices. |
| `laser_clock.py` | Check timing settings or output the clock on Ctr0/PFI12. |
| `laser_clock_eom.py` | Adjust static AO0/AO1 EOM voltages while generating the PFI12 clock; adjust clock settings in the same window. |
| `ph330_acquire.py` | Check settings, check count rates, or record CH1/CH2 TTTR data for the 2 MHz HBT test. |
| `ph330_preview.py` | Plot a saved acquisition without connecting to hardware. |
| `ph330_lifetime.py` | Record CH1 fluorescence delays, or analyze a saved run and optionally fit its decay tail. |

`gui_app.py` supplies the shared windows. The `test_*.py` files remain developer offline tests, not acquisition applications. The two copied JSON profiles are reference configurations; acquisition settings are entered using the form and saved in each run's metadata. The separate `laser_clock_gemini.py` was not copied.

## Laser clock + EOM

Select **Laser clock + EOM** in the launcher (or run `laser_clock_eom.py` directly). Enter the clock frequency, positive pulse width, and both EOM target voltages. Choose **Output clock + EOM** and click **Run**. A duration of **0** keeps the session running until Stop; a positive duration ends it automatically.

While running, edit the EOM fields and click **Apply EOM voltages**. AO0 drives EOM 1 and AO1 drives EOM 2. The clock continues during manual voltage changes. The fields use target voltages at the EOM, with the same assumed gain/sign as the existing bench code: **DAQ volts = −EOM target / 20**. For example, +100 V requests −5 V from the DAQ. Both target and DAQ values appear in the log when the write succeeds. These are commanded values, not measured high-voltage readbacks. Inputs beyond ±200 V (±10 V DAQ) are rejected instead of silently clipped.

Edit frequency/pulse width and click **Apply clock (restart)** to change the pulse train. The counter stops, is reconfigured, and restarts; EOM voltages are held. This introduces a gap and does not preserve excitation indices. Duration is measured from the original clock start, not reset by adjustments. **Stop acquisition**, window close, expiry, and error cleanup stop/close the counter and attempt to return both AO outputs to 0 V. Cleanup failures are reported.

This is a manual alignment tool. AO uses on-demand static writes; updates are not synchronized to individual laser pulses, and transients may overlap excitation. The initial 10 ms pause before starting the clock is not a calibrated EOM settling measurement. Final BB84 operation still needs buffered, hardware-synchronized AO generation.

Close other programs using AO0/AO1 or Ctr0 before running. In particular, `EOM_scripts/EOM_gui_log.py` also uses Ctr0 and resets the device at startup, so do not run it alongside this tool. The new tool never resets the DAQ or reassigns detector inputs. The existing PFI12-to-BDL electrical interface requirements still apply. Each session saves initial settings, applied adjustments with host timestamps, restart events, and cleanup status in `pulsed_GUI/runs/clock_eom/*_clock_eom.json`.

The on-demand AO write behavior follows [NI's Python task documentation](https://nidaqmx-python.readthedocs.io/en/stable/task.html); the [USB-6351 specification](https://www.ni.com/en/shop/hardware/voltage/model-usb-6351) lists two AO channels with a ±10 V range.

## CH1 lifetime

1. Choose **CH1 lifetime**. Enter the measured **SYNC edge** and signed **SYNC threshold (mV)**. These start blank because the recent wiring discussion included both positive TTL and negative TRG OUT signals. Enter the correct settings for the pulse actually reaching SYNC. Detector CH1 uses positive pulses and a rising edge.
2. Set the expected laser rate (currently **2000000 Hz**), duration, and data directory. Keep **Fit an exponential decay tail = No** initially.
3. Choose **Check rates**, then click **Run**. Read the SYNC and CH1 rates in the log.
4. Choose **Record**, then click **Run**. This records CH1 only and disables CH2. The saved plot opens automatically after a successful acquisition and analysis.
5. To estimate lifetime, choose **Analyze saved run**, browse to the timestamped run folder, select **Fit = Yes**, and enter the start/end of the decay tail in ns. Click **Run**. No additional measurement is made. This preliminary exponential-plus-background fit excludes instrument-response correction; do not fit the prompt peak.

Lifetime plots show the CH1 delay histogram on linear and logarithmic scales; a successful fit adds residuals. The estimated lifetime appears in the plot and log. Raw delays include cable/instrument offsets. A flat trace need not yield a measurable lifetime.

## Data and plots

Paths are absolute and do not depend on the terminal's working directory. By default:

- CH1 lifetime: `pulsed_GUI/runs/lifetime_ch1/<UTC timestamp>/`
- HBT/G2 acquisition: `pulsed_GUI/runs/ph330/<UTC timestamp>/`
- Clock logs: `pulsed_GUI/runs/clock/<UTC timestamp>_clock.json`

Each PicoHarp acquisition saves `events.t3raw` and `metadata.json`; keep them together. Lifetime analysis adds `lifetime_ch1.png`, `.csv`, and `.json`. The HBT preview adds `preview.png`, `preview_summary.json`, and `coincidences_preview.csv`. **Open data folder** and **Open plot** become available when those artifacts are saved. Analysis can also select existing runs in the original `pulsed/runs` directory; it writes derived plots into the selected run folder and can replace earlier derived plots there, leaving raw data unchanged.

The HBT preview's three plots are counts versus time, photon delays after SYNC, and raw CH2−CH1 coincidence counts. These are diagnostics, not a normalized or background-corrected g² result. The preview uses a bounded prefix (default 2 million raw records); lifetime analysis reads the entire file.

## Stopping and hardware behavior

**Stop acquisition** requests a cooperative stop after the current driver call. PicoHarp measurement stops, its device closes, and saved partial data is marked incomplete. Existing analysis refuses incomplete runs. Closing the window during a run requests the same stop and waits for cleanup. Plotting that has already started may finish before the window closes. Avoid forcibly terminating Python during acquisition.

The clock still uses hardware timing. Python only controls its approximate overall duration. PFI0/PFI1/PFI2 are not reassigned. The unresolved DAQ-to-laser impedance interface is still needed before connecting PFI12 to the BDL. The GUI copy corrects the source script's `1_000_000_000` timebase constant to **100 MHz**, matching its existing `100MHzTimebase` route and 10 ns tick calculations. Its inherited allowed frequency range remains 1 kHz–10 MHz, subject to minimum pulse widths and ≤30% duty cycle; this is not a claim that the laser or EOM operates throughout that range.

The PicoHarp still uses the existing PH330Lib T3 setup and validation. Threshold settings do not attenuate incoming pulses; use the verified electrical interfaces and correct 50 Ω signal levels. The GUI does not control the laser repetition rate or the EOMs.

Offline tests (no hardware): select `niDaqENV` and run `python -m unittest discover -s pulsed_GUI -v` from the repository root. Dependencies are the existing environment's tkinter, numpy, scipy, matplotlib/Pillow, and nidaqmx for the clock; the PicoHarp driver/DLL is required only for hardware operations.
