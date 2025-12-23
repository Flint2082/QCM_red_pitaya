import sympy as sp
import csv

class TempCompAlgorithm:
    def __init__(self, parameter_file, T_start, fT_start, fM_start, mat_dens=2700, sens_area=32.0E-6):
        # Load calibration parameters from the provided file
        with open(parameter_file, mode='r') as file:
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
        
         
    def FreqToTemp(self, fT, fM):

        
        # Calculate the differences from the starting values 
        fT_dif = fT - self.fT_start
        fM_dif = fM - self.fM_start

        # Calculate the 'd' components for the temperature and mass modes
        fT_d = self.fT_start - self.fT_3 * self.T_start**3 - self.fT_2 * self.T_start**2 - self.fT_1 * self.T_start 
        fM_d = self.fM_start - self.fM_3 * self.T_start**3 - self.fM_2 * self.T_start**2 - self.fM_1 * self.T_start
        
        dT= sp.symbols('dT')      
        T_dif = sp.solve((self.fM_3*self.fT_0 - self.fT_3*self.fM_0) * (dT + self.T_start)**3 + (self.fM_2*self.fT_0 - self.fT_2*self.fM_0) * (dT + self.T_start)**2 + (self.fM_1*self.fT_0 - self.fT_1*self.fM_0) * (dT + self.T_start) + self.fM_0*(fT_dif-fT_d) - self.fT_0*(fM_dif - fM_d), dT)
        M_dif = -(-fM_dif + (self.fM_3 * (T_dif[0] + self.T_start)**3 + self.fM_2 * (T_dif[0] + self.T_start)**2 + self.fM_1 * (T_dif[0] + self.T_start)) - (self.fM_3 * (self.T_start)**3 + self.fM_2 * (self.T_start)**2 + self.fM_1 * (self.T_start)))/ self.fM_0

        T = self.T_start+T_dif[0]
        uncompensated_thickness_nm = (fM_dif / self.fM_0)*1000/(self.mat_dens * self.sens_area)
        compensated_thickness_nm = (M_dif*1000)/(self.mat_dens * self.sens_area)
        
        return T, T_dif[0], uncompensated_thickness_nm, compensated_thickness_nm
    
    def calibrate(self, parameter_file):
        # Placeholder for calibration method if needed
        pass
        


        
