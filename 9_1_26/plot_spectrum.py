import pandas as pd
import matplotlib.pyplot as plt

# File to reference
filename = "END_OF_EXPERIMENT_Spectrum_X42.9_Y58.6_ND150_Pow167_Int10_S7_NoF.csv"

try:
    # Read the CSV file
    df = pd.read_csv(filename, sep=None, engine='python')
    
    # Extract the first two column names
    x_col = df.columns[0]
    y_col = df.columns[1]
    
    # Create the plot
    plt.figure(figsize=(10, 6))
    plt.plot(df[x_col], df[y_col], color='b', label='Spectrum Data')
    
    # Formatting
    plt.title(f"{filename}")
    plt.xlabel('Wavelength (nm)')
    plt.ylabel('Intensity (A.U.)')
    plt.grid(True, which='both', linestyle='--', alpha=0.7)
    plt.legend()
    plt.tight_layout()
    
    # Display the plot
    plt.show()

except FileNotFoundError:
    print(f"Error: Ensure '{filename}' is in the same directory as this script.")
except IndexError:
    print("Error: The CSV file must contain at least two columns.")
except Exception as e:
    print(f"An unexpected error occurred: {e}")