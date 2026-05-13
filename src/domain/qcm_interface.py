### Class to interface with the QCM Red Pitaya firmware, and to run measurements and calibrations.

import os
import time
import processing.TempCompAlgorithm as tca
import calendar
from collections import deque
import numpy as np



class QCMInterface:
    def __init__(self, fpga):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        directory = os.path.join(base_dir, "..", "..", "model_composer", "qcm_rp", "outputs")
        newest_file = max(
            (os.path.join(directory, f) for f in os.listdir(directory)),
            key=os.path.getmtime
        )
        
        # constants
        self.WINDOW_SIZE = 2**14
        self.MASS_MODE = 1
        self.TEMP_MODE = 2
        
        self.coeff_file = os.path.join(base_dir, "..", "..", "data", "coeffecients.csv")
        
        self.T_start = 0
        self.fT_start = 0
        self.fM_start = 0


        self.fpga = fpga
        print("CasperFpga connected to red pitaya")

        print("Newest file", newest_file)

        try:
            self.fpga.load_register_map(newest_file)
        except Exception as e:
            print(f"Failed to upload FPGA program: {e}")
            raise

    def to_signed(self, value, bits):
        """Interpret unsigned integer as signed."""
        mask = (1 << bits) - 1
        value &= mask  # ensure it fits within the given bit width
        sign_bit = 1 << (bits - 1)
        return (value ^ sign_bit) - sign_bit

    # ===========================
    # setter methods
    # ===========================

    def setFreq(self, osc_index, freq):
        self.fpga.write_register(register_name='freq_'+str(osc_index),value=int(freq*2**6)) # multiplication to account for fixed-point (32F6) representation in FPGA

    def setInt(self, osc_index, gain):
        self.fpga.write_register(register_name='integral_'+str(osc_index),value=int(gain*2**32)) # multiplication to account for fixed-point (32F32) representation in FPGA

    def setIQGain(self, osc_index, gain):
        self.fpga.write_register(register_name='IQ_gain_'+str(osc_index),value=int(gain*2**32)) # multiplication to account for fixed-point (32F32) representation in FPGA

    def setInv(self, osc_index, inv: bool):
        self.fpga.write_register(register_name='inv_fb_'+str(osc_index),value=inv)
        
    def setOutputMode(self, mode = -1):
        if mode == -1:
            print("Output mode not set. These are the available modes:")
            print("0: The delta of the two inputs")
            print("1: The mass mode frequency (fine)")
            print("2: The mass mode frequency (coarse)")
            print("3: The mass mode multiplier output")
            print("4: The mass mode lock detector")
            print("5: The mass mode power detector")
            print("6: The temp mode frequency (fine)")
            print("7: The temp mode frequency (coarse)")
            print("8: The temp mode multiplier output")
            print("9: The temp mode lock detector")
            print("10: The temp mode power detector")
        else:
            self.fpga.write_register(register_name='output_select', value=mode)
        
    # ===========================
    # getter methods
    # ===========================

    def reset(self):
        self.fpga.write_register(register_name='reset', value=1)
        self.fpga.write_register(register_name='reset', value=0)
        
    def getFreq(self, osc_index):
        lsb = self.fpga.read_register(f'frequency_out_lsb_{osc_index}') & 0xFFFFFFFF
        msb = self.fpga.read_register(f'frequency_out_msb_{osc_index}') & 0xFFFFFFFF

        raw = (msb << 32) | lsb      # reconstruct full fixed-point integer
        freq = raw / (1 << 10)       # apply fixed-point scaling

        return freq

    def getI(self, osc_index):
        inPhase = self.to_signed(self.fpga.read_register(f'I_out_{osc_index}'),32)
        return inPhase/2**31
        
    def getQ(self, osc_index):
        quadrature = self.to_signed(self.fpga.read_register(f'Q_out_{osc_index}'),32)
        return quadrature/2**31

    def getAmpAndPhase(self, osc_index):
        I = self.getI(osc_index)
        Q = self.getQ(osc_index)
        return (I**2 + Q**2)**0.5, np.arctan2(Q, I)
    
    def getLockDetect(self, osc_index):
        amp, phase = self.getAmpAndPhase(osc_index)
        
        # Checks if the amplitude is above a certain threshold and if the phase is about 1 PI (which is expected for a properly locked loop in this configuration)
        if amp > 0.1 and round(phase) == 3:
            return True
        else:
            return False
    
    # ===========================
    # Control methods
    # ===========================
    
    def standby(self, osc_index: int):
        self.setFreq(osc_index,0)
        self.setInt(osc_index,0)
        self.reset()

    def capacitorAdjustment(self):
        self.standby(2)
        self.setFreq(1, 6000000)
        self.setInt(1, 0.00)
        self.setIQGain(1, 0.00001)

        while True:
            try:
                amplitude = self.getAmpAndPhase(1)[0]
                print(f"Amplitude: {amplitude}")
                time.sleep(0.1)
            except KeyboardInterrupt:
                print("\nMeasurement stopped by user")
                self.startup()
                break
            
            
    
    def sweep(self, osc_index: int, start: float, stop: float, step: float, timestep: float):
        self.standby(1)
        self.standby(2)
        
        frequencies = []
        phases = []
        amplitudes = []
        
        for f in range(start, stop, step):
            self.setFreq(osc_index, f)
            self.reset()
            time.sleep(timestep)
            amplitude, phase = self.getAmpAndPhase(osc_index)
            frequencies.append(f)
            phases.append(phase)
            amplitudes.append(amplitude)
            print(f"Freq: {f}\t Phase: {phase}\t Amplitude: {amplitude}")      
             
    def startupPLL(self, start_freq_mass: float, start_freq_temp: float):
        self.reset()
        
        print(f"Starting up PLLs at frequencies {start_freq_mass} and {start_freq_temp}")
        
        ## 6MHz crystal
        self.setInv(1,1)                          
        self.setFreq(1,start_freq_mass)
        self.setIQGain(1,0.00001)
        self.setInt(1,0.1)
        
        self.setInv(2,1)
        self.setFreq(2,start_freq_temp)
        self.setIQGain(2,0.00001)
        self.setInt(2,0.1)
        
        # wait for the loops to stabilize before starting measurement
        for _ in range(300):  # Wait for up to 30 seconds
            if self.getLockDetect(1) and self.getLockDetect(2):
                break
            # self.moveWindow(start_freq_mass, start_freq_temp) 
            print("Waiting for PLLs to lock...")
            time.sleep(0.1)
        
        if not self.getLockDetect(1) or not self.getLockDetect(2):
            print("Warning: PLLs did not lock within expected time. Check starting frequencies.")
        else:
            print("PLLs locked successfully at frequencies:")
            print(f"  Oscillator 1: {self.getFreq(1)} Hz")
            print(f"  Oscillator 2: {self.getFreq(2)} Hz")

        self.setInt(1,0.00001)
        self.setInt(2,0.00001)
        
    def setMeasurementReference(self, T = 23, mat_dens=19320):
        self.fM_start = self.getFreq(1)
        self.fT_start = self.getFreq(2)
        self.T_start = T # would be nice to measure this with a thermometer
        self.temp_comp = tca.TempCompAlgorithm(
            coefficient_file = self.coeff_file,
            T_start=T, 
            mat_dens=mat_dens,
            fM_start= self.fM_start,  # Hz
            fT_start= self.fT_start # Hz
        )

        print(f"Reference set: fM={self.fM_start}, fT={self.fT_start}, T={self.T_start}")
        
    def getMeasurement(self):
        fM = self.getFreq(1)
        fT = self.getFreq(2)
        T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq = self.temp_comp.FreqToTemp(fT, fM)
        return fM, fT, T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq
    
    def moveWindow(self, fM, fT):
        self.setFreq(1, fM - (self.window_size/2))
        self.setFreq(2, fT - (self.window_size/2))


    def startCalibration(self, cal_file_name):
        # confirm overwrite
        if(input(f"This will overwrite {cal_file_name}. Continue? (y/n): ") != 'y'):
            print("Calibration aborted.")
            return
        
        with open(cal_file_name, mode='w') as cal_file:
                cal_file.write(f"Temp,Freq_T,Freq_M\n")
        
        # Calibration routine
        self.startupPLL()
        while(True):
            temp = input("Current Temperature (C): ")
            if temp == '0':
                break
            freqM = self.getFreq(1)
            freqT = self.getFreq(2)
            print(f"Freq M: {freqM}, Freq T: {freqT} at Temp: {temp}")
            with open(cal_file_name, mode='a') as cal_file:
                cal_file.write(f"{temp},{freqT},{freqM}\n")


        
        
            


 
 
 







