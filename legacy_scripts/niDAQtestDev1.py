import nidaqmx
from nidaqmx.constants import AcquisitionType
import numpy as np
import nidaqmx.system

# Outputs a sine wave on AO1 while holding AO0 at 0 V

# Resets the hardware and clears all reserved resources
nidaqmx.system.Device("Dev1").reset_device()

# Parameters
sample_rate = 1000
frequency = 0.3
amplitude = 8.0
offset = 0.0
samples_per_cycle = int(sample_rate / frequency)
num_cycles = 10

# Generate the sine wave for AO1
t = np.arange(samples_per_cycle * num_cycles) / sample_rate
ao1_data = offset + amplitude * np.sin(2 * np.pi * frequency * t)

# AO0 stays at 0 V
ao0_data = np.zeros_like(ao1_data)

# Stack data as [AO0, AO1]
multi_channel_data = np.vstack((ao0_data, ao1_data))

# Create task
with nidaqmx.Task() as task:
    # Add AO0 and AO1
    task.ao_channels.add_ao_voltage_chan("Dev1/ao0:1")

    # Configure timing for continuous output
    task.timing.cfg_samp_clk_timing(
        rate=sample_rate,
        sample_mode=AcquisitionType.CONTINUOUS
    )

    # Write data to the output buffer
    task.write(multi_channel_data, auto_start=False)

    # Start output
    task.start()

    print("AO0 = 0 V, AO1 = sine wave. Press Ctrl+C to stop.")

    try:
        while True:
            pass
    except KeyboardInterrupt:
        print("Stopped.")
