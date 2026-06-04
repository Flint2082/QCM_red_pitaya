### Class to interface with the QCM Red Pitaya firmware, and to run measurements and calibrations.

import os
import time
import processing.TempCompAlgorithm as tca
import calendar
from collections import deque
import numpy as np

from domain.measurement import MeasurementData



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
        
        self.INT_GAIN_PRE_LOCK = 0.01
        self.INT_GAIN_POST_LOCK = 0.00001
        self.IQ_GAIN = 0.00001
        
        # variables
        self.coeff_file = os.path.join(base_dir, "..", "..", "data", "coeffecients.csv")

        self.T_start = 0
        self.fT_start = 0
        self.fM_start = 0
        self._inv = {1: False, 2: False}  # cached INV state, updated by setInv


        self.fpga = fpga

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
        self.fpga.write_register(register_name='inv_fb_'+str(osc_index), value=inv)
        self._inv[osc_index] = bool(inv)
        
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
        amp = (I**2 + Q**2)**0.5
        # arctan2(-Q, -I) rotates by exactly π and stays within [-π, π]
        phase = np.arctan2(-Q, -I) if self._inv.get(osc_index, False) else np.arctan2(Q, I)
        return amp, phase
    
    def getLockDetect(self, osc_index, amp=None, phase=None):
        if amp is None or phase is None:
            amp, phase = self.getAmpAndPhase(osc_index)
        # Amplitude above threshold and phase close to 0 indicates lock. The phase is the most important factor, but the amplitude check helps avoid false positives when the signal is very weak.
        return amp > 0.1 and abs(round(phase*10)) == 0
    
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
        self.setIQGain(1, self.IQ_GAIN)

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
        self.bothLocked = False
        self.MAX_STARTUP_TRIES = 100  # seconds
        
        print(f"Starting up PLLs around frequencies {start_freq_mass} and {start_freq_temp}")
        
        ## 6MHz crystal
        self.setInv(1,1)      
        self.setIQGain(1, self.IQ_GAIN)                    
        
        self.setInv(2,1)
        self.setIQGain(2, self.IQ_GAIN)
        
        for t in range(self.MAX_STARTUP_TRIES): # try to lock for up to MAX_STARTUP_TRIES
            self.reset()  # Ensure we're starting from a known state each time
            self.setFreq(1,start_freq_mass-self.WINDOW_SIZE/2)
            self.setInt(1,self.INT_GAIN_PRE_LOCK)
            
            self.setFreq(2,start_freq_temp-self.WINDOW_SIZE/2)
            self.setInt(2,self.INT_GAIN_PRE_LOCK)
            
            time.sleep(0.1)  # wait a bit for PLL to respond
        
            bothLocked = self.getLockDetect(1) and self.getLockDetect(2)
            if bothLocked:
                break

            print(f"Trying to lock... ( {t} / {self.MAX_STARTUP_TRIES} )", end='\r')
        
        if not bothLocked:
            print("Warning: PLLs did not lock within expected time. Check starting frequencies.")
        else:
            print("PLLs locked successfully at frequencies:")
            print(f"  Oscillator 1: {self.getFreq(1)} Hz    Phase: {self.getAmpAndPhase(1)[1]} ")
            print(f"  Oscillator 2: {self.getFreq(2)} Hz    Phase: {self.getAmpAndPhase(2)[1]} ")

        self.setInt(1, self.INT_GAIN_POST_LOCK)
        self.setInt(2, self.INT_GAIN_POST_LOCK)
        return bothLocked
        
    def getCoefficients(self) -> dict:
        import csv as _csv
        try:
            with open(self.coeff_file) as f:
                reader = _csv.DictReader(f)
                return {row['Name']: float(row['value']) for row in reader}
        except Exception as e:
            print(f"[QCM] Failed to read coefficients: {e}")
            return {}

    def setCoefficients(self, fM_0, fM_1, fM_2, fM_3, fT_0, fT_1, fT_2, fT_3):
        with open(self.coeff_file, 'w') as f:
            f.write("Name,value\n")
            for name, val in [('fM_0', fM_0), ('fM_1', fM_1), ('fM_2', fM_2), ('fM_3', fM_3),
                               ('fT_0', fT_0), ('fT_1', fT_1), ('fT_2', fT_2), ('fT_3', fT_3)]:
                f.write(f"{name},{val}\n")
        # Hot-patch the running TempCompAlgorithm if one is active
        tc = getattr(self, 'temp_comp', None)
        if tc is not None:
            tc.fM_1, tc.fM_2, tc.fM_3 = fM_1, fM_2, fM_3
            tc.fT_1, tc.fT_2, tc.fT_3 = fT_1, fT_2, fT_3
            tc.a = tc.fM_3 * tc.fT_0 - tc.fT_3 * tc.fM_0
            tc.b = tc.fM_2 * tc.fT_0 - tc.fT_2 * tc.fM_0
            tc.c = tc.fM_1 * tc.fT_0 - tc.fT_1 * tc.fM_0
        print(f"[QCM] Coefficients updated")

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
        amp_mass, phase_mass = self.getAmpAndPhase(1)
        amp_temp, phase_temp = self.getAmpAndPhase(2)
        T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq = self.temp_comp.FreqToTemp(fT, fM)
        return MeasurementData(
            timestamp=time.time(),
            freq_mass_mode=fM,
            freq_temp_mode=fT,
            uncompensated_thickness=uncompensated_thickness_nm,
            calculated_thickness=compensated_thickness_nm,
            calculated_temp=T_calc,
            compensated_freq=compensated_m_freq,
            amp_mass=amp_mass,
            phase_mass=phase_mass,
            amp_temp=amp_temp,
            phase_temp=phase_temp,
            lock_mass=self.getLockDetect(1, amp=amp_mass, phase=phase_mass),
            lock_temp=self.getLockDetect(2, amp=amp_temp, phase=phase_temp),
        )
    
    def moveWindow(self, fM, fT):
        self.setFreq(1, fM - (self.WINDOW_SIZE/2))
        self.setFreq(2, fT - (self.WINDOW_SIZE/2))


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


        
        
            


 
 
 







