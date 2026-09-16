import nidaqmx
import nidaqmx.system

# Resets the hardware and clears all reserved resources
nidaqmx.system.Device("Dev1").reset_device()

with nidaqmx.Task() as task:
    # Add both channels
    task.ao_channels.add_ao_voltage_chan("Dev1/ao0:1")

    # Write a single data point of 0.0V to both channels
    task.write([0.0, 0.0], auto_start=True)

print("AO0 and AO1 have been successfully set to 0.0 V. Good night!")