import TempCompAlgorithm as tca
import csv

test_value_index = 5  # Change this index to test different entries

with open('calibration_data.csv', mode='r') as file:
    reader = csv.DictReader(file)
    calibration_data = [row for row in reader]

start_values = calibration_data[len(calibration_data)-1]

print(f"There are {len(calibration_data)} entries in the calibration data.")

temp_comp = tca.TempCompAlgorithm(
    coefficient_file = "coeffecients.csv",
    T_start= float(start_values['Temp']), 
    fT_start= float(start_values['Freq_T']),
    fM_start= float(start_values['Freq_M'])
)

temp, uncomp_th, comp_th, comp_Fm = temp_comp.FreqToTemp(
    fT = float(calibration_data[test_value_index]['Freq_T']),
    fM = float(calibration_data[test_value_index]['Freq_M'])
)

print(f"Test index: {test_value_index}")
print(f"Input Temp: {calibration_data[test_value_index]['Temp']} C")
print(f"Input Freq_T: {calibration_data[test_value_index]['Freq_T']} Hz")
print(f"Input Freq_M: {calibration_data[test_value_index]['Freq_M']} Hz\n")
print(f"Calculated Temp : {temp} C")
print(f"Uncompensated Thickness Change: {uncomp_th} nm")
print(f"Compensated Thickness Change: {comp_th} nm")
