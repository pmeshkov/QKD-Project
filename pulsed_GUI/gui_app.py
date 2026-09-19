"""Parameter windows for the copied pulsed tools. Opening a window never accesses hardware."""
from contextlib import redirect_stdout, redirect_stderr
import importlib
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText

ROOT = Path(__file__).resolve().parent
SETTINGS = ROOT / "gui_settings.json"
DLL = r"C:\Program Files\PicoQuant\UniHarp\PH330Lib.dll"
TOOLS = {
    "Connection test": "ph330",
    "Laser clock": "laser_clock",
    "Laser clock + EOM": "laser_clock_eom",
    "G2 acquisition": "ph330_acquire",
    "Preview saved run": "ph330_preview",
    "CH1 lifetime": "ph330_lifetime",
}

# name, visible label (with units), initial value, optional choices or file picker.
COMMON = [
    ("dll", "PicoHarp DLL", DLL, "file"),
    ("device-index", "Device index", "0", None),
    ("serial", "Device serial", "1050578", None),
    ("laser-hz", "Expected laser rate (Hz)", "2000000", None),
    ("binning", "T3 binning code (6 → 64 ps on this device)", "6", None),
    ("sync-edge", "SYNC edge — choose for measured signal", "", ["rising", "falling"]),
    ("sync-level-mv", "SYNC threshold (mV, signed)", "", None),
    ("ch1-level-mv", "CH1 threshold (mV)", "300", None),
]
SPECS = {
    "Laser clock + EOM": [
        ("action", "Action", "Check settings", ["Check settings", "Output clock + EOM"]),
        ("device", "NI device", "Dev1", None),
        ("frequency-hz", "Clock frequency (Hz) — editable while running", "1000000", None),
        ("high-ns", "Positive pulse width (ns) — editable while running", "200", None),
        ("eom1-v", "EOM 1 target (V) → AO0; DAQ = −target / 20", "0", None),
        ("eom2-v", "EOM 2 target (V) → AO1; DAQ = −target / 20", "0", None),
        ("seconds", "Duration (s; 0 = run until Stop)", "0", None),
        ("log-dir", "Run log directory", str(ROOT / "runs" / "clock_eom"), "directory"),
        ("note", "Measurement notes", "", None),
    ],
    "Connection test": [
        ("action", "Action", "Find devices", ["Find devices", "Check DLL"]),
        COMMON[0],
    ],
    "Laser clock": [
        ("action", "Action", "Check settings", ["Check settings", "Output clock"]),
        ("device", "NI device", "Dev1", None),
        ("frequency-hz", "Clock frequency (Hz)", "1000000", None),
        ("high-ns", "Positive pulse width (ns)", "200", None),
        ("seconds", "Duration (s)", "60", None),
        ("log-dir", "Clock log directory", str(ROOT / "runs" / "clock"), "directory"),
        ("note", "Measurement notes", "", None),
    ],
    "G2 acquisition": [
        ("action", "Action", "Check settings", ["Check settings", "Check rates", "Record"]),
        *COMMON,
        ("ch2-level-mv", "CH2 threshold (mV)", "300", None),
        ("sync-mode", "SYNC trigger mode", "edge", ["edge", "cfd"]),
        ("sync-zero", "SYNC CFD zero crossing (mV; CFD only)", "-10", None),
        ("ch1-mode", "CH1 trigger mode", "edge", ["edge", "cfd"]),
        ("ch1-edge", "CH1 edge", "rising", ["rising", "falling"]),
        ("ch1-zero", "CH1 CFD zero crossing (mV; CFD only)", "-10", None),
        ("ch2-mode", "CH2 trigger mode", "edge", ["edge", "cfd"]),
        ("ch2-edge", "CH2 edge", "rising", ["rising", "falling"]),
        ("ch2-zero", "CH2 CFD zero crossing (mV; CFD only)", "-10", None),
        ("seconds", "Duration (s)", "10", None),
        ("output", "Data directory", str(ROOT / "runs" / "ph330"), "directory"),
        ("preview", "Make plots after recording", "Yes", ["Yes", "No"]),
        ("notes", "Measurement notes", "Internal 2 MHz laser; CH1 transmitted, CH2 reflected.", None),
    ],
    "Preview saved run": [
        ("folder", "Run folder containing metadata.json", "", "directory"),
        ("max-records", "Maximum raw records to preview", "2000000", None),
    ],
    "CH1 lifetime": [
        ("action", "Action", "Check settings", ["Check settings", "Check rates", "Record", "Analyze saved run"]),
        *COMMON,
        ("seconds", "Duration (s)", "60", None),
        ("output", "Data directory", str(ROOT / "runs" / "lifetime_ch1"), "directory"),
        ("folder", "Existing run folder (Analyze only)", "", "directory"),
        ("rebin", "Combine native bins for plot/fit", "8", None),
        ("fit", "Fit an exponential decay tail", "No", ["No", "Yes"]),
        ("fit-start", "Fit start (ns after SYNC)", "", None),
        ("fit-stop", "Fit end (ns after SYNC)", "", None),
    ],
}
HELP = {
    "Laser clock + EOM": "Manual alignment: Ctr0/PFI12 clock plus static AO0/AO1 biases. EOM targets ±200 V using the existing −20 gain convention. Apply EOM keeps the clock running; Apply clock briefly stops/restarts it. Stop returns both AO channels to 0 V.",
    "Connection test": "Close UniHarp first. Find devices opens/closes the PicoHarp without starting a measurement.",
    "Laser clock": "Ctr0 → PFI12. Check on an oscilloscope first. The DAQ/laser impedance interface is still required. Duration is approximate.",
    "G2 acquisition": "CH1 = transmitted, CH2 = reflected. This profile expects 2 MHz and SYNC divider 1. Verify signal levels into 50 Ω; a threshold does not attenuate a TTL pulse.",
    "Preview saved run": "Offline only. Plots: counts versus time, photon delays after SYNC, and raw CH2−CH1 coincidences. The preview uses only the selected record prefix.",
    "CH1 lifetime": "Records CH1 only; CH2 is disabled. Start without fitting, inspect the decay, then Analyze saved run with a tail interval. Fits are preliminary, without IRF correction.",
}


def defaults(tool):
    return {key: value for key, _, value, _ in SPECS[tool]}


def required(values, key):
    value = values[key].strip()
    if not value:
        raise ValueError(f"Enter or select {key}.")
    return value


def acquisition_config(values):
    """Build the existing two-detector configuration from visible form fields."""
    def trigger(prefix):
        mode = required(values, prefix + "-mode")
        result = {"mode": mode, "edge": required(values, prefix + "-edge"),
                  "level_mv": int(required(values, prefix + "-level-mv"))}
        if mode == "cfd":
            result["zero_cross_mv"] = int(required(values, prefix + "-zero"))
        return result
    return {"device_index": int(values["device-index"]), "serial": required(values, "serial"),
            "laser_hz": float(values["laser-hz"]), "sync_divider": 1,
            "binning": int(values["binning"]), "sync": trigger("sync"),
            "detectors": [dict(trigger("ch1"), channel=0, label="CH1 transmitted HBT arm"),
                          dict(trigger("ch2"), channel=1, label="CH2 reflected HBT arm")],
            "notes": values["notes"]}


def arguments(tool, values):
    """Convert form values to the copied backends' existing, tested arguments."""
    action = values.get("action")
    args = []
    if tool == "Connection test":
        keys = ["dll"]
        if action == "Find devices":
            args.append("--probe")
    elif tool in ("Laser clock", "Laser clock + EOM"):
        keys = ["device", "frequency-hz", "high-ns", "seconds", "log-dir"]
        if tool == "Laser clock + EOM":
            keys += ["eom1-v", "eom2-v"]
        args += ["--note", values["note"]]
        if action in ("Output clock", "Output clock + EOM"):
            args.append("--run")
    elif tool == "Preview saved run":
        args.append(required(values, "folder"))
        keys = ["max-records"]
    elif tool == "G2 acquisition":
        keys = ["dll", "seconds", "output"]
        if action == "Record" and values["preview"] == "Yes":
            args.append("--preview")
    else:
        keys = ["rebin"]
        if action == "Analyze saved run":
            args += ["--analyze", required(values, "folder")]
        else:
            keys += [key for key, *_ in COMMON] + ["seconds", "output"]
        if values["fit"] == "Yes":
            args += ["--fit-window", required(values, "fit-start"), required(values, "fit-stop")]
    if action == "Check rates":
        args.append("--rates")
    elif action == "Record":
        args.append("--acquire")
    for key in keys:
        # Equals form allows signed values and paths containing spaces without shell parsing.
        args.append(f"--{key}={required(values, key)}")
    return args


def execute(tool, values, stop_event, commands=None):
    args = arguments(tool, values)
    module = importlib.import_module(TOOLS[tool])
    kwargs = {"stop_event": stop_event} if tool in ("Laser clock", "Laser clock + EOM", "G2 acquisition", "CH1 lifetime") else {}
    if tool == "Laser clock + EOM":
        kwargs["commands"] = commands
    if stop_event.is_set():
        return 130
    if tool == "G2 acquisition":
        config = acquisition_config(values)
        module.validate(config)
        # The backend expects a config file. It copies the full config into run metadata.
        with tempfile.TemporaryDirectory(prefix="qkd_gui_") as temp:
            path = Path(temp) / "config.json"
            path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            return module.main(["--config", str(path), *args], **kwargs)
    return module.main(args, **kwargs)


class QueueWriter:
    def __init__(self, events):
        self.events = events

    def write(self, text):
        if text:
            self.events.put(("log", text))
        return len(text)

    def flush(self):
        pass


class App:
    def __init__(self, root, tool="CH1 lifetime"):
        self.root = root
        root.title("Pulsed experiment controls")
        root.geometry("1100x780")
        root.minsize(850, 580)
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.commands = queue.Queue(maxsize=1)
        self.running = self.closing = False
        self.last_folder = self.last_plot = None
        self.settings = {}
        try:
            saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.settings = saved
        except (OSError, ValueError):
            pass
        self.tool = tk.StringVar(value=tool)
        top = ttk.Frame(root, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="Tool").pack(side="left", padx=(0, 10))
        self.selector = ttk.Combobox(top, textvariable=self.tool, values=list(TOOLS), state="readonly", width=25)
        self.selector.pack(side="left")
        self.selector.bind("<<ComboboxSelected>>", self.change_tool)
        ttk.Label(top, text="No hardware starts until you click Run.").pack(side="left", padx=16)
        self.help = ttk.Label(root, wraplength=1030, padding=(12, 0, 12, 12))
        self.help.pack(fill="x")
        self.livebar = ttk.Frame(root)
        self.apply_eom_button = ttk.Button(self.livebar, text="Apply EOM voltages", command=lambda: self.apply_live("eom"), state="disabled")
        self.apply_eom_button.pack(side="left")
        self.apply_clock_button = ttk.Button(self.livebar, text="Apply clock (restart)", command=lambda: self.apply_live("clock"), state="disabled")
        self.apply_clock_button.pack(side="left", padx=8)
        ttk.Label(self.livebar, text="Edit fields below, then Apply. Applied values appear in the log.").pack(side="left")
        panes = self.panes = ttk.Panedwindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=12)
        left = ttk.Frame(panes)
        panes.add(left, weight=3)
        self.canvas = tk.Canvas(left, highlightthickness=0, width=560)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.form = ttk.Frame(self.canvas, padding=(0, 0, 12, 12))
        self.form_id = self.canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.form_id, width=e.width))
        root.bind("<MouseWheel>", self.scroll_form)
        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        ttk.Label(right, text="Status, count rates and saved file locations").pack(anchor="w")
        self.log = ScrolledText(right, wrap="word", width=42, state="disabled")
        self.log.pack(fill="both", expand=True)
        bar = ttk.Frame(root, padding=12)
        bar.pack(fill="x")
        self.run_button = ttk.Button(bar, text="Run", command=self.start)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="Stop acquisition", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.folder_button = ttk.Button(bar, text="Open data folder", command=self.open_folder, state="disabled")
        self.folder_button.pack(side="left")
        self.plot_button = ttk.Button(bar, text="Open plot", command=self.open_plot, state="disabled")
        self.plot_button.pack(side="left", padx=8)
        self.status = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status).pack(side="right")
        self.vars = {}
        self.current_tool = tool
        self.build_form()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(100, self.poll)

    def values(self):
        return {key: var.get() for key, var in self.vars.items()}

    def scroll_form(self, event):
        # Do not scroll the form when the pointer is over the independent log panel.
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget and (str(widget).startswith(str(self.form)) or widget == self.canvas):
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def remember(self):
        self.settings[self.current_tool] = self.values()
        try:
            temporary = SETTINGS.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
            temporary.replace(SETTINGS)
        except OSError as exc:
            self.write(f"Could not save GUI settings: {exc}\n")

    def change_tool(self, _event=None):
        self.remember()
        self.current_tool = self.tool.get()
        self.build_form()

    def build_form(self):
        for child in self.form.winfo_children():
            child.destroy()
        self.vars = {}
        self.inputs = []
        self.live_inputs = []
        if self.current_tool == "Laser clock + EOM":
            self.livebar.pack(fill="x", padx=12, pady=(0, 8), before=self.panes)
        else:
            self.livebar.pack_forget()
        self.help.configure(text=HELP[self.current_tool])
        saved = self.settings.get(self.current_tool, {})
        if not isinstance(saved, dict):
            saved = {}
        for row, (key, label, initial, kind) in enumerate(SPECS[self.current_tool]):
            value = str(saved.get(key, initial))
            if isinstance(kind, list) and value not in kind:
                value = initial
            var = self.vars[key] = tk.StringVar(value=value)
            ttk.Label(self.form, text=label, wraplength=510).grid(row=row*2, column=0, columnspan=2, sticky="w", pady=(7, 2))
            if isinstance(kind, list):
                field = ttk.Combobox(self.form, textvariable=var, values=kind, state="readonly")
            else:
                field = ttk.Entry(self.form, textvariable=var)
            field.grid(row=row*2+1, column=0, sticky="ew")
            self.inputs.append((field, "readonly" if isinstance(kind, list) else "normal"))
            if key in ("frequency-hz", "high-ns", "eom1-v", "eom2-v"):
                self.live_inputs.append(field)
            if kind in ("file", "directory"):
                button = ttk.Button(self.form, text="Browse…", command=lambda v=var, k=kind: self.browse(v, k))
                button.grid(row=row*2+1, column=1, padx=(5, 0))
                self.inputs.append((button, "normal"))
        self.form.columnconfigure(0, weight=1)
        self.canvas.yview_moveto(0)

    def browse(self, var, kind):
        path = filedialog.askdirectory(parent=self.root) if kind == "directory" else filedialog.askopenfilename(parent=self.root)
        if path:
            var.set(path)

    def write(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        # Keep long acquisitions from accumulating an unbounded GUI log.
        if int(self.log.index("end-1c").split(".")[0]) > 3000:
            self.log.delete("1.0", "1000.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self):
        if self.running:
            return
        tool, values = self.current_tool, self.values()
        try:
            arguments(tool, values)
            if tool == "G2 acquisition":
                acquisition_config(values)
        except (ValueError, KeyError) as exc:
            self.write(f"Settings: {exc}\n")
            self.status.set("Check settings")
            return
        self.remember()
        self.running = True
        self.stop_event.clear()
        self.commands = queue.Queue(maxsize=1)
        self.last_folder = self.last_plot = None
        self.run_button.configure(state="disabled")
        self.selector.configure(state="disabled")
        for widget, _state in self.inputs:
            widget.configure(state="disabled")
        if tool == "Laser clock + EOM" and values.get("action") == "Output clock + EOM":
            for widget in self.live_inputs:
                widget.configure(state="normal")
            self.apply_eom_button.configure(state="normal")
            self.apply_clock_button.configure(state="normal")
        self.folder_button.configure(state="disabled")
        self.plot_button.configure(state="disabled")
        can_stop = values.get("action") in ("Record", "Output clock", "Output clock + EOM")
        self.stop_button.configure(state="normal" if can_stop else "disabled")
        self.status.set("Running…")
        self.write(f"\n--- {tool}: {values.get('action', 'Analyze')} ---\n")

        def worker():
            writer = QueueWriter(self.events)
            with redirect_stdout(writer), redirect_stderr(writer):
                try:
                    result = execute(tool, values, self.stop_event, commands=self.commands)
                except KeyboardInterrupt:
                    result = 130
                    print("Stopped. Any partial acquisition is marked incomplete.")
                except SystemExit as exc:
                    result = exc.code or 0
                except Exception:
                    result = 1
                    traceback.print_exc()
            self.events.put(("done", result))
        threading.Thread(target=worker, name="pulsed-acquisition", daemon=False).start()

    def apply_live(self, kind):
        if not self.running or self.stop_event.is_set() or self.current_tool != "Laser clock + EOM":
            return
        try:
            if kind == "eom":
                from laser_clock_eom import eom_plan
                plan = eom_plan(self.vars["eom1-v"].get(), self.vars["eom2-v"].get())
            else:
                from laser_clock import clock_plan
                plan = clock_plan(float(self.vars["frequency-hz"].get()), float(self.vars["high-ns"].get()))
            self.commands.put_nowait((kind, plan))
            self.remember()
            self.write(f"Queued {kind} adjustment. Waiting for hardware acknowledgement.\n")
        except (ValueError, queue.Full) as exc:
            self.write(f"Adjustment not queued: {exc or 'previous adjustment is still pending'}\n")

    def stop(self):
        self.stop_event.set()
        self.apply_eom_button.configure(state="disabled")
        self.apply_clock_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status.set("Stopping…")
        self.write("Stop requested. Hardware stops after the current driver call; plotting already in progress may finish.\n")

    def poll(self):
        self.poll_id = None
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.write(value)
                    # Backends print these absolute paths after flushing/saving artifacts.
                    for line in value.splitlines():
                        for prefix in ("Acquisition record: ", "Run record: ", "Preview image: ", "Lifetime plot: "):
                            if line.startswith(prefix):
                                path = Path(line[len(prefix):])
                                if path.exists():
                                    self.last_folder = path.parent
                                    if path.suffix.lower() == ".png":
                                        self.last_plot = path
                else:
                    self.running = False
                    self.apply_eom_button.configure(state="disabled")
                    self.apply_clock_button.configure(state="disabled")
                    self.status.set("Complete" if value == 0 else "Stopped" if value == 130 else "Error — see log")
                    self.run_button.configure(state="normal")
                    self.selector.configure(state="readonly")
                    for widget, state in self.inputs:
                        widget.configure(state=state)
                    self.stop_button.configure(state="disabled")
                    self.folder_button.configure(state="normal" if self.last_folder else "disabled")
                    self.plot_button.configure(state="normal" if self.last_plot else "disabled")
                    if value == 0 and self.last_plot and not self.closing:
                        self.open_plot()
        except queue.Empty:
            pass
        if self.closing and not self.running:
            self.destroy()
        else:
            self.poll_id = self.root.after(100, self.poll)

    def open_folder(self):
        if self.last_folder:
            os.startfile(str(self.last_folder))

    def open_plot(self):
        if not self.last_plot:
            return
        try:
            from PIL import Image, ImageTk
            window = tk.Toplevel(self.root)
            window.title(str(self.last_plot))
            with Image.open(self.last_plot) as original:
                picture = original.copy()
            picture.thumbnail((min(1100, self.root.winfo_screenwidth()-100),
                               min(850, self.root.winfo_screenheight()-150)))
            photo = ImageTk.PhotoImage(picture, master=window)
            label = ttk.Label(window, image=photo)
            label.image = photo
            label.pack()
        except Exception as exc:
            self.write(f"Plot is saved at {self.last_plot}, but display failed: {exc}\n")

    def close(self):
        self.remember()
        if self.running:
            self.closing = True
            self.stop()
        else:
            self.destroy()

    def destroy(self):
        if self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None
        self.root.destroy()


def launch(tool="CH1 lifetime"):
    root = tk.Tk()
    App(root, tool)
    root.mainloop()


if __name__ == "__main__":
    launch()
