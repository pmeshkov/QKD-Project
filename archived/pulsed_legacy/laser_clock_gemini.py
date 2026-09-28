import math
import tkinter as tk
from tkinter import ttk, messagebox

# Delay DAQmx import for offline testing
try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, Level
    DAQMX_AVAILABLE = True
except ImportError:
    DAQMX_AVAILABLE = False

TIMEBASE_HZ = 100_000_000

def clock_plan(frequency_hz, high_ns):
    """Round to 10 ns ticks and validate the realized waveform."""
    if not math.isfinite(frequency_hz) or not 1_000 <= frequency_hz <= 10_000_000:
        raise ValueError("Frequency must be 1 kHz through 10 MHz.")
    if not math.isfinite(high_ns) or high_ns <= 0:
        raise ValueError("High time must be finite and positive.")
    
    period_ticks = round(TIMEBASE_HZ / frequency_hz)
    high_ticks = round(high_ns / 10)
    low_ticks = period_ticks - high_ticks
    
    if min(high_ticks, low_ticks) < 2:
        raise ValueError("Each pulse phase must have at least two timebase ticks.")
    if high_ticks / period_ticks > 0.30:
        raise ValueError("Realized duty cycle exceeds the BDL limit of 30%.")
        
    return {
        "nominal_timebase_hz": TIMEBASE_HZ,
        "high_ticks": high_ticks,
        "low_ticks": low_ticks,
        "realized_frequency_hz": TIMEBASE_HZ / period_ticks,
        "realized_duty_cycle": high_ticks / period_ticks,
    }

class ClockGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Live NI-DAQmx Clock Control")
        self.task = None
        self.device = "Dev1"
        self.is_running = False

        # --- UI Elements ---
        frame = ttk.Frame(root, padding="10")
        frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        ttk.Label(frame, text="Frequency (Hz):").grid(row=0, column=0, sticky=tk.W)
        self.freq_var = tk.DoubleVar(value=1_000_000)
        ttk.Entry(frame, textvariable=self.freq_var).grid(row=0, column=1, pady=5)

        ttk.Label(frame, text="High Time (ns):").grid(row=1, column=0, sticky=tk.W)
        self.high_ns_var = tk.DoubleVar(value=200)
        ttk.Entry(frame, textvariable=self.high_ns_var).grid(row=1, column=1, pady=5)

        self.status_var = tk.StringVar(value="Status: Stopped")
        ttk.Label(frame, textvariable=self.status_var, foreground="blue").grid(row=2, column=0, columnspan=2, pady=5)

        self.btn_toggle = ttk.Button(frame, text="Start Hardware", command=self.toggle_task)
        self.btn_toggle.grid(row=3, column=0, pady=10)

        self.btn_update = ttk.Button(frame, text="Update Live", command=self.apply_settings, state=tk.DISABLED)
        self.btn_update.grid(row=3, column=1, pady=10)

    def toggle_task(self):
        if not self.is_running:
            self.start_hardware()
        else:
            self.stop_hardware()

    def start_hardware(self):
        if not DAQMX_AVAILABLE:
            messagebox.showerror("Error", "nidaqmx is not installed or device is missing.")
            return
            
        try:
            plan = clock_plan(self.freq_var.get(), self.high_ns_var.get())
            self.task = nidaqmx.Task()
            channel = self.task.co_channels.add_co_pulse_chan_ticks(
                f"{self.device}/ctr0",
                source_terminal=f"/{self.device}/100MHzTimebase",
                idle_state=Level.LOW,
                initial_delay=plan["low_ticks"],
                low_ticks=plan["low_ticks"],
                high_ticks=plan["high_ticks"]
            )
            channel.co_pulse_term = f"/{self.device}/PFI12"
            self.task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.CONTINUOUS)
            
            self.task.start()
            self.is_running = True
            self.btn_toggle.config(text="Stop Hardware")
            self.btn_update.config(state=tk.NORMAL)
            self.status_var.set(f"Status: Running at {plan['realized_frequency_hz']/1000:.1f} kHz")
        except Exception as e:
            messagebox.showerror("Hardware Error", str(e))
            self.stop_hardware()

    def apply_settings(self):
        if self.is_running:
            # Safest way to change continuous ticks is stop -> reconfigure -> start
            self.stop_hardware()
            self.start_hardware()

    def stop_hardware(self):
        if self.task:
            self.task.stop()
            self.task.close()
            self.task = None
        self.is_running = False
        self.btn_toggle.config(text="Start Hardware")
        self.btn_update.config(state=tk.DISABLED)
        self.status_var.set("Status: Stopped")

if __name__ == "__main__":
    root = tk.Tk()
    app = ClockGUI(root)
    root.mainloop()