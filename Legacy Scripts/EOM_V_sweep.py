import nidaqmx
from nidaqmx.constants import Edge, CountDirection
import nidaqmx.system
import time
import csv
from datetime import datetime

# Hardware Parameters
device_name = "Dev1"
ao_channel = f"{device_name}/ao1"       
pfi_pin_0 = f"/{device_name}/PFI1"
pfi_pin_1 = f"/{device_name}/PFI2"

# Sweep Parameters
START_V = -200.0
STOP_V = 200.0
STEP_V = 10.0
DWELL_TIME_SEC = 1.0
HV_GAIN = 20.0

# Generate timestamped filename
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
csv_filename = f"Vpi_Sweep_{timestamp}.csv"

# Reset hardware
nidaqmx.system.Device(device_name).reset_device()
print(f"Hardware reset. AO: {ao_channel} | Counters: {pfi_pin_0}, {pfi_pin_1}")

# Configure Tasks
with nidaqmx.Task() as ao_task, nidaqmx.Task() as ci_task0, nidaqmx.Task() as ci_task1:
    
    # Configure Analog Output
    ao_task.ao_channels.add_ao_voltage_chan(ao_channel)
    ao_task.write(0.0)

    # Configure Counter Inputs
    ci_chan0 = ci_task0.ci_channels.add_ci_count_edges_chan(
        f"{device_name}/ctr0", edge=Edge.RISING, initial_count=0, count_direction=CountDirection.COUNT_UP)
    ci_chan0.ci_count_edges_term = pfi_pin_0
    
    ci_chan1 = ci_task1.ci_channels.add_ci_count_edges_chan(
        f"{device_name}/ctr1", edge=Edge.RISING, initial_count=0, count_direction=CountDirection.COUNT_UP)
    ci_chan1.ci_count_edges_term = pfi_pin_1

    # Start Tasks
    ci_task0.start()
    ci_task1.start()
    ao_task.start()
    
    print(f"\nStarting EOM Voltage Sweep: {START_V}V to {STOP_V}V in {STEP_V}V steps.")
    print(f"Integration time per step: {DWELL_TIME_SEC}s")
    print(f"Data will be saved to: {csv_filename}\n")
    print(f"{'Target (V)':>10} | {'DAQ Out (V)':>11} | {'PFI1 (Hz)':>10} | {'PFI2 (Hz)':>10}")
    print("-" * 52)
    
    try:
        # Open CSV in write mode
        with open(csv_filename, mode='w', newline='') as file:
            writer = csv.writer(file)
            writer.writerow(["Target_Voltage_V", "DAQ_Voltage_V", "PFI1_Rate_Hz", "PFI2_Rate_Hz"])
            
            # Generate voltage sequence
            current_target_v = START_V
            firstrun = True
            while current_target_v <= STOP_V:
                # 1. Calculate and clamp DAQ voltage
                daq_volt = current_target_v / HV_GAIN
                clamped_daq_volt = max(-10.0, min(10.0, daq_volt))
                
                # 2. Set Analog Output
                ao_task.write(clamped_daq_volt)
                
                # Tiny sleep to allow hardware to settle before reading counters
                if firstrun:
                    # Peter added, 5 seconds of sleep time before the first voltage 
                    # reading, to allow the EOM voltage to stabilize properly
                    time.sleep(5)
                    firstrun = False
                time.sleep(0.1) 
                
                # 3. Read initial counts and timestamp
                t_start = time.perf_counter()
                c0_start = ci_task0.read()
                c1_start = ci_task1.read()
                
                # 4. Integrate over dwell time
                time.sleep(DWELL_TIME_SEC)
                
                # 5. Read final counts and timestamp
                t_end = time.perf_counter()
                c0_end = ci_task0.read()
                c1_end = ci_task1.read()
                
                # 6. Calculate Rates
                dt = t_end - t_start
                hz0 = (c0_end - c0_start) / dt
                hz1 = (c1_end - c1_start) / dt
                
                # 7. Log to CSV and flush to disk immediately to prevent data loss on crash
                writer.writerow([current_target_v, clamped_daq_volt, hz0, hz1])
                file.flush()
                
                # 8. Print progress
                print(f"{current_target_v:10.1f} | {clamped_daq_volt:11.3f} | {hz0:10.1f} | {hz1:10.1f}")
                
                # Increment step
                current_target_v += STEP_V

        print(f"\nSweep Complete, saved to {csv_filename}")
                
    except KeyboardInterrupt:
        print("\nSweep interrupted by user.")
        
    finally:
        # Ensure voltage is safely returned to 0V
        print("Zeroing AO output...")
        ao_task.write(0.0)

import matplotlib.pyplot as plt

# Lists to hold data for plotting
target_voltages = []
pfi1_rates = []
pfi2_rates = []

# Read back the saved CSV file to plot the data
with open(csv_filename, mode='r') as file:
    reader = csv.DictReader(file)
    for row in reader:
        target_voltages.append(float(row["Target_Voltage_V"]))
        pfi1_rates.append(float(row["PFI1_Rate_Hz"]))
        pfi2_rates.append(float(row["PFI2_Rate_Hz"]))

# Create the plot
plt.figure(figsize=(10, 6))
plt.plot(target_voltages, pfi1_rates, label='PFI1 (ctr0)', marker='o', linestyle='-')
plt.plot(target_voltages, pfi2_rates, label='PFI2 (ctr1)', marker='s', linestyle='--')

plt.title(f"EOM Voltage Sweep - {timestamp}")
plt.xlabel("Target Voltage (V)")
plt.ylabel("Count Rate (Hz)")
plt.grid(True, which='both', linestyle='--', alpha=0.7)
plt.legend()
plt.tight_layout()

# Display the interactive window
plt.show()