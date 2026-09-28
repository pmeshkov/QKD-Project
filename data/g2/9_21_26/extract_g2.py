import re
import numpy as np
import pandas as pd
from scipy.optimize import curve_fit

def g2_bunching_model(tau, y0, a1, tau1, a2, tau2):
    """
    Sum of two exponentials for g2(tau) fitting.
    a1 represents the antibunching dip (negative amplitude).
    a2 represents the bunching shoulder (positive amplitude).
    """
    return y0 + a1 * np.exp(-np.abs(tau) / tau1) + a2 * np.exp(-np.abs(tau) / tau2)

def analyze_g2_data(filename):
    # Extract power in uW from the filename
    power_match = re.search(r'Pow(\d+\.?\d*)', filename)
    if not power_match:
        raise ValueError("Could not extract power from filename.")
    power_uw = float(power_match.group(1))

    # Read the CSV as a single row without headers
    data = pd.read_csv(filename, header=None)
    
    # Extract the first row as the coincidence counts array
    g2_raw = data.iloc[0, :].values
    num_bins = len(g2_raw)

    # Reconstruct the tau (delay) axis using filename parameters
    # Assuming 'TAC50' indicates a 50 ns range
    tac_range_ns = 50.0 
    bin_width = tac_range_ns / num_bins
    
    # Locate the antibunching dip to define tau = 0 to account for electronic offsets
    dip_index = np.argmin(g2_raw)
    tau = (np.arange(num_bins) - dip_index) * bin_width

    # Normalize g2_raw to the baseline (far from tau=0) to fit a standard g2 function
    # Averages the first 10% and last 10% of the array as the baseline
    edge_bins = int(num_bins * 0.1)
    baseline = np.mean(np.concatenate((g2_raw[:edge_bins], g2_raw[-edge_bins:])))
    g2_normalized = g2_raw / baseline if baseline > 0 else g2_raw

    # Initial guesses: [y0, a1, tau1, a2, tau2]
    initial_guess = [1.0, -1.0, 1.0, 0.5, 10.0]
    
    bounds = (
        [0.0, -np.inf, 0.0, 0.0, 0.0], 
        [np.inf, 0.0, np.inf, np.inf, np.inf]
    )

    try:
        popt, pcov = curve_fit(
            g2_bunching_model, 
            tau, 
            g2_normalized, 
            p0=initial_guess, 
            bounds=bounds,
            maxfev=10000
        )
        y0, a1, tau1, a2, tau2 = popt
        
        g2_zero = y0 + a1 + a2
        estimated_lifetime = tau1 

    except RuntimeError:
        print("Curve fit failed to converge.")
        return None

    results = {
        "Filename": filename,
        "Power (uW)": power_uw,
        "g2(0)": g2_zero,
        "Estimated Lifetime (ns)": estimated_lifetime,
        "Fit Parameters (y0, a1, tau1, a2, tau2)": popt
    }
    
    return results


# Target file execution
data_directory = "C:\\Users\\nanometa\\Documents\\QKD_Code\\data\\g2\\9_21_26\\emitter_x_46.994_y_41.300\\"
target_file = "g2_X47.0_Y41.3_ND650_Pow381_Filter550LP_Int10_TAC50_Gain1_Bin1024.csv"
if __name__ == "__main__":
    result = analyze_g2_data(data_directory+target_file)
    if result:
        print(f"Power: {result['Power (uW)']} uW")
        print(f"g2(0): {result['g2(0)']:.4f}")
        print(f"Lifetime: {result['Estimated Lifetime (ns)']:.4f} ns")
    pass
