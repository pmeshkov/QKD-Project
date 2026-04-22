import nidaqmx
from nidaqmx.constants import Edge, CountDirection
import nidaqmx.system
import matplotlib.pyplot as plt
from matplotlib.widgets import TextBox, Button
import collections
import time

# Hardware Parameters
device_name = "Dev1"
ao_channel = f"{device_name}/ao0"       
pfi_pin_0 = f"/{device_name}/PFI1"
pfi_pin_1 = f"/{device_name}/PFI2"

# Initial State
init_voltage = 0.0               

# Reset hardware
nidaqmx.system.Device(device_name).reset_device()
print(f"Hardware reset complete. DC Output on {ao_channel}, reading from {pfi_pin_0} and {pfi_pin_1}.")

# Plotting Setup
window_size = 100
rate_ctr0 = collections.deque([0] * window_size, maxlen=window_size)
rate_ctr1 = collections.deque([0] * window_size, maxlen=window_size)

buffer_0 = collections.deque()
buffer_1 = collections.deque()
AVERAGING_WINDOW_SEC = 1.0

plt.ion()
fig, ax = plt.subplots()
plt.subplots_adjust(bottom=0.25) # Leave room for the GUI widgets

line0, = ax.plot(rate_ctr0, label='PFI1 Rate (Hz)')
line1, = ax.plot(rate_ctr1, label='PFI2 Rate (Hz)')
ax.legend()
ax.set_xlabel(f"Samples (Last {window_size} updates)")
ax.set_ylabel("Counts Per Second (Hz)")
ax.set_title("Live EOM DC Bias & Dual Count Rate")

# --- Define GUI Widgets ---
# Text box for voltage input
ax_volt = fig.add_axes([0.25, 0.1, 0.2, 0.05])
volt_box = TextBox(ax_volt, 'Target EOM Volts: ', initial=str(init_voltage))

# Button for toggling the scale
ax_btn = fig.add_axes([0.6, 0.1, 0.2, 0.05])
scale_btn = Button(ax_btn, 'Scale: Linear')

# Amplifier Parameter
HV_GAIN = 20.0

# Scale State Management
scale_state = {'is_log': False}

def toggle_scale(event):
    scale_state['is_log'] = not scale_state['is_log']
    if scale_state['is_log']:
        ax.set_yscale('log')
        scale_btn.label.set_text('Scale: Log')
    else:
        ax.set_yscale('linear')
        scale_btn.label.set_text('Scale: Linear')

scale_btn.on_clicked(toggle_scale)

# Configure Tasks
with nidaqmx.Task() as ao_task, nidaqmx.Task() as ci_task0, nidaqmx.Task() as ci_task1:
    
    # 1. Configure Analog Output
    ao_task.ao_channels.add_ao_voltage_chan(ao_channel)
    ao_task.write(init_voltage)

    # 2. Define Text Box Callback (No set_val!)
    def submit_volt(text):
        try:
            target_hv = float(text)
            
            # Calculate required DAQ voltage
            daq_volt = target_hv / HV_GAIN
            
            # Clamp DAQ voltage to safe hardware limits (+- 5.0V max output)
            clamped_daq_volt = max(-10.0, min(10.0, daq_volt))
            
            # Send to hardware
            ao_task.write(clamped_daq_volt)
            
            # Calculate what the EOM is *actually* getting
            actual_hv = clamped_daq_volt * HV_GAIN
            
            # Update UI safely via the title
            ax.set_title(f"Target: {target_hv}V | DAQ Out: {clamped_daq_volt:.2f}V | EOM Bias: {actual_hv:.1f}V")
            
        except ValueError:
            ax.set_title("Error: Invalid Input. Hardware state unchanged.")

    volt_box.on_submit(submit_volt)

    # 3. Configure Counter Inputs
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
    
    print("Tasks started. Type a voltage and press ENTER. Bounded to ±100V.\n")
    
    try:
        while plt.fignum_exists(fig.number):
            now = time.perf_counter()
            
            count0 = ci_task0.read()
            count1 = ci_task1.read()
            
            buffer_0.append((now, count0))
            buffer_1.append((now, count1))
            
            while buffer_0 and (now - buffer_0[0][0]) > AVERAGING_WINDOW_SEC:
                buffer_0.popleft()
            while buffer_1 and (now - buffer_1[0][0]) > AVERAGING_WINDOW_SEC:
                buffer_1.popleft()
            
            hz0, hz1 = 0, 0
            if len(buffer_0) > 1:
                dt0 = buffer_0[-1][0] - buffer_0[0][0]
                if dt0 > 0: hz0 = (buffer_0[-1][1] - buffer_0[0][1]) / dt0
                    
            if len(buffer_1) > 1:
                dt1 = buffer_1[-1][0] - buffer_1[0][0]
                if dt1 > 0: hz1 = (buffer_1[-1][1] - buffer_1[0][1]) / dt1

            rate_ctr0.append(hz0)
            rate_ctr1.append(hz1)
            
            line0.set_ydata(rate_ctr0)
            line1.set_ydata(rate_ctr1)
            
            max_rate = max(max(rate_ctr0), max(rate_ctr1))
            
            # Dynamically adjust limits based on current scale mode
            if scale_state['is_log']:
                # Establish a non-zero floor for the logarithmic scale
                ax.set_ylim(0.1, max(10, max_rate * 1.5))
            else:
                ax.set_ylim(0, max_rate + max_rate * 0.1 + 5) 
            
            fig.canvas.flush_events()
            plt.pause(0.1)  
            
    except KeyboardInterrupt:
        pass

plt.ioff()
plt.show()
print("Stopped.")