import pandas as pd
import plotly.graph_objects as go
import glob
import re
import sys
import os

# 1. Locate all matching files in the current directory
file_pattern = "Vpi_Sweep_LP_*_deg.csv"
files = glob.glob(file_pattern)

if not files:
    print(f"Error: Could not find any files matching '{file_pattern}'.")
    sys.exit(1)

# 2. Extract angles and sort the files
data_files = []
for f in files:
    # Regex to capture integers or decimals in the filename
    match = re.search(r'Vpi_Sweep_LP_(\d+(?:\.\d+)?)_deg\.csv', f)
    if match:
        angle = float(match.group(1))
        data_files.append((angle, f))

if not data_files:
    print("Error: Files found, but could not parse degrees from the titles.")
    sys.exit(1)

# Sort the list of tuples based on the extracted angle
data_files.sort(key=lambda x: x[0])

# 3. Initialize the interactive 3D plot
fig = go.Figure()

colors_pfi1 = '#1f77b4' # Muted blue
colors_pfi2 = '#ff7f0e' # Safety orange

# We only want the legend to show 'PFI1' and 'PFI2' once, not for every sweep
show_legend = True

for angle, f in data_files:
    df = pd.read_csv(f)
    
    # Extract columns
    voltage = df['Target_Voltage_V']
    rate_pfi1 = df['PFI1_Rate_Hz']
    rate_pfi2 = df['PFI2_Rate_Hz']
    
    # Create an array of the current angle to match the voltage array's length
    angle_arr = [angle] * len(voltage)
    
    # Add PFI1 Trace
    fig.add_trace(go.Scatter3d(
        x=angle_arr,
        y=voltage,
        z=rate_pfi1,
        mode='lines+markers',
        marker=dict(size=3, color=colors_pfi1),
        line=dict(color=colors_pfi1, width=3),
        name='PFI1 Rate',
        legendgroup='PFI1', # Groups all PFI1 traces together in the legend toggle
        showlegend=show_legend
    ))
    
    # Add PFI2 Trace
    fig.add_trace(go.Scatter3d(
        x=angle_arr,
        y=voltage,
        z=rate_pfi2,
        mode='lines+markers',
        marker=dict(size=3, color=colors_pfi2),
        line=dict(color=colors_pfi2, width=3),
        name='PFI2 Rate',
        legendgroup='PFI2', # Groups all PFI2 traces together in the legend toggle
        showlegend=show_legend
    ))
    
    show_legend = False # Disable legend entry for subsequent iterations

# 4. Format the 3D layout
fig.update_layout(
    title="EOM Interference: Photon Counts vs. Voltage and Angle",
    scene=dict(
        xaxis_title='Linear Polarizer Angle (Degrees)',
        yaxis_title='Target EOM Bias (V)',
        zaxis_title='Counts Per Second (Hz)',
        camera=dict(
            eye=dict(x=1.5, y=1.5, z=1.2) # Default viewing angle
        )
    ),
    width=1200,
    height=800,
    margin=dict(l=10, r=10, b=10, t=50)
)

# 5. Render the plot in your default web browser
fig.show()