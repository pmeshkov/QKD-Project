import pandas as pd
import matplotlib.pyplot as plt
import sys
import os

# Update this to match your most recent sweep file
print(os.listdir())
filename = "C:\\Users\\nanometa\\Documents\\QKD_Code\\9_1_26\\Vpi_Sweep_20260901_183523.csv"

# Check if file exists before attempting to load
if not os.path.exists(filename):
    print(f"Error: Could not find '{filename}'.")
    print("Please update the 'filename' variable with your exact CSV name.")
    sys.exit(1)

# Load the data
df = pd.read_csv(filename)

# Extract columns
voltage = df['Target_Voltage_V']
rate_pfi1 = df['PFI1_Rate_Hz']
rate_pfi2 = df['PFI2_Rate_Hz']

# Initialize plot
plt.figure(figsize=(10, 6))

# Plot both channels
# Using markers to show the actual integration points from your 1V steps
plt.plot(voltage, rate_pfi1, marker='o', markersize=4, linestyle='-', linewidth=1.5, label='PFI1 Rate')
plt.plot(voltage, rate_pfi2, marker='s', markersize=4, linestyle='-', linewidth=1.5, label='PFI2 Rate')

# Formatting
plt.xlabel("Target EOM Bias (V)")
plt.ylabel("Counts Per Second (Hz)")
plt.title("EOM Interference: Photon Counts vs. Voltage")
plt.legend()
plt.grid(True, alpha=0.5, linestyle='--')

# Optimize layout and display
plt.tight_layout()
plt.show()