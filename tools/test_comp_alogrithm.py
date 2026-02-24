from pathlib import Path
import csv
import matplotlib.pyplot as plt
import sys

path_root = Path(__file__).parent.parent
sys.path.append(str(path_root))

import src.QCM_package.TempCompAlgorithm as tca




with open('data/calibration_data.csv', mode='r') as file:
    reader = csv.DictReader(file)
    calibration_data = [row for row in reader]

start_values = calibration_data[len(calibration_data)-1]

print(f"There are {len(calibration_data)} entries in the calibration data.")

temp_comp = tca.TempCompAlgorithm(
    coefficient_file = "data/coeffecients.csv",
    T_start= float(start_values['Temp']), 
    fT_start= float(start_values['Freq_T']),
    fM_start= float(start_values['Freq_M'])
)

# Lists to store data for plotting
temperatures = []
compensated_thicknesses = []

for i in range(len(calibration_data)):
    temp, uncomp_th, comp_th, comp_Fm = temp_comp.FreqToTemp(
            fT = float(calibration_data[i]['Freq_T']),
            fM = float(calibration_data[i]['Freq_M'])
        )

    # Store data for plotting
    temperatures.append(temp)
    compensated_thicknesses.append(comp_th)

    print(f"Test index: {i}")
    print(f"Input Temp: {calibration_data[i]['Temp']} C")
    print(f"Input Freq_T: {calibration_data[i]['Freq_T']} Hz")
    print(f"Input Freq_M: {calibration_data[i]['Freq_M']} Hz\n")
    print(f"Calculated Temp : {temp} C")
    print(f"Uncompensated Thickness Change: {uncomp_th} nm")
    print(f"Compensated Thickness Change: {comp_th} nm")

# Plot compensated thickness vs temperature
plt.figure(figsize=(10, 6))
plt.plot(temperatures, compensated_thicknesses, 'b-o', linewidth=2, markersize=6)
plt.xlabel('Temperature (°C)', fontsize=12)
plt.ylabel('Compensated Thickness Change (nm)', fontsize=12)
plt.title('Compensated Thickness vs Temperature', fontsize=14)
plt.grid(True, alpha=0.3)
plt.tight_layout()
plt.show()
    
    
