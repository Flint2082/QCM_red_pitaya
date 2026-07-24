import csv
import numpy as np
import matplotlib.pyplot as plt

class TempCompAlgorithm:
    def __init__(self, coefficients, T_start, fT_start, fM_start,
                 mat_dens=19320,            # kg/m^3 = Material density (e.g. gold)
                 sens_area=5.25e-5,         # m^2 = Sensor area (e.g. 5.25e-5 m^2 for 8.2 mm dia apature)
                 mass_sensitivity=-13.3e-8, # kg/m^2/Hz = Mass sensitivity, negative: added mass lowers the frequency (-13.3 ng/(cm2*Hz) for ~6 MHz AT-cut)
                 z_ratio=1.0,               # acoustic impedance ratio quartz/film (gold 0.381); 1.0 ~ plain Sauerbrey
                 freq_virgin=None,          # Hz = pristine (uncoated) crystal frequency; None/0 = use fM_start
                 tooling_ratio=1.0          # proportional scaling of the reported thickness (sensor/substrate geometry); 1.0 = no scaling
                 ):
        # Calibration coefficients are provided directly (e.g. from the active
        # crystal profile).
        # so only the fM_1..3 / fT_1..3 terms are read from the supplied dict.
        self.mass_sensitivity = mass_sensitivity        
        self.fT_0 = (fT_start/fM_start) / (mass_sensitivity * sens_area)  # Hz/kg = Temp sensitivity NOTE: this is derived from the starting frequencies and mass sensitivity, not fitted from calibration data
        self.fM_0 = 1 / ( mass_sensitivity * sens_area )                  # Hz/kg = Mass sensitivity              
        self.fT_1 = coefficients['fT_1']               
        self.fT_2 = coefficients['fT_2']
        self.fT_3 = coefficients['fT_3']
        self.fM_1 = coefficients['fM_1']
        self.fM_2 = coefficients['fM_2']
        self.fM_3 = coefficients['fM_3']
        
        # values from the starting measurement
        self.T_start = T_start
        # Last physically-selected temperature; seeds the cubic-root selection so
        # the solve tracks the real temperature continuously (see FreqToTemp).
        self._last_T = T_start
        self.fT_start = fT_start
        self.fM_start = fM_start
        
        self.mat_dens = mat_dens        # kg/m^3 = Material density
        self.sens_area = sens_area      # m^2 = Sensor area
        self.z_ratio = z_ratio          # quartz/film acoustic impedance ratio
        self.f_virgin = freq_virgin or fM_start  # Hz = unloaded reference for the Z-match conversion
        self.tooling_ratio = tooling_ratio  # proportional scaling of the reported thickness
        
        # Coefficients for the cubic equation a*T_dif^3 + b*T_dif^2 + c*T_dif + d = 0
        # with d calculated later
        self.a = (self.fM_3*self.fT_0 - self.fT_3*self.fM_0)
        self.b = (self.fM_2*self.fT_0 - self.fT_2*self.fM_0)
        self.c = (self.fM_1*self.fT_0 - self.fT_1*self.fM_0)

    def _zmatch_areal_mass(self, f_loaded):
        """Z-match (Lu-Lewis) areal mass [kg/m^2] of the film between the virgin
        crystal frequency and f_loaded. The quartz constant Nq*rho_q is expressed
        through the crystal's calibrated mass sensitivity: Nq*rho_q = |ms| * f_v^2.
        Reduces to linear Sauerbrey for small loads and Z=1 (arctan(tan(x)) = x)."""
        Nq_rho_q = abs(self.mass_sensitivity) * self.f_virgin**2  # kg*Hz/m^2
        x = np.pi * (self.f_virgin - f_loaded) / self.f_virgin
        return (Nq_rho_q / (np.pi * self.z_ratio * f_loaded)) * np.arctan(self.z_ratio * np.tan(x))

    def freq_to_thickness(self, f_from, f_to):
        """Film thickness [m] deposited while the (temperature-clean) mass-mode
        frequency moved from f_from to f_to, via the Z-match equation."""
        return (self._zmatch_areal_mass(f_to) - self._zmatch_areal_mass(f_from)) / self.mat_dens
        
        
         
    def FreqToTemp(self, fT, fM):
        try:
        
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
            real_roots = roots[np.isclose(roots.imag, 0)].real  # physical temperatures are real

            # The cubic can have up to three real roots but only one is physical.
            # np.roots returns them in no meaningful order, so pick the root closest
            # to the last known temperature (T_start on the first sample). This keeps
            # the solve on the correct, continuous branch instead of latching onto a
            # spurious root, which would corrupt the temperature and the compensated
            # thickness while leaving the uncompensated value untouched.
            if real_roots.size == 0:
                # No real solution — frequencies inconsistent with the calibration.
                # Hold the previous temperature rather than crash the running loop.
                print("FreqToTemp: no real root; holding last temperature")
                T = self._last_T
            else:
                T = float(real_roots[np.argmin(np.abs(real_roots - self._last_T))])
            self._last_T = T

            # Calculate the compensated mass change using the found temperature
            M_dif = -(-fM_dif + (self.fM_3 * T**3 + self.fM_2 * T**2 + self.fM_1 * T) - (self.fM_3 * self.T_start**3 + self.fM_2 * self.T_start**2 + self.fM_1 * self.T_start)) / self.fM_0

            compensated_m_freq = self.fM_start + self.fM_0 * M_dif  # measured freq minus the thermal shift

            # frequency -> thickness via the Z-match (Lu-Lewis) equation; the raw
            # frequency gives the uncompensated value, the temperature-clean
            # compensated frequency gives the compensated one. The tooling ratio is
            # a simple proportional correction for the sensor/substrate geometry.
            uncompensated_thickness_nm = self.freq_to_thickness(self.fM_start, fM) * 1e9 * self.tooling_ratio
            compensated_thickness_nm = self.freq_to_thickness(self.fM_start, compensated_m_freq) * 1e9 * self.tooling_ratio

            return T, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq
        except Exception as e:
            print(f"Error in FreqToTemp: {e}")
            return None, None, None, None
        
        
    
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

    
        
        
        
        
        
        
