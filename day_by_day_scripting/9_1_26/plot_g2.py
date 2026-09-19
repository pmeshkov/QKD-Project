import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

filename = "END_OF_EXPERIMENT_g2_X42.9_Y58.6_ND150_Pow166_Filter550LP_Int20_TAC50_Gain1_Bin1024_S7_NoF.csv"

try:
    # Auto-detect delimiter, do not expect headers
    df = pd.read_csv(filename, sep=None, engine='python', header=None)

    # Handle whether the hardware exported a single row (many columns) or single column (many rows)
    if len(df) == 1 or len(df.columns) > 100:
        counts = df.iloc[0].values
    else:
        counts = df.iloc[:, 0].values

    # Force conversion to numeric, coercing any stray text/metadata to NaN
    counts = pd.to_numeric(counts, errors='coerce')

    # Drop the NaN values to clean the array
    counts = counts[~np.isnan(counts)]

    # Generate a centered x-axis assuming the center bin corresponds to zero delay
    total_bins = len(counts)
    bins = np.arange(total_bins) - (total_bins // 2)

    # Create the plot
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(bins, counts, color='blue', linewidth=1.2, label='Measured Coincidences')

    # Add a vertical dashed line at zero delay
    ax.axvline(x=0, color='red', linestyle='--', alpha=0.7, label='Zero Delay (Center Bin)')

    # Formatting
    ax.set_title(f"Second-Order Correlation: {filename}")
    ax.set_xlabel("Delay (Relative Bin Number)")
    ax.set_ylabel("Coincidences")
    ax.grid(True, which='both', linestyle=':', alpha=0.6)
    ax.legend(loc='upper right')

    plt.tight_layout()
    plt.show()

except FileNotFoundError:
    print(f"Error: Could not find '{filename}'.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")