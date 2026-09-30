import os
import matplotlib.pyplot as plt
from matplotlib.widgets import Button, Slider
import numpy as np
import pandas as pd

# 1. Define the list of CSV filenames to compare
csv_files = [
    "Detector_Traces_8_51_pm_Tint_0.05.csv",
    "Detector_Traces_9_12_pm_Tint_0.1.csv",
    "Detector_Traces_9_21_pm_Tint_0.1.csv",
    "Detector_Traces_9_30_pm_Tint_0.1.csv",
]

# 2. Pre-load and reshape datasets into memory to keep GUI updates instant
datasets = []
for file_path in csv_files:
  df = pd.read_csv(file_path)
  label = os.path.splitext(os.path.basename(file_path))[0]
  N = int(np.sqrt(len(df)))

  datasets.append({
      "label": label,
      "v_alice": np.linspace(-200, 200, N),
      "v_bob": np.linspace(-200, 200, N),
      "z0": df["Rate0_Hz"].values.reshape((N, N)),
      "z1": df["Rate1_Hz"].values.reshape((N, N)),
  })

# 3. Initialize figure and adjust layout to accommodate widgets at the bottom
fig, ax = plt.subplots(figsize=(11, 7))
plt.subplots_adjust(bottom=0.22, right=0.78)

# Initial slice at EOM0 = -200 V
initial_v0 = -200.0
for data in datasets:
  col_idx = np.argmin(np.abs(data["v_alice"] - initial_v0))

  t0_crossection = data["z0"][:, col_idx]
  t1_crossection = data["z1"][:, col_idx]

  # Normalized rates preserved
  t0_crossection = t0_crossection / np.max(t0_crossection)
  t1_crossection = t1_crossection / np.max(t1_crossection)

  (line0,) = ax.plot(
      data["v_bob"],
      t0_crossection,
      marker="o",
      markersize=3,
      label=f"{data['label']} (T0)",
  )
  (line1,) = ax.plot(
      data["v_bob"],
      t1_crossection,
      marker="s",
      markersize=3,
      linestyle="--",
      color=line0.get_color(),
      label=f"{data['label']} (T1)",
  )

  # Store line handles to enable fast in-place data updates
  data["line0"] = line0
  data["line1"] = line1

# 4. Format plot styling
ax.set_title(
    rf"Detector Cross-Sections at $\mathrm{{EOM0}} \approx {initial_v0:.1f}\ \mathrm{{V}}$",
    fontsize=13,
)
ax.set_xlabel("EOM1 Voltage (V) [BOB]", fontsize=11)
ax.set_ylabel("Normalized Detector Rate", fontsize=11)
ax.grid(True, linestyle="--", alpha=0.6)
ax.legend(bbox_to_anchor=(1.02, 1), loc="upper left", frameon=True)

# 5. Add Slider and Button UI widgets
ax_slider = fig.add_axes([0.15, 0.08, 0.50, 0.04])
slider_eom0 = Slider(
    ax=ax_slider,
    label="EOM0 Target (V) ",
    valmin=-200.0,
    valmax=200.0,
    valinit=initial_v0,
    valstep=1.0,
)

ax_button = fig.add_axes([0.68, 0.07, 0.14, 0.06])
btn_update = Button(ax_button, "Update Plot", hovercolor="0.92")


# 6. Callback executed ONLY when the button is clicked
def on_button_click(event):
  target_v0 = slider_eom0.val

  for data in datasets:
    # Locate the nearest Alice voltage column
    col_idx = np.argmin(np.abs(data["v_alice"] - target_v0))

    t0_crossection = data["z0"][:, col_idx]
    t1_crossection = data["z1"][:, col_idx]

    # Normalized rates preserved
    t0_crossection = t0_crossection / np.max(t0_crossection)
    t1_crossection = t1_crossection / np.max(t1_crossection)

    data["line0"].set_ydata(t0_crossection)
    data["line1"].set_ydata(t1_crossection)

  closest_actual_v0 = datasets[0]["v_alice"][
      np.argmin(np.abs(datasets[0]["v_alice"] - target_v0))
  ]
  ax.set_title(
      rf"Detector Cross-Sections at $\mathrm{{EOM0}} \approx {closest_actual_v0:.1f}\ \mathrm{{V}}$",
      fontsize=13,
  )
  fig.canvas.draw_idle()


# Attach callback strictly to the button, keeping the slider non-blocking
btn_update.on_clicked(on_button_click)

plt.show()