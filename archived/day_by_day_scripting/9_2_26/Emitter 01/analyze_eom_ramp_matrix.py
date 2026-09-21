import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

# 1. Define the measurement logic arrays
v_sweep = np.linspace(-200, 200, 60)
timearray = np.ones(3600) * 0.1
signalarray0 = np.tile(v_sweep, 60)   # Sweeps from -200 to 200 repeatedly
signalarray1 = np.repeat(v_sweep, 60) # Steps from -200 to 200 slowly

# 2. Load the detector trace data
filename = "C:\\Users\\nanometa\\Documents\\QKD_Code\\9_2_26\\Emitter 01\\20260902_213040_Detector_Traces.csv"
df = pd.read_csv(filename)

# 3. Initialize the 3D plot
fig = plt.figure(figsize=(10, 8))
ax = fig.add_subplot(111, projection='3d')

# 4. Plot both traces on the same graph with different colors
# Assuming Rate0_Hz and Rate1_Hz represent the respective traces
ax.scatter(signalarray0, signalarray1, df['Rate0_Hz'], c='blue', label='Trace 0', s=5, alpha=0.7)
ax.scatter(signalarray0, signalarray1, df['Rate1_Hz'], c='red', label='Trace 1', s=5, alpha=0.7)

# 5. Format axes and legend
ax.set_xlabel('EOM1 Voltage (V)')
ax.set_ylabel('EOM2 Voltage (V)')
ax.set_zlabel('Detector Rate (Hz)')
ax.set_title('3D Map of Detector Traces vs EOM Voltages')
ax.legend()

# 6. Display the plot
plt.show()