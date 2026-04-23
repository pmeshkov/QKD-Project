import nidaqmx
from nidaqmx.constants import AcquisitionType
import numpy as np
import nidaqmx.system

# A script that just outputs sign waves on two analog outputs,
# useful for checking that everything is set up

# Resets the hardware and clears all reserved resources
nidaqmx.system.Device("Dev1").reset_device()
# Parameters
sample_rate = 1000000       
frequency = 1000          
amplitude = 5.0            
offset = 0.0               
samples_per_cycle = int(sample_rate / frequency)
num_cycles = 10            

# Generate the 1D base sine wave
t = np.arange(samples_per_cycle * num_cycles) / sample_rate
data = offset + amplitude * np.sin(2 * np.pi * frequency * t)

# Stack the 1D array into a 2D array for two channels
# np.vstack creates an array structured as [channel_0_data, channel_1_data]
multi_channel_data = np.vstack((data, data)) 

# Create task
with nidaqmx.Task() as task:
    # Add both channels. "ao0:1" adds AO 0 and AO 1 simultaneously.
    # Alternatively, you can use "Dev1/ao0, Dev1/ao1"
    task.ao_channels.add_ao_voltage_chan("Dev1/ao0:1")

    # Configure timing for continuous output
    task.timing.cfg_samp_clk_timing(
        rate=sample_rate,
        sample_mode=AcquisitionType.CONTINUOUS
    )

    # Write the 2D data to the buffer
    task.write(multi_channel_data, auto_start=False)

    # Start output
    task.start()

    print("Generating synchronized sine waves on AO0 and AO1... Press Ctrl+C to stop.")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("Stopped.")