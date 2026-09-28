"""Tk-only live view for laser_clock_eom_counts; no hardware calls here."""
from collections import deque
import math
import queue
import time
import tkinter as tk
from tkinter import ttk

from laser_clock_eom import eom_plan
from laser_clock_eom_counts import live_clock


class LiveCountsWindow:
    def __init__(self, parent, cfg, commands, on_stop):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        self.window = tk.Toplevel(parent)
        self.window.title("Live alignment — PicoHarp counts, EOM voltages and laser clock")
        self.window.geometry("1100x790")
        self.window.minsize(900, 650)
        self.cfg, self.commands, self.on_stop = cfg, commands, on_stop
        self.active, self.stopping, self.close_requested, self.pending = True, False, False, False
        self.epoch = -1
        self.history = deque(maxlen=6002)
        self.average = deque(maxlen=602)
        self.last_sample_at = None
        self.status = tk.StringVar(value="Opening PicoHarp and reserving NI outputs…")
        self.applied = tk.StringVar(value="No applied settings acknowledged yet.")
        self.health = tk.StringVar(value="Waiting for rate meters…")
        self.cleanup_message = ""
        header = ttk.Frame(self.window, padding=12)
        header.pack(fill="x")
        self.cards = {}
        for label in ("CH1", "CH2", "SUM", "SYNC"):
            box = ttk.LabelFrame(header, text=label, padding=8)
            box.pack(side="left", fill="x", expand=True, padx=4)
            self.cards[label] = tk.StringVar(value="—")
            ttk.Label(box, textvariable=self.cards[label], font=("Segoe UI", 18)).pack()
        ttk.Label(self.window, text=f"CH1/CH2/SUM: mean of rate-meter readings over the last {cfg['average_s']:g} s "
                  "(0 = latest). SYNC: latest meter reading. Units: counts/s.", padding=(12, 0)).pack(anchor="w")
        self.figure = Figure(figsize=(10, 4), layout="constrained")
        self.ax = self.figure.subplots()
        self.lines = [self.ax.plot([], [], label=label, color=color)[0] for label, color in
                      (("CH1", "#2563a6"), ("CH2", "#cc5522"), ("CH1 + CH2", "#21834a"))]
        self.ax.set(xlabel="Elapsed time (s)", ylabel="Count rate (counts/s)", xlim=(0, cfg["history_s"]), ylim=(0, 10))
        self.ax.grid(alpha=.2)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.window)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=12)
        controls = ttk.Frame(self.window, padding=12)
        controls.pack(fill="x")
        self.vars, self.entries = {}, []
        initial = {"eom1-v": cfg["eom"]["target_eom_v"][0], "eom2-v": cfg["eom"]["target_eom_v"][1],
                   "frequency-hz": cfg["clock"]["requested_frequency_hz"], "high-ns": cfg["clock"]["requested_high_ns"]}
        for col, (key, label) in enumerate((("eom1-v", "Alice / AO0 target (V)"), ("eom2-v", "Bob / AO1 target (V)"),
                                           ("frequency-hz", "Laser rate (Hz)"), ("high-ns", "Trigger high time (ns)"))):
            ttk.Label(controls, text=label).grid(row=0, column=col, sticky="w", padx=5)
            self.vars[key] = tk.StringVar(value=f"{initial[key]:g}")
            entry = ttk.Entry(controls, textvariable=self.vars[key], width=20, state="disabled")
            entry.grid(row=1, column=col, sticky="ew", padx=5, pady=4)
            entry.bind("<Return>", lambda event, kind="eom" if col < 2 else "clock": self.submit(kind))
            self.entries.append(entry)
            controls.columnconfigure(col, weight=1)
        self.eom_button = ttk.Button(controls, text="Apply both EOM voltages", command=lambda: self.submit("eom"), state="disabled")
        self.eom_button.grid(row=2, column=0, columnspan=2, sticky="ew", padx=5)
        self.clock_button = ttk.Button(controls, text="Apply clock (brief restart)", command=lambda: self.submit("clock"), state="disabled")
        self.clock_button.grid(row=2, column=2, columnspan=2, sticky="ew", padx=5)
        options = ttk.Frame(self.window, padding=(12, 0))
        options.pack(fill="x")
        self.log_scale, self.show_sum = tk.BooleanVar(value=False), tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Log scale (zero counts omitted)", variable=self.log_scale, command=self.draw).pack(side="left")
        ttk.Checkbutton(options, text="Show combined trace", variable=self.show_sum, command=self.draw).pack(side="left", padx=16)
        self.stop_button = ttk.Button(options, text="Stop outputs and counts", command=self.on_stop)
        self.stop_button.pack(side="right")
        for variable in (self.applied, self.health, self.status):
            ttk.Label(self.window, textvariable=variable, wraplength=1050, padding=(12, 3)).pack(fill="x")
        ttk.Label(self.window, text="Manual alignment only. DAQ = −target/20; ±200 V targets. "
                  "Clock changes interrupt the pulse train. Stop returns AO0/AO1 to 0 V.", padding=(12, 6)).pack(anchor="w")
        self.window.protocol("WM_DELETE_WINDOW", self.request_close)
        self.draw()

    def exists(self):
        return bool(self.window.winfo_exists())

    def enable(self, enabled):
        state = "normal" if enabled and self.active and not self.stopping else "disabled"
        for widget in [*self.entries, self.eom_button, self.clock_button]:
            widget.configure(state=state)

    def submit(self, kind):
        if not self.active or self.stopping or self.pending or self.epoch < 0:
            return
        try:
            plan = eom_plan(self.vars["eom1-v"].get(), self.vars["eom2-v"].get()) if kind == "eom" else live_clock(
                self.vars["frequency-hz"].get(), self.vars["high-ns"].get())
            self.commands.put_nowait((kind, plan))
            self.pending = True
            self.enable(False)
            self.status.set("Adjustment queued; waiting for hardware acknowledgement…")
        except (ValueError, TypeError, queue.Full) as exc:
            self.status.set(f"Not applied: {exc or 'previous command is still pending'}")

    def handle(self, event):
        if not self.exists():
            return
        kind = event["kind"]
        if kind == "applied":
            self.epoch = event["epoch"]
            self.average.clear()
            if self.history:
                self.history.append((self.history[-1][0], math.nan, math.nan, math.nan))
            self.last_sample_at = None
            for var in self.cards.values():
                var.set("—")
            clock, eom = event["clock"], event["eom"]
            self.applied.set(f"Applied commands: Alice {eom['target_eom_v'][0]:g} V | Bob {eom['target_eom_v'][1]:g} V | "
                             f"NI clock {clock['realized_nominal_frequency_hz']:,.3f} Hz | pulse {clock['realized_high_ns']:g} ns")
            self.health.set("Waiting for fresh rate-meter readings after output change…")
            self.status.set("Running. Edit a value and press Enter or click Apply.")
            self.pending = False
            self.enable(True)
        elif kind == "rejected":
            self.pending = False
            self.enable(True)
            self.status.set("Not applied: " + event["message"])
        elif kind == "error":
            self.status.set("Hardware error: " + event["message"])
            self.enable(False)
        elif kind == "cleanup":
            self.cleanup_message = ("AO0/AO1 returned to 0 V." if event["zeroed"] else "AO zeroing not confirmed; see log.")
            if event["errors"]:
                self.cleanup_message += " Cleanup errors: " + "; ".join(event["errors"])

    def sample(self, reading):
        if not self.exists() or not self.active or self.stopping or reading["epoch"] != self.epoch:
            return
        now = reading["elapsed_s"]
        self.last_sample_at = time.monotonic()
        self.average.append((now, reading["ch1_hz"], reading["ch2_hz"]))
        while len(self.average) > 1 and (self.cfg["average_s"] == 0 or now - self.average[0][0] > self.cfg["average_s"]):
            self.average.popleft()
        ch1 = sum(r[1] for r in self.average) / len(self.average)
        ch2 = sum(r[2] for r in self.average) / len(self.average)
        self.history.append((now, ch1, ch2, ch1 + ch2))
        while self.history and now - self.history[0][0] > self.cfg["history_s"]:
            self.history.popleft()
        for label, value in (("CH1", ch1), ("CH2", ch2), ("SUM", ch1 + ch2), ("SYNC", reading["sync_hz"])):
            self.cards[label].set(f"{value:,.1f}" if label != "SYNC" else f"{value:,}")
        health = "SYNC within 5% of commanded rate." if reading["sync_matches"] else "SYNC MISMATCH — check BDL triggering and SYNC input."
        if reading["warnings"]:
            health += " PicoHarp: " + (reading["warnings_text"] or f"warning bits {reading['warnings']:#x}")
        self.health.set(health)
        self.draw()

    def tick(self):
        if self.exists() and self.active and not self.stopping and self.last_sample_at is not None:
            if time.monotonic() - self.last_sample_at > max(2, 3 * self.cfg["poll_s"]):
                self.health.set("No fresh meter reading — displayed values are stale. Check status log.")

    def draw(self):
        is_log = self.log_scale.get()
        self.ax.set_yscale("log" if is_log else "linear")
        x = [v[0] for v in self.history]
        visible = []
        for i, line in enumerate(self.lines):
            y = [v[i + 1] if not is_log or v[i + 1] > 0 else math.nan for v in self.history]
            line.set_data(x, y)
            line.set_visible(i < 2 or self.show_sum.get())
            if line.get_visible():
                visible.extend(v for v in y if math.isfinite(v))
        high = max(visible, default=0)
        if is_log:
            low = max(1e-3, min(visible, default=1) / 2)
            self.ax.set_ylim(low, max(low * 10, high * 1.3, 10))
        else:
            self.ax.set_ylim(0, max(10, high * 1.15))
        end = max(self.cfg["history_s"], x[-1] if x else 0)
        self.ax.set_xlim(max(0, end - self.cfg["history_s"]), end)
        shown = [line for line in self.lines if line.get_visible()]
        self.ax.legend(shown, [line.get_label() for line in shown], loc="upper right")
        self.canvas.draw_idle()

    def stopping_now(self):
        if self.exists():
            self.stopping = True
            self.enable(False)
            self.stop_button.configure(state="disabled")
            self.status.set("Stopping clock, zeroing AO and closing PicoHarp…")

    def finish(self, code):
        if not self.exists():
            return
        self.active = False
        self.enable(False)
        self.stop_button.configure(state="disabled")
        self.status.set(("Stopped. " if code in (0, 130) else "Error — see launcher log. ") + self.cleanup_message)
        if self.close_requested:
            self.window.destroy()

    def request_close(self):
        if self.active:
            self.close_requested = True
            self.on_stop()
        else:
            self.window.destroy()
