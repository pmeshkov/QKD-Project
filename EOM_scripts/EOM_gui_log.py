import nidaqmx
from nidaqmx.constants import Edge, CountDirection
import nidaqmx.system
import matplotlib.pyplot as plt
from matplotlib.widgets import TextBox, Button
import collections
import time

# Hardware Parameters
device_name = "Dev1"
ao_channel_0 = f"{device_name}/ao0"
ao_channel_1 = f"{device_name}/ao1"       
pfi_pin_0 = f"/{device_name}/PFI1"
pfi_pin_1 = f"/{device_name}/PFI2"

# Initial State
init_voltage_0 = 0.0
init_voltage_1 = 0.0               

# Reset hardware
nidaqmx.system.Device(device_name).reset_device()
print(f"Hardware reset complete. DC Output on {ao_channel_0} and {ao_channel_1}, reading from {pfi_pin_0} and {pfi_pin_1}.")

# Plotting Setup
window_size = 100
rate_ctr0 = collections.deque([0] * window_size, maxlen=window_size)
rate_ctr1 = collections.deque([0] * window_size, maxlen=window_size)
rate_combined = collections.deque([0] * window_size, maxlen=window_size)

buffer_0 = collections.deque()
buffer_1 = collections.deque()
AVERAGING_WINDOW_SEC = 0.5

plt.ion()
fig, ax = plt.subplots()
plt.subplots_adjust(bottom=0.25) # Leave room for the GUI widgets

line0, = ax.plot(rate_ctr0, label='PFI1 Rate (Hz)')
line1, = ax.plot(rate_ctr1, label='PFI2 Rate (Hz)')
line_combined, = ax.plot(rate_combined, label='Sum Rate (Hz)', color='green', visible=False)
ax.legend()
ax.set_xlabel(f"Samples (Last {window_size} updates)")
ax.set_ylabel("Counts Per Second (Hz)")
ax.set_title("Live EOM DC Bias & Dual Count Rate")

# --- Define GUI Widgets ---
# Adjusted positions to fit 4 widgets evenly
ax_volt_0 = fig.add_axes([0.05, 0.1, 0.22, 0.05])
volt_box_0 = TextBox(ax_volt_0, 'EOM0: ', initial=str(init_voltage_0))

ax_volt_1 = fig.add_axes([0.29, 0.1, 0.22, 0.05])
volt_box_1 = TextBox(ax_volt_1, 'EOM1: ', initial=str(init_voltage_1))

ax_btn_scale = fig.add_axes([0.53, 0.1, 0.20, 0.05])
scale_btn = Button(ax_btn_scale, 'Scale: Linear')

ax_btn_comb = fig.add_axes([0.75, 0.1, 0.22, 0.05])
comb_btn = Button(ax_btn_comb, 'Comb. Trace: Off')

# Amplifier Parameter
HV_GAIN = 20.0

# State Management
scale_state = {'is_log': False}
trace_state = {'show_combined': False}

def toggle_scale(event):
    scale_state['is_log'] = not scale_state['is_log']
    if scale_state['is_log']:
        ax.set_yscale('log')
        scale_btn.label.set_text('Scale: Log')
    else:
        ax.set_yscale('linear')
        scale_btn.label.set_text('Scale: Linear')

def toggle_combined(event):
    trace_state['show_combined'] = not trace_state['show_combined']
    line_combined.set_visible(trace_state['show_combined'])
    if trace_state['show_combined']:
        comb_btn.label.set_text('Comb. Trace: On')
    else:
        comb_btn.label.set_text('Comb. Trace: Off')
    ax.legend() # Refresh legend to reflect visibility

scale_btn.on_clicked(toggle_scale)
comb_btn.on_clicked(toggle_combined)

# State array to persist the DAQ voltage for both channels simultaneously 
current_daq_volts = [init_voltage_0 / HV_GAIN, init_voltage_1 / HV_GAIN]

# Configure Tasks
with nidaqmx.Task() as ao_task, nidaqmx.Task() as ci_task0, nidaqmx.Task() as ci_task1:
    
    # 1. Configure Analog Outputs (Grouped in one task to avoid resource conflict)
    ao_task.ao_channels.add_ao_voltage_chan(ao_channel_0)
    ao_task.ao_channels.add_ao_voltage_chan(ao_channel_1)
    ao_task.write(current_daq_volts)

    # 2. Define Text Box Callbacks
    def submit_volt_0(text):
        try:
            target_hv = float(text)
            # inverting here, see note below
            daq_volt = -target_hv / HV_GAIN 
            clamped_daq_volt = max(-10.0, min(10.0, daq_volt))
            
            current_daq_volts[0] = clamped_daq_volt
            ao_task.write(current_daq_volts)
            
            actual_hv_0 = current_daq_volts[0] * HV_GAIN
            actual_hv_1 = current_daq_volts[1] * HV_GAIN

            # Peter Note 9/2/26
            # The amplifier seemingly inverts the voltage. We are inverting to reverse this.
            # Thus, the EOM Bias shown in the GUI should be multiplied by -1.
            ax.set_title(f"EOM0 Bias: {-actual_hv_0:.1f}V | EOM1 Bias: {-actual_hv_1:.1f}V")
        except ValueError:
            ax.set_title("Error: Invalid Input for EOM0. Hardware state unchanged.")

    def submit_volt_1(text):
        try:
            target_hv = float(text)
            # inverting here, see note below
            daq_volt = -target_hv / HV_GAIN 
            clamped_daq_volt = max(-10.0, min(10.0, daq_volt))
            
            current_daq_volts[1] = clamped_daq_volt
            ao_task.write(current_daq_volts)
            
            actual_hv_0 = current_daq_volts[0] * HV_GAIN
            actual_hv_1 = current_daq_volts[1] * HV_GAIN

            # Peter Note 9/2/26
            # The amplifier seemingly inverts the voltage. We are inverting to reverse this.
            # Thus, the EOM Bias shown in the GUI should be multiplied by -1.
            ax.set_title(f"EOM0 Bias: {-actual_hv_0:.1f}V | EOM1 Bias: {-actual_hv_1:.1f}V")
        except ValueError:
            ax.set_title("Error: Invalid Input for EOM1. Hardware state unchanged.")

    volt_box_0.on_submit(submit_volt_0)
    volt_box_1.on_submit(submit_volt_1)

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
            
            hz_combined = hz0 + hz1

            rate_ctr0.append(hz0)
            rate_ctr1.append(hz1)
            rate_combined.append(hz_combined)
            
            line0.set_ydata(rate_ctr0)
            line1.set_ydata(rate_ctr1)
            line_combined.set_ydata(rate_combined)
            
            # Determine maximum rate based on trace visibility
            if trace_state['show_combined']:
                max_rate = max(max(rate_ctr0), max(rate_ctr1), max(rate_combined))
            else:
                max_rate = max(max(rate_ctr0), max(rate_ctr1))
            
            # Dynamically adjust limits based on current scale mode
            if scale_state['is_log']:
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