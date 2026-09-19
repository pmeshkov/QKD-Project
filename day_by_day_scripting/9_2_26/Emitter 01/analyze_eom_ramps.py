import pandas as pd
import matplotlib.pyplot as plt

def plot_detector_traces():
    # File paths
    trace_file = "C:\\Users\\nanometa\\Documents\\QKD_Code\\9_2_26\\Emitter 01\\20260902_211816_Detector_Traces.csv"

    # Read the detector traces
    df = pd.read_csv(trace_file)

    # Separate the sweeps based on which EOM is held constant near 0V
    # This assumes the non-sweeping EOM is held at 0.0V while the other sweeps
    sweep0_mask = df['EOM1_Target_V'].abs() < 1e-3
    sweep1_mask = df['EOM0_Target_V'].abs() < 1e-3

    df_sweep0 = df[sweep0_mask]
    df_sweep1 = df[sweep1_mask]

    # Initialize the figure with two subplots
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    # Subplot 1: EOM0 changing, EOM1 constant
    ax1.plot(df_sweep0['EOM0_Target_V'], df_sweep0['Rate0_Hz'], label='Detector 0', alpha=0.8, color='C0')
    ax1.plot(df_sweep0['EOM0_Target_V'], df_sweep0['Rate1_Hz'], label='Detector 1', alpha=0.8, color='C1')
    ax1.set_title('Detector Rates vs EOM0 Voltage\n(EOM1 Constant)')
    ax1.set_xlabel('EOM0 Voltage (V)')
    ax1.set_ylabel('Count Rate (Hz)')
    ax1.legend()
    ax1.grid(True, linestyle='--', alpha=0.6)

    # Subplot 2: EOM1 changing, EOM0 constant
    ax2.plot(df_sweep1['EOM1_Target_V'], df_sweep1['Rate0_Hz'], label='Detector 0', alpha=0.8, color='C0')
    ax2.plot(df_sweep1['EOM1_Target_V'], df_sweep1['Rate1_Hz'], label='Detector 1', alpha=0.8, color='C1')
    ax2.set_title('Detector Rates vs EOM1 Voltage\n(EOM0 Constant)')
    ax2.set_xlabel('EOM1 Voltage (V)')
    ax2.set_ylabel('Count Rate (Hz)')
    ax2.legend()
    ax2.grid(True, linestyle='--', alpha=0.6)

    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    plot_detector_traces()