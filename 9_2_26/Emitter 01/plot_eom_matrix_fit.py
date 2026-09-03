import numpy as np
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
from mpl_toolkits.mplot3d import Axes3D

# 1. Define the fitted interference model
def new_eom_model(x, y, A, k1, k2, phi, C):
    return A * (np.sin(k1 * x + k2 * y + phi)**2) + C

# 2. Input the provided optimal fit parameters
params0 = [6.33512563e+03, 7.98555400e-03, 7.01334000e-03, 4.34042037e-01, 1.41744928e+03]
params1 = [-4.88362559e+03,  8.03226600e-03,  6.99513100e-03,  4.32605366e-01, 6.10748530e+03]

# 3. Generate a LOWER-resolution meshgrid (30x30 instead of 100x100)
X, Y = np.meshgrid(np.linspace(-200, 200, 30), np.linspace(-200, 200, 30))

# 4. Calculate the corresponding Z (Rate) values
Z0_fit = new_eom_model(X, Y, *params0)
Z1_fit = new_eom_model(X, Y, *params1)

# 5. Initialize the 3D plot
fig = plt.figure(figsize=(10, 8))
ax = fig.add_subplot(111, projection='3d')

# 6. Plot using plot_wireframe instead of plot_surface for lighter rendering
ax.plot_wireframe(X, Y, Z0_fit, color='cyan', alpha=0.7)
ax.plot_wireframe(X, Y, Z1_fit, color='orange', alpha=0.7)

# 7. Create proxy artists for the legend (using mlines for wireframes)
proxy0 = mlines.Line2D([], [], color='cyan', label='Trace 0 Fit')
proxy1 = mlines.Line2D([], [], color='orange', label='Trace 1 Fit')
ax.legend(handles=[proxy0, proxy1])

# 8. Format the axes and title
ax.set_xlabel('EOM1 Voltage (V)')
ax.set_ylabel('EOM2 Voltage (V)')
ax.set_zlabel('Detector Rate (Hz)')
ax.set_title('Overlaid Fitted Wireframes (Low Compute)')

# 9. Display the plot
plt.tight_layout()
plt.show()