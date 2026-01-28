import sympy as sp
import csv
import numpy as np
import matplotlib.pyplot as plt

class TempCompAlgorithm:
    def __init__(self, coefficient_file, T_start, fT_start, fM_start, mat_dens=2700, sens_area=32.0E-6):
        # Load calibration parameters from the provided file
        with open(coefficient_file, mode='r') as file:
            reader = csv.DictReader(file)
            params = {row['Name']: float(row['value']) for row in reader}
        
        self.fT_0 = params['fT_0'] #(fT_start * 2)/28.5 
        self.fM_0 = params['fM_0'] #(fM_start * 2)/28.5
        self.fT_1 = params['fT_1']
        self.fT_2 = params['fT_2']
        self.fT_3 = params['fT_3']
        self.fM_1 = params['fM_1']
        self.fM_2 = params['fM_2']
        self.fM_3 = params['fM_3']
        
        # values from the starting measurement
        self.T_start = T_start
        self.fT_start = fT_start    
        self.fM_start = fM_start
        
        self.mat_dens = mat_dens # kg/m^3  // Material density
        self.sens_area = sens_area  # m^2  // Sensor area
        
        # Coefficients for the cubic equation a*T_dif^3 + b*T_dif^2 + c*T_dif + d = 0
        # with d calculated later
        self.a = (self.fM_3*self.fT_0 - self.fT_3*self.fM_0)
        self.b = (self.fM_2*self.fT_0 - self.fT_2*self.fM_0)
        self.c = (self.fM_1*self.fT_0 - self.fT_1*self.fM_0) 
        
        
         
    def FreqToTemp(self, fT, fM):

        
        # Calculate the differences from the starting values 
        fT_dif = fT - self.fT_start
        fM_dif = fM - self.fM_start

        # Calculate the 'd' components for the temperature and mass modes
        fT_d = self.fT_start - self.fT_3 * self.T_start**3 - self.fT_2 * self.T_start**2 - self.fT_1 * self.T_start 
        fM_d = self.fM_start - self.fM_3 * self.T_start**3 - self.fM_2 * self.T_start**2 - self.fM_1 * self.T_start
        
        # d coefficient for the cubic equation a*T_dif^3 + b*T_dif^2 + c*T_dif + d = 0
        d = self.fM_0*(fT_dif-fT_d) - self.fT_0*(fM_dif - fM_d)
        
        # calculate the roots of the cubic equation
        roots = np.roots([self.a, self.b, self.c, d])
        T_dif = roots[np.isclose(roots.imag, 0)].real  # Select only the real root(s)

        # Calculate the compensated mass change using the found temperature difference
        M_dif = -(-fM_dif + (self.fM_3 * (T_dif[0])**3 + self.fM_2 * (T_dif[0])**2 + self.fM_1 * (T_dif[0])) - (self.fM_3 * (self.T_start)**3 + self.fM_2 * (self.T_start)**2 + self.fM_1 * (self.T_start)))/ self.fM_0


        uncompensated_thickness_nm = (fM_dif / self.fM_0)*1000/(self.mat_dens * self.sens_area)
        compensated_thickness_nm = (M_dif*1000)/(self.mat_dens * self.sens_area)

        compensated_m_freq = 0

        return T_dif[0], uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq
        
        
    
def calculateCoeffecients(calibration_file, coeffecient_file):
    
    with open(calibration_file, mode='r') as file:
        reader = csv.DictReader(file)
        calibration_data = [row for row in reader]
        
    with open(coeffecient_file, mode='r') as file:
        reader = csv.DictReader(file)
        old_coeffecients = {row['Name']: float(row['value']) for row in reader}
        
        
    mass_coef = np.polyfit(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_M']) for row in calibration_data],
        deg = 3
    )
    
    temp_coef = np.polyfit(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_T']) for row in calibration_data],
        deg = 3
    )
        
    
    fig, (ax1, ax2) = plt.subplots(2,1, sharex=True)
    x = np.linspace(20,100,100)
    
    ### MASS MODE
    ax1.set_title("Mass mode")
    ax1.set_xlabel("Temperature [C]")
    ax1.set_ylabel("Delta Frequency [Hz]")
    
    y = mass_coef[0]*x**3 + mass_coef[1]*x**2 +mass_coef[2]*x + mass_coef[3]
    ax1.plot(x,y, color='black', linestyle='solid')  ### plot polynomial
    
    y = old_coeffecients['fM_3']*x**3 + old_coeffecients['fM_2']*x**2 + old_coeffecients['fM_1']*x + old_coeffecients['fM_0']
    ax1.plot(x,y, color='grey', linestyle='dashdot')   ### plot old polynomial
    
    ax1.scatter(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_M']) for row in calibration_data],
        color = 'r',
        marker = 'x'
    )
    
                                                    
    
    
    ### TEMP MODE
    ax2.set_title("Temp mode")
    ax2.set_xlabel("Temperature [C]")
    ax2.set_ylabel("Delta Frequency [Hz]")
    
    y = temp_coef[0]*x**3 + temp_coef[1]*x**2 + temp_coef[2]*x + temp_coef[3]
    ax2.plot(x,y, color='black', linestyle='solid')   ### plot polynomial
    
    y = old_coeffecients['fT_3']*x**3 + old_coeffecients['fT_2']*x**2 + old_coeffecients['fT_1']*x + old_coeffecients['fT_0']
    ax2.plot(x,y, color='grey', linestyle='dashdot')   ### plot old polynomial
    
    ax2.scatter(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_T']) for row in calibration_data],
        color = 'r',
        marker = 'x'
    )
           
    fig.tight_layout() # adjust graph spacing
    plt.show()
    
    print("\n")
    print(f"fM_0: {mass_coef[3]}")
    print(f"fM_1: {mass_coef[2]}")
    print(f"fM_2: {mass_coef[1]}")
    print(f"fM_3: {mass_coef[0]}")
    print(f"fT_0: {temp_coef[3]}")
    print(f"fT_1: {temp_coef[2]}")
    print(f"fT_2: {temp_coef[1]}")
    print(f"fT_3: {temp_coef[0]}")
    
    # confirm overwrite
    if(input("Do you want to overwrite 'coeffecients.csv' (y/n): ") != 'y'):
        print("aborted")
        return
    
    with open(coeffecient_file, mode='w') as file:
        file.write("Name,value\n")
        file.write(f"fM_0,{mass_coef[3]}\n")
        file.write(f"fM_1,{mass_coef[2]}\n")
        file.write(f"fM_2,{mass_coef[1]}\n")
        file.write(f"fM_3,{mass_coef[0]}\n")
        file.write(f"fT_0,{temp_coef[3]}\n")
        file.write(f"fT_1,{temp_coef[2]}\n")
        file.write(f"fT_2,{temp_coef[1]}\n")
        file.write(f"fT_3,{temp_coef[0]}\n")

    
        
        
        
        
        
        
