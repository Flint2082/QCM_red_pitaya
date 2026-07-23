### Class to interface with the QCM Red Pitaya firmware, and to run measurements and calibrations.

import os
import time
import processing.TempCompAlgorithm as tca
import calendar
from collections import deque
import numpy as np

from domain.measurement import MeasurementData


# Frequency capture window (Hz). Must match the FPGA's scan window so the PLL
# start point (target - WINDOW_SIZE/2) lines up with the hardware sweep. Kept at
# module level so other layers (e.g. the REST API) can report the resulting
# capture range without needing a QCMInterface instance.
WINDOW_SIZE = 2**12


class QCMInterface:
    def __init__(self, fpga):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        directory = os.path.join(base_dir, "..", "..", "model_composer", "qcm_rp", "outputs")
        newest_file = max(
            (os.path.join(directory, f) for f in os.listdir(directory)),
            key=os.path.getmtime
        )
        
        # constants
        self.WINDOW_SIZE = WINDOW_SIZE  # see the module-level constant
        self.MASS_MODE = 1
        self.TEMP_MODE = 2
        
        self.INT_GAIN_PRE_LOCK = 0.01
        self.INT_GAIN_POST_LOCK = 0.00001
        self.PROP_GAIN_DEFAULT = 0.0  # proportional path off by default (pure-I loop)
        self.LPF_FREQ = 200.0  # Hz — default demodulator LPF cutoff frequency
        self.PHASE_DETECT_DEFAULT = 0  # mult_sel: FPGA phase-detector type (0 = ATAN, 1 = multiplier)

        # Lock-detect conditions (configurable via settings). A channel counts
        # as locked when its amplitude exceeds the threshold AND its phase is
        # within the tolerance of the loop's lock point.
        self.LOCK_AMP_THRESHOLD = 0.1     # minimum amplitude
        self.LOCK_PHASE_TOLERANCE = 0.05  # maximum |phase - lock point|
        # Phase (radians) the loop settles at once locked, for the default
        # inverted feedback. Both phase detectors (ATAN and multiplier) lock in
        # quadrature at -pi/2; non-inverted feedback flips the sign — see
        # getPhaseLockTarget.
        self.PHASE_LOCK_TARGET = -np.pi / 2
        
        # variables
        # Calibration coefficients, pushed in from the active crystal profile via
        # setCoefficients. fM_0/fT_0 are recomputed by the temp-comp algorithm
        # from the start frequencies, so the 0-order terms here are unused.
        self.coefficients = {
            'fM_0': 0.0, 'fM_1': 0.0, 'fM_2': 0.0, 'fM_3': 0.0,
            'fT_0': 0.0, 'fT_1': 0.0, 'fT_2': 0.0, 'fT_3': 0.0,
        }

        # Sensor parameters, pushed in from the active crystal profile via
        # setSensorParams. Defaults match TempCompAlgorithm's.
        self.mass_sensitivity = -13.3e-8  # kg/(m²·Hz) — negative: added mass lowers the frequency
        self.sens_area = 5.25e-5         # m²
        self.freq_virgin = 0.0           # Hz — pristine crystal frequency for Z-match; 0 = use run start

        self.T_start = 0
        self.fT_start = 0
        self.fM_start = 0
        # Cached per-oscillator loop settings, updated via setOscConfig and reused
        # by startupPLL so a lock honors the configured settings rather than fixed
        # defaults. Defaults match startupPLL's historical hard-coded values.
        self._inv = {1: True, 2: True}                                          # inverted feedback
        self._int_gain = {1: self.INT_GAIN_POST_LOCK, 2: self.INT_GAIN_POST_LOCK}  # post-lock integrator gain
        self._prop_gain = {1: self.PROP_GAIN_DEFAULT, 2: self.PROP_GAIN_DEFAULT}   # proportional gain
        self._lpf_freq = {1: self.LPF_FREQ, 2: self.LPF_FREQ}                   # LPF cutoff frequency (Hz)
        self._phase_detect = {1: self.PHASE_DETECT_DEFAULT, 2: self.PHASE_DETECT_DEFAULT}  # mult_sel: phase-detector type


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

    def setProp(self, osc_index, gain):
        self.fpga.write_register(register_name='proportional_'+str(osc_index),value=int(gain*2**32)) # multiplication to account for fixed-point (32F32) representation in FPGA

    def setLPFFreq(self, osc_index, freq):
        gain = ( 2 * np.pi * freq ) / ( self.fpga.sample_rate + ( 2 * np.pi * freq ) )
        self.fpga.write_register(register_name='lpf_gain_'+str(osc_index),value=int(gain*2**32)) # multiplication to account for fixed-point (32F32) representation in FPGA

    def setLockDetect(self, amp_threshold, phase_tolerance):
        self.LOCK_AMP_THRESHOLD = amp_threshold
        self.LOCK_PHASE_TOLERANCE = phase_tolerance

    def setSensorParams(self, mass_sensitivity, sens_area, freq_virgin=0.0):
        self.mass_sensitivity = mass_sensitivity
        self.sens_area = sens_area
        self.freq_virgin = freq_virgin
        # Hot-patch the running TempCompAlgorithm if one is active. Mirrors the
        # derivations in TempCompAlgorithm.__init__: fM_0 = 1/(ms*A) and
        # fT_0 = (fT_start/fM_start)/(ms*A), both in Hz/kg.
        tc = getattr(self, 'temp_comp', None)
        if tc is not None:
            tc.mass_sensitivity = mass_sensitivity
            tc.sens_area = sens_area
            tc.f_virgin = freq_virgin or tc.fM_start
            tc.fM_0 = 1 / (mass_sensitivity * sens_area)
            tc.fT_0 = (tc.fT_start / tc.fM_start) / (mass_sensitivity * sens_area)
            tc.a = tc.fM_3 * tc.fT_0 - tc.fT_3 * tc.fM_0
            tc.b = tc.fM_2 * tc.fT_0 - tc.fT_2 * tc.fM_0
            tc.c = tc.fM_1 * tc.fT_0 - tc.fT_1 * tc.fM_0
        print(f"[QCM] Sensor params updated: mass_sensitivity={mass_sensitivity}, sens_area={sens_area}, freq_virgin={freq_virgin}")

    def setMockSigFreq(self, freq):
        self.fpga.write_register(register_name='mock_sig_freq', value=int(freq*2**6)) # multiplication to account for fixed-point (32F6) representation in FPGA

    def setInv(self, osc_index, inv: bool):
        self.fpga.write_register(register_name='inv_fb_'+str(osc_index), value=inv)
        self._inv[osc_index] = bool(inv)

    def setPhaseDetect(self, osc_index, mode):
        # mult_sel is a 1-bit FPGA register selecting the phase-detector type
        # (0 = ATAN, 1 = multiplier), so coerce to a single bit.
        value = 1 if int(mode) else 0
        self.fpga.write_register(register_name='mult_sel_'+str(osc_index), value=value)
        self._phase_detect[osc_index] = value

    def setOscConfig(self, osc_index, int_gain=None, prop_gain=None, lpf_freq=None, inverted=None, phase_detect=None):
        """Apply and remember the configured per-oscillator loop settings. The
        cached values are reused by startupPLL so a lock uses the persisted
        settings. Low-level setters (setInt/setProp/setLPFFreq) stay uncached for
        the transient writes done during locking, sweeps and standby."""
        if int_gain is not None:
            self._int_gain[osc_index] = int_gain
            self.setInt(osc_index, int_gain)
        if prop_gain is not None:
            self._prop_gain[osc_index] = prop_gain
            self.setProp(osc_index, prop_gain)
        if lpf_freq is not None:
            self._lpf_freq[osc_index] = lpf_freq
            self.setLPFFreq(osc_index, lpf_freq)
        if inverted is not None:
            self.setInv(osc_index, inverted)
        if phase_detect is not None:
            self.setPhaseDetect(osc_index, phase_detect)

    def setOutputMode(self, mode = -1):
        if mode == -1:
            print("Output mode not set. These are the available modes:")
            print("0: The delta of the two inputs")
            print("1: Mock sinewave at the software-defined frequency (mock_sig_freq)")
            print("2: The mass mode frequency (fine)")
            print("3: The mass mode frequency (coarse)")
            print("4: The mass mode multiplier output")
            print("5: The mass mode lock detector")
            print("6: The mass mode power detector")
            print("7: The temp mode frequency (fine)")
            print("8: The temp mode frequency (coarse)")
            print("9: The temp mode multiplier output")
            print("10: The temp mode lock detector")
            print("11: The temp mode power detector")
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

    def getMag(self, osc_index):
        magnitude = self.to_signed(self.fpga.read_register(f'mag_out_{osc_index}'),30)
        return magnitude/2**12       # FIX_30_12
        
    def getPhase(self, osc_index):
        phase = self.to_signed(self.fpga.read_register(f'phase_out_{osc_index}'),30)
        return phase/2**12           # FIX_30_12

    def getPhaseLockTarget(self, osc_index):
        """Phase (radians) this channel settles at once locked. Both phase
        detectors lock in quadrature, and inverting the feedback flips which of
        the two quadrature points (-pi/2 / +pi/2) is the stable one."""
        target = self.PHASE_LOCK_TARGET
        if not self._inv.get(osc_index, True):
            target = -target
        return target

    def getLockDetect(self, osc_index, amp=None, phase=None):
        if amp is None or phase is None:
            amp = self.getMag(osc_index)
            phase = self.getPhase(osc_index)
        # Amplitude above threshold and phase close to this channel's lock point
        # indicates lock. The phase is the most important factor, but the amplitude check helps avoid false positives when the signal is very weak.
        target = self.getPhaseLockTarget(osc_index)
        error = (phase - target + np.pi) % (2 * np.pi) - np.pi  # shortest angular distance, handles wrap
        return amp > self.LOCK_AMP_THRESHOLD and abs(error) < self.LOCK_PHASE_TOLERANCE
    
    # ===========================
    # Control methods
    # ===========================
    
    def standby(self, osc_index: int):
        self.setFreq(osc_index,0)
        self.setInt(osc_index,0)
        self.setProp(osc_index,0)
        self.reset()

    def startCapAdjust(self, freq_mass, freq_temp):
        """Emit two static (open-loop) tones for nulling the trim capacitor:
        osc 1 at (Fm+Ft)/2 (between the modes) and osc 2 at Fm*0.9 (below the
        mass mode) — both off-resonance, so the demodulator amplitude there is
        dominated by the crystal's static capacitance C0. The integrators are
        held at 0 so the NCOs stay parked; the user minimises both amplitudes by
        tuning the PCB capacitor. Returns the two emitted frequencies."""
        f1 = (freq_mass + freq_temp) / 2.0
        f2 = freq_mass * 0.9
        self.reset()
        self.setInt(1, 0.0)
        self.setInt(2, 0.0)
        self.setProp(1, 0.0)
        self.setProp(2, 0.0)
        self.setLPFFreq(1, self._lpf_freq[1])
        self.setLPFFreq(2, self._lpf_freq[2])
        self.setFreq(1, f1)
        self.setFreq(2, f2)
        print(f"[QCM] Capacitor-adjust tones: osc1={f1:.0f} Hz, osc2={f2:.0f} Hz")
        return f1, f2

    def capacitorAdjustment(self):
        self.standby(2)
        self.setFreq(1, 6000000)
        self.setInt(1, 0.00)
        self.setProp(1, 0.00)
        self.setLPFFreq(1, self.LPF_FREQ)

        while True:
            try:
                amplitude = self.getMag(1)
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
            amplitude = self.getMag(osc_index)
            phase = self.getPhase(osc_index)
            frequencies.append(f)
            phases.append(phase)
            amplitudes.append(amplitude)
            print(f"Freq: {f}\t Phase: {phase}\t Amplitude: {amplitude}")      
             
    def startupPLL(self, start_freq_mass: float, start_freq_temp: float):
        self.bothLocked = False
        self.MAX_STARTUP_TRIES = 20  
        
        print(f"Starting up PLLs around frequencies {start_freq_mass} and {start_freq_temp}")

        ## Apply the configured per-oscillator settings (inversion + proportional gain + LPF cutoff + phase-detector type)
        self.setInv(1, self._inv[1])
        self.setProp(1, self._prop_gain[1])
        self.setLPFFreq(1, self._lpf_freq[1])
        self.setPhaseDetect(1, self._phase_detect[1])

        self.setInv(2, self._inv[2])
        self.setProp(2, self._prop_gain[2])
        self.setLPFFreq(2, self._lpf_freq[2])
        self.setPhaseDetect(2, self._phase_detect[2])

        for t in range(self.MAX_STARTUP_TRIES): # try to lock for up to MAX_STARTUP_TRIES
            self.reset()  # Ensure we're starting from a known state each time
            self.setFreq(1,start_freq_mass-self.WINDOW_SIZE/2)
            self.setInt(1,self.INT_GAIN_PRE_LOCK)
            
            self.setFreq(2,start_freq_temp-self.WINDOW_SIZE/2)
            self.setInt(2,self.INT_GAIN_PRE_LOCK)
            
            time.sleep(0.5)  # wait a bit for PLL to respond
        
            bothLocked = self.getLockDetect(1) and self.getLockDetect(2)
            if bothLocked:
                break

            print(f"Trying to lock... ( {t} / {self.MAX_STARTUP_TRIES} )", end='\r')
        
        if not bothLocked:
            print("Warning: PLLs did not lock within expected time. Check starting frequencies.")
        else:
            print("PLLs locked successfully at frequencies:")
            print(f"  Oscillator 1: {self.getFreq(1)} Hz    Phase: {self.getPhase(1)}    Amplitude: {self.getMag(1)}")
            print(f"  Oscillator 2: {self.getFreq(2)} Hz    Phase: {self.getPhase(2)}    Amplitude: {self.getMag(2)}")

        # Settle to the configured (post-lock) integrator gain
        self.setInt(1, self._int_gain[1])
        self.setInt(2, self._int_gain[2])
        return bothLocked
        
    def getCoefficients(self) -> dict:
        return dict(self.coefficients)

    def setCoefficients(self, fM_0, fM_1, fM_2, fM_3, fT_0, fT_1, fT_2, fT_3):
        self.coefficients = {
            'fM_0': fM_0, 'fM_1': fM_1, 'fM_2': fM_2, 'fM_3': fM_3,
            'fT_0': fT_0, 'fT_1': fT_1, 'fT_2': fT_2, 'fT_3': fT_3,
        }
        # Hot-patch the running TempCompAlgorithm if one is active
        tc = getattr(self, 'temp_comp', None)
        if tc is not None:
            tc.fM_1, tc.fM_2, tc.fM_3 = fM_1, fM_2, fM_3
            tc.fT_1, tc.fT_2, tc.fT_3 = fT_1, fT_2, fT_3
            tc.a = tc.fM_3 * tc.fT_0 - tc.fT_3 * tc.fM_0
            tc.b = tc.fM_2 * tc.fT_0 - tc.fT_2 * tc.fM_0
            tc.c = tc.fM_1 * tc.fT_0 - tc.fT_1 * tc.fM_0
        print(f"[QCM] Coefficients updated")

    def setMeasurementReference(self, T = 23, mat_dens=19320, z_ratio=1.0):
        self.fM_start = self.getFreq(1)
        self.fT_start = self.getFreq(2)
        self.T_start = T # would be nice to measure this with a thermometer
        self.temp_comp = tca.TempCompAlgorithm(
            coefficients = self.coefficients,
            T_start=T,
            mat_dens=mat_dens,
            sens_area=self.sens_area,
            mass_sensitivity=self.mass_sensitivity,
            z_ratio=z_ratio,
            freq_virgin=self.freq_virgin,
            fM_start= self.fM_start,  # Hz
            fT_start= self.fT_start # Hz
        )

        print(f"Reference set: fM={self.fM_start}, fT={self.fT_start}, T={self.T_start}")
        
    def getMeasurement(self):
        fM = self.getFreq(1)
        fT = self.getFreq(2)
        amp_mass = self.getMag(1)
        phase_mass = self.getPhase(1)
        amp_temp = self.getMag(2)
        phase_temp = self.getPhase(2)
        T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq = self.temp_comp.FreqToTemp(fT, fM)
        if np.isfinite(compensated_m_freq):
            self.setMockSigFreq(compensated_m_freq)
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


        
        
            


 
 
 







