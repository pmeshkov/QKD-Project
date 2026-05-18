import pandas as pd
import plotly.graph_objects as go
from dash import Dash, dcc, html, Input, Output
import glob
import re
import sys

# 1. Locate and parse files into a master DataFrame
file_pattern = "Vpi_Sweep_LP_*_deg.csv"
files = glob.glob(file_pattern)

if not files:
    print(f"Error: Could not find any files matching '{file_pattern}'.")
    sys.exit(1)

data_frames = []
for f in files:
    match = re.search(r'Vpi_Sweep_LP_(\d+(?:\.\d+)?)_deg\.csv', f)
    if match:
        angle = float(match.group(1))
        df = pd.read_csv(f)
        df['Angle'] = angle
        data_frames.append(df)

if not data_frames:
    print("Error: Files found, but could not parse degrees from the titles.")
    sys.exit(1)

# Consolidate data for efficient filtering
master_df = pd.concat(data_frames, ignore_index=True)
available_angles = sorted(master_df['Angle'].unique())

# 2. Initialize the Dash App
app = Dash(__name__)

app.layout = html.Div([
    html.H2("EOM Interference Visualization", style={'fontFamily': 'Arial, sans-serif'}),
    
    # Input controls container
    html.Div([
        html.Label("Cross-section by Angle (°): ", style={'fontWeight': 'bold'}),
        dcc.Input(id='input-angle', type='number', placeholder='e.g., 45', debounce=True, style={'marginRight': '20px'}),
        
        html.Label("Cross-section by EOM Bias (V): ", style={'fontWeight': 'bold'}),
        dcc.Input(id='input-voltage', type='number', placeholder='e.g., 2.5', debounce=True, style={'marginRight': '20px'}),
        
        html.Button('Clear Inputs (Return to 3D)', id='clear-btn', n_clicks=0)
    ], style={'padding': '10px', 'backgroundColor': '#f9f9f9', 'border': '1px solid #ccc', 'marginBottom': '10px'}),
    
    # Graph container
    dcc.Graph(id='eom-plot', style={'height': '80vh'})
])

# Callback to handle clearing inputs
@app.callback(
    Output('input-angle', 'value'),
    Output('input-voltage', 'value'),
    Input('clear-btn', 'n_clicks')
)
def clear_inputs(n_clicks):
    return None, None

# Callback to update graph based on inputs
@app.callback(
    Output('eom-plot', 'figure'),
    Input('input-angle', 'value'),
    Input('input-voltage', 'value')
)
def update_graph(target_angle, target_voltage):
    df_filtered = master_df.copy()
    title_suffix = " (Full 3D View)"
    camera = dict(eye=dict(x=1.5, y=1.5, z=1.2)) # Default 3D angle

    # Filter by closest Angle if provided
    if target_angle is not None:
        closest_angle = min(available_angles, key=lambda x: abs(x - target_angle))
        df_filtered = df_filtered[df_filtered['Angle'] == closest_angle]
        title_suffix = f" (Angle Cross-section: ~{closest_angle}°)"
        camera = dict(eye=dict(x=0, y=2.0, z=0), projection=dict(type='orthographic'))
        
    # Filter by closest Voltage if provided (Overrides angle if both are typed)
    elif target_voltage is not None:
        # Find the absolute closest voltage point in the entire dataset
        idx_closest = (df_filtered['Target_Voltage_V'] - target_voltage).abs().idxmin()
        closest_v = df_filtered.loc[idx_closest, 'Target_Voltage_V']
        df_filtered = df_filtered[df_filtered['Target_Voltage_V'] == closest_v]
        title_suffix = f" (Voltage Cross-section: ~{closest_v:.3f}V)"
        camera = dict(eye=dict(x=2.0, y=0, z=0), projection=dict(type='orthographic'))

    # Build the figure
    fig = go.Figure()
    colors_pfi1, colors_pfi2 = '#1f77b4', '#ff7f0e'
    show_legend = True
    
    # Group by angle so continuous lines don't streak across the 3D space
    for angle, group in df_filtered.groupby('Angle'):
        fig.add_trace(go.Scatter3d(
            x=group['Angle'], y=group['Target_Voltage_V'], z=group['PFI1_Rate_Hz'],
            mode='lines+markers', marker=dict(size=3), line=dict(width=3),
            name='PFI1 Rate', legendgroup='PFI1', showlegend=show_legend,
            marker_color=colors_pfi1, line_color=colors_pfi1
        ))
        
        fig.add_trace(go.Scatter3d(
            x=group['Angle'], y=group['Target_Voltage_V'], z=group['PFI2_Rate_Hz'],
            mode='lines+markers', marker=dict(size=3), line=dict(width=3),
            name='PFI2 Rate', legendgroup='PFI2', showlegend=show_legend,
            marker_color=colors_pfi2, line_color=colors_pfi2
        ))
        show_legend = False

    fig.update_layout(
        title=f"EOM Interference{title_suffix}",
        scene=dict(
            xaxis_title='Linear Polarizer Angle (Degrees)',
            yaxis_title='Target EOM Bias (V)',
            zaxis_title='Counts Per Second (Hz)',
            camera=camera
        ),
        margin=dict(l=10, r=10, b=10, t=50)
    )
    
    return fig

if __name__ == '__main__':
    print("Starting Dash server. Open http://127.0.0.1:8050/ in your browser.")
    app.run(debug=True)