import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from scipy.optimize import curve_fit

# 1. Define the measurement logic arrays
v_sweep = np.linspace(-200, 200, 60)
signalarray0 = np.tile(v_sweep, 60)
signalarray1 = np.repeat(v_sweep, 60)

# 2. Load the detector trace data
df = pd.read_csv("C:\\Users\\nanometa\\Documents\\QKD_Code\\9_2_26\\Emitter 01\\20260902_213040_Detector_Traces.csv")
z0 = df['Rate0_Hz'].values
z1 = df['Rate1_Hz'].values

# 3. Define the updated 2D interference fitting function
def new_eom_model(xy, A, k1, k2, phi, C):
    x, y = xy
    return A * (np.sin(k1 * x + k2 * y + phi)**2) + C

# 4. Execute the curve fitting for both traces
# Initial parameter guesses: [Amplitude, k1, k2, phi, Offset]
p0_guess0 = [np.max(z0) - np.min(z0), 0.01, 0.01, 0, np.min(z0)]
popt0, pcov0 = curve_fit(new_eom_model, (signalarray0, signalarray1), z0, p0=p0_guess0)

p0_guess1 = [np.max(z1) - np.min(z1), 0.01, 0.01, 0, np.min(z1)]
try:
    popt1, pcov1 = curve_fit(new_eom_model, (signalarray0, signalarray1), z1, p0=p0_guess1)
except RuntimeError:
    # Fallback guess if optimization fails to converge
    popt1, pcov1 = curve_fit(new_eom_model, (signalarray0, signalarray1), z1, p0=[np.max(z1) - np.min(z1), 0.01, 0.01, 0, np.min(z1)])

print(f"Trace 0 Fit Parameters (A, k1, k2, phi, C): {np.round(popt0, 9)}")
print(f"Trace 1 Fit Parameters (A, k1, k2, phi, C): {np.round(popt1, 9)}")

# 5. Generate a high-resolution meshgrid for smooth surface plotting
X, Y = np.meshgrid(np.linspace(-200, 200, 100), np.linspace(-200, 200, 100))
Z0_fit = new_eom_model((X, Y), *popt0)
Z1_fit = new_eom_model((X, Y), *popt1)

# 6. Initialize the 3D plot with two subplots
fig = plt.figure(figsize=(14, 6))

# Subplot 1: Trace 0
ax1 = fig.add_subplot(121, projection='3d')
ax1.scatter(signalarray0, signalarray1, z0, c='blue', s=5, alpha=0.3, label='Raw Trace 0')
ax1.plot_surface(X, Y, Z0_fit, color='cyan', alpha=0.5, edgecolor='none')
ax1.set_xlabel('EOM1 Voltage (V)')
ax1.set_ylabel('EOM2 Voltage (V)')
ax1.set_zlabel('Detector Rate (Hz)')
ax1.set_title('Trace 0 & Sum Fit')
ax1.legend()

# Subplot 2: Trace 1
ax2 = fig.add_subplot(122, projection='3d')
ax2.scatter(signalarray0, signalarray1, z1, c='red', s=5, alpha=0.3, label='Raw Trace 1')
ax2.plot_surface(X, Y, Z1_fit, color='orange', alpha=0.5, edgecolor='none')
ax2.set_xlabel('EOM1 Voltage (V)')
ax2.set_ylabel('EOM2 Voltage (V)')
ax2.set_zlabel('Detector Rate (Hz)')
ax2.set_title('Trace 1 & Sum Fit')
ax2.legend()

plt.tight_layout()
plt.show()