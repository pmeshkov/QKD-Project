import nidaqmx
from nidaqmx.constants import Edge, CountDirection
import nidaqmx.system
import matplotlib.pyplot as plt
import collections
import time
import threading
import csv
import datetime
import os
import numpy as np

# ==========================================
# EXPERIMENTAL SEQUENCE PARAMETERS
# ==========================================
# timearray (seconds) defines how long each step lasts.
# signalarray (volts) defines the target voltage for the EOM during that step.
#timearray =    np.ones(120)*1
#signalarray1 = np.append(np.linspace(-200,200,60), np.zeros(60))
#signalarray0 = np.append(np.zeros(60), np.linspace(-200,200,60))

v_sweep = np.linspace(-200, 200, 60)

# This is the GUI window size. Note that it defaults to
# 100 counts per window. Decide this based on the length of each
# measurement in a natural way. So for example, for time_unit = 0.1,
# 100 counts per window corresponds to window scrolling completely
# through once roughly every two seconds, which is reasonable.
window_size = None

time_unit = 0.1

# Total sequence time: 60 * 60 = 3600 units
timearray = np.ones(3600) * time_unit

# Sweeps from -200 to 200 repeatedly
signalarray0 = np.tile(v_sweep, 60)

# Steps from -200 to 200 slowly
signalarray1 = np.repeat(v_sweep, 60)

# Hardware Parameters
device_name = "Dev1"
ao_channel_0 = f"{device_name}/ao0"
ao_channel_1 = f"{device_name}/ao1"       
pfi_pin_0 = f"/{device_name}/PFI1"
pfi_pin_1 = f"/{device_name}/PFI2"
HV_GAIN = 20.0

# Ensure arrays are identically sized
assert len(timearray) == len(signalarray0) == len(signalarray1), "Input arrays must be the same length."
total_sequence_time = sum(timearray)

# ==========================================
# SHARED STATE & THREADING SETUP
# ==========================================
# Thread-safe data structures for passing data between DAQ and GUI
shared_state = {
    'is_running': True,
    'elapsed_time': 0.0,
    'current_step_duration': timearray[0],
    'current_v0': signalarray0[0],
    'current_v1': signalarray1[0],
    'rate0': 0.0,
    'rate1': 0.0
}

# Data structure to hold all points for the CSV export
# Format: list of tuples (Timestamp, Elapsed_Time, V0, V1, Raw_Count0, Raw_Count1, Rate0, Rate1)
recorded_data = []

# ==========================================
# DAQ WORKER THREAD (Strict hardware loop)
# ==========================================
def daq_worker():
    nidaqmx.system.Device(device_name).reset_device()
    print(f"Hardware reset complete. Initializing DAQ tasks...")
    
    try:
        with nidaqmx.Task() as ao_task, nidaqmx.Task() as ci_task0, nidaqmx.Task() as ci_task1:
            
            # Setup AO
            ao_task.ao_channels.add_ao_voltage_chan(ao_channel_0)
            ao_task.ao_channels.add_ao_voltage_chan(ao_channel_1)
            
            # Setup CI
            ci_chan0 = ci_task0.ci_channels.add_ci_count_edges_chan(
                f"{device_name}/ctr0", edge=Edge.RISING, initial_count=0, count_direction=CountDirection.COUNT_UP)
            ci_chan0.ci_count_edges_term = pfi_pin_0
            
            ci_chan1 = ci_task1.ci_channels.add_ci_count_edges_chan(
                f"{device_name}/ctr1", edge=Edge.RISING, initial_count=0, count_direction=CountDirection.COUNT_UP)
            ci_chan1.ci_count_edges_term = pfi_pin_1

            # Start Hardware
            ci_task0.start()
            ci_task1.start()
            ao_task.start()
            
            start_time = time.perf_counter()
            
            for step_idx in range(len(timearray)):
                if not shared_state['is_running']:
                    break
                
                # Setup current step parameters
                step_dur = timearray[step_idx]
                target_v0 = signalarray0[step_idx]
                target_v1 = signalarray1[step_idx]
                
                # Peter Note 9/2/26
                # The amplifier seemingly inverts the voltage. We are inverting to reverse this.
                # This inversion is erroneously reflected in the GUI automatically. Thus, later
                # in the GUI section we must invert before showing the voltage at the GUI.

                # Clamp and calculate DAQ voltage
                daq_v0 = -max(-10.0, min(10.0, target_v0 / HV_GAIN))
                daq_v1 = -max(-10.0, min(10.0, target_v1 / HV_GAIN))
                
                # Apply voltages to hardware immediately
                ao_task.write([daq_v0, daq_v1])

                # Update shared state for GUI
                shared_state['current_v0'] = daq_v0 * HV_GAIN
                shared_state['current_v1'] = daq_v1 * HV_GAIN
                shared_state['current_step_duration'] = step_dur
                
                # Grab initial counts at the start of the step
                c0_start = ci_task0.read()
                c1_start = ci_task1.read()
                step_start_time = time.perf_counter()
                
                # Wait for the duration of the interval (checking for early exit)
                while (time.perf_counter() - step_start_time) < step_dur:
                    if not shared_state['is_running']:
                        break
                    time.sleep(0.01) # Keeps thread responsive to stop commands
                
                if not shared_state['is_running']:
                    break
                
                # Grab final counts at the end of the step
                c0_end = ci_task0.read()
                c1_end = ci_task1.read()
                step_end_time = time.perf_counter()
                
                # Calculate average rate over the entire interval step
                dt = step_end_time - step_start_time
                hz0 = (c0_end - c0_start) / dt if dt > 0 else 0.0
                hz1 = (c1_end - c1_start) / dt if dt > 0 else 0.0
                
                shared_state['rate0'] = hz0
                shared_state['rate1'] = hz1
                
                now = time.perf_counter()
                elapsed = now - start_time
                shared_state['elapsed_time'] = elapsed
                
                # Save exact timestamp and single averaged data point to master record
                unix_timestamp = time.time()
                recorded_data.append((unix_timestamp, elapsed, target_v0, target_v1, c0_end, c1_end, hz0, hz1))
                    
    except Exception as e:
        print(f"DAQ Error: {e}")
    finally:
        shared_state['is_running'] = False
        print("Sequence complete. DAQ tasks closed.")

# Start DAQ thread
daq_thread = threading.Thread(target=daq_worker, daemon=True)
daq_thread.start()

# ==========================================
# GUI / MAIN THREAD (Monitoring Only)
# ==========================================
plt.ion()
fig, ax = plt.subplots(figsize=(10, 6))
plt.subplots_adjust(top=0.8) # Leave room for clean status text at the top

# Set the window to the last 100 samples
if not window_size:
    window_size = 100 
plot_rate0 = collections.deque(maxlen=window_size)
plot_rate1 = collections.deque(maxlen=window_size)
plot_x = collections.deque(maxlen=window_size) # Track X coordinates for the rolling window

# Initialize empty lines
line0, = ax.plot([], [], label='PFI1 Rate (Hz)')
line1, = ax.plot([], [], label='PFI2 Rate (Hz)')
ax.legend(loc='upper left')
ax.set_xlabel(f"Samples (Rolling window of {window_size})")
ax.set_ylabel("Counts Per Second (Hz)")

# Status Text Elements
time_text = fig.text(0.5, 0.92, '', ha='center', fontsize=14, fontweight='bold')
v0_text = fig.text(0.25, 0.85, '', ha='center', fontsize=12, color='blue')
v1_text = fig.text(0.75, 0.85, '', ha='center', fontsize=12, color='orange')

last_plotted_elapsed = -1
sample_count = 0 # Track how many samples we've plotted for the X-axis

try:
    while shared_state['is_running'] and plt.fignum_exists(fig.number):
        elap = shared_state['elapsed_time']
        
        # Only update plot if a new step has finished and updated the state
        if elap > last_plotted_elapsed:
            v0 = shared_state['current_v0']
            v1 = shared_state['current_v1']
            step_dur = shared_state['current_step_duration']
            r0 = shared_state['rate0']
            r1 = shared_state['rate1']
            
            # Update text
            time_text.set_text(f"Elapsed Time: {elap:.2f} s / {total_sequence_time:.2f} s")
            v0_text.set_text(f"EOM0 Current: {-v0:.1f} V\n(Hold duration: {step_dur:.1f} s)")
            v1_text.set_text(f"EOM1 Current: {-v1:.1f} V\n(Hold duration: {step_dur:.1f} s)")
            
            # Update deques
            plot_rate0.append(r0)
            plot_rate1.append(r1)
            plot_x.append(sample_count)
            
            # Update BOTH X and Y data dynamically
            line0.set_data(plot_x, plot_rate0)
            line1.set_data(plot_x, plot_rate1)
            
            # Dynamically scale both X and Y axes
            ax.set_xlim(max(0, sample_count - window_size), max(window_size, sample_count))
            
            max_rate = max(max(plot_rate0), max(plot_rate1)) if len(plot_rate0) > 0 else 10
            ax.set_ylim(0, max(10, max_rate * 1.1))
            
            fig.canvas.flush_events()
            
            last_plotted_elapsed = elap
            sample_count += 1
            
        plt.pause(0.1) # Check state at 10Hz
        
except KeyboardInterrupt:
    print("Measurement interrupted by user.")
    shared_state['is_running'] = False

# Ensure thread closes gracefully if window was closed early
shared_state['is_running'] = False 
daq_thread.join(timeout=2.0)

plt.ioff()
plt.close(fig)

# ==========================================
# EXPORT DATA TO CSV
# ==========================================
if recorded_data:
    # Prompt the user for input in the terminal
    user_input = input("Sequence finished. Do you want to save the recorded data to CSV? (y/n): ").strip().lower()
    
    if user_input in ['y', 'yes']:
        run_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        trace_file = f"{run_id}_Detector_Traces.csv"
        profile_file = f"{run_id}_EOM_Profile.csv"
        
        # Save 1: Detector Traces
        with open(trace_file, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Unix_Timestamp", "Elapsed_Time_s", "EOM0_Target_V", "EOM1_Target_V", "Raw_Count0", "Raw_Count1", "Rate0_Hz", "Rate1_Hz"])
            writer.writerows(recorded_data)
            
        # Save 2: Input Profiles
        with open(profile_file, mode='w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(["Time_Duration_s", "EOM0_Voltage", "EOM1_Voltage"])
            for t, v0, v1 in zip(timearray, signalarray0, signalarray1):
                writer.writerow([t, v0, v1])

        print(f"Data successfully saved to:\n - {os.path.abspath(trace_file)}\n - {os.path.abspath(profile_file)}")
    else:
        print("Data discarded without saving.")
else:
    print("No data was recorded.")