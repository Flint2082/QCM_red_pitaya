import pandas as pd
import matplotlib.pyplot as plt
import sys
from pathlib import Path

path_root = Path(__file__).parent.parent
sys.path.append(str(path_root))

import src.QCM_package.TempCompAlgorithm as tca

# File containing the data
file_path = "data\\QCM_opdamptest_12_3_2026.trace.txt"

# Read the data (whitespace separated)
df = pd.read_csv(file_path, delimiter = " ")
# Convert timestamp from milliseconds to seconds
time_s = df["Timestamp(ms)"] / 1000.0

calibration_factor = (df["GVL_OPCUA.out.deposited_thickness"].iloc[-100:] / df["GVL_QCM.READ.UncompensatedThickness"].iloc[-100:]).mean()


# Extract temperature
temperature = df["GVL_QCM.READ.Temperature"]
reference_sensor_thickness = df["GVL_OPCUA.out.deposited_thickness"]
uncompensated_thickness = calibration_factor * df["GVL_QCM.READ.UncompensatedThickness"]
compensated_thickness = calibration_factor * df["GVL_QCM.READ.CompensatedThickness"]
error = uncompensated_thickness - compensated_thickness

# Plot with separate y-axes for thickness and temperature
fig, ax1 = plt.subplots()
# ax1.plot(time_s, reference_sensor_thickness, label="Reference Sensor Thickness")
line_uncomp, = ax1.plot(time_s, uncompensated_thickness, label="Uncompensated Thickness")
line_comp, = ax1.plot(time_s, compensated_thickness, label="Compensated Thickness")
# ax1.plot(time_s, error, label="Error")

ax2 = ax1.twinx()
line_temp, = ax2.plot(time_s, temperature, label="Temperature (°C)", linestyle='--', color='tab:red')

ax1.set_xlabel("Time (s)")
ax1.set_ylabel("Thickness (nm)")
ax2.set_ylabel("Temperature (°C)")
ax1.set_title("Thickness vs Time")

ax1.grid(True)

# Combine legends from both axes
lines = [line_uncomp, line_comp, line_temp]
labels = [line.get_label() for line in lines]
ax1.legend(lines, labels)

plt.show()