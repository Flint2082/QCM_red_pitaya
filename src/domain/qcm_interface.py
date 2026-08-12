### Class to interface with the QCM Red Pitaya firmware, and to run measurements and calibrations.

import os
import time
import processing.TempCompAlgorithm as tca
import calendar
from collections import deque
import numpy as np

from domain.measurement import MeasurementData, TelemetryData


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
        
        self.INT_GAIN_PRE_LOCK = 0.0001
        self.INT_GAIN_POST_LOCK = 0.00001
        self.LPF_FREQ = 200.0  # Hz — default demodulator LPF cutoff frequency

        # Lock-detect conditions (configurable via settings). A channel counts as
        # locked when the phase error is both *centred* on the loop's lock point
        # and *quiet* — mean and standard deviation over a rolling window.
        #
        # The scatter test replaces the amplitude threshold this used to carry.
        # Phase noise scales with 1/SNR, so a weak signal widens the scatter by
        # itself: the same guard, but self-normalising. An absolute amplitude
        # level could not do the job here — the b-mode amplitude halves over
        # ~20 K, so any level was either too low to mean anything or high enough
        # to flag a perfectly good hot lock as lost.
        self.LOCK_PHASE_TOLERANCE = 0.05  # rad — max |mean phase error|
        self.LOCK_PHASE_STD = 0.05        # rad — max phase-error std ("lock quality")
        self.LOCK_WINDOW = 12             # samples in the rolling estimate (~1.2 s at 10 Hz)
        self.LOCK_MIN_SAMPLES = 4         # fewer than this says nothing either way
        # Phase (radians) the loop settles at once locked, for the default
        # inverted feedback. The phase detector locks in quadrature at -pi/2;
        # non-inverted feedback flips the sign — see getPhaseLockTarget.
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
        self.tooling_ratio = 1.0         # proportional scaling of reported thickness; 1.0 = no scaling

        self.T_start = 0
        self.fT_start = 0
        self.fM_start = 0
        # Cached per-oscillator loop settings, updated via setOscConfig and reused
        # by startupPLL so a lock honors the configured settings rather than fixed
        # defaults. Defaults match startupPLL's historical hard-coded values.
        self._inv = {1: True, 2: True}                                          # inverted feedback
        self._int_gain = {1: self.INT_GAIN_POST_LOCK, 2: self.INT_GAIN_POST_LOCK}  # post-lock integrator gain
        self._lpf_freq = {1: self.LPF_FREQ, 2: self.LPF_FREQ}                   # LPF cutoff frequency (Hz)

        # Rolling phase-error history per oscillator, fed one sample per
        # acquisition cycle by sampleLock. Lock state and lock quality are both
        # read back out of these, so every consumer judges lock the same way.
        self._phase_err = {1: deque(maxlen=self.LOCK_WINDOW),
                           2: deque(maxlen=self.LOCK_WINDOW)}

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

    def setLPFFreq(self, osc_index, freq):
        gain = ( 2 * np.pi * freq ) / ( self.fpga.sample_rate + ( 2 * np.pi * freq ) )
        self.fpga.write_register(register_name='lpf_gain_'+str(osc_index),value=int(gain*2**32)) # multiplication to account for fixed-point (32F32) representation in FPGA

    def setLockDetect(self, phase_tolerance, phase_std):
        self.LOCK_PHASE_TOLERANCE = phase_tolerance
        self.LOCK_PHASE_STD = phase_std

    def setSensorParams(self, mass_sensitivity, sens_area, freq_virgin=0.0, tooling_ratio=1.0):
        self.mass_sensitivity = mass_sensitivity
        self.sens_area = sens_area
        self.freq_virgin = freq_virgin
        self.tooling_ratio = tooling_ratio
        # Hot-patch the running TempCompAlgorithm if one is active. Mirrors the
        # derivations in TempCompAlgorithm.__init__: fM_0 = 1/(ms*A) and
        # fT_0 = (fT_start/fM_start)/(ms*A), both in Hz/kg.
        tc = getattr(self, 'temp_comp', None)
        if tc is not None:
            tc.mass_sensitivity = mass_sensitivity
            tc.sens_area = sens_area
            tc.f_virgin = freq_virgin or tc.fM_start
            tc.tooling_ratio = tooling_ratio
            tc.fM_0 = 1 / (mass_sensitivity * sens_area)
            tc.fT_0 = (tc.fT_start / tc.fM_start) / (mass_sensitivity * sens_area)
            tc.a = tc.fM_3 * tc.fT_0 - tc.fT_3 * tc.fM_0
            tc.b = tc.fM_2 * tc.fT_0 - tc.fT_2 * tc.fM_0
            tc.c = tc.fM_1 * tc.fT_0 - tc.fT_1 * tc.fM_0
        print(f"[QCM] Sensor params updated: mass_sensitivity={mass_sensitivity}, sens_area={sens_area}, freq_virgin={freq_virgin}, tooling_ratio={tooling_ratio}")

    def setMockSigFreq(self, freq):
        self.fpga.write_register(register_name='mock_sig_freq', value=int(freq*2**6)) # multiplication to account for fixed-point (32F6) representation in FPGA

    def setInv(self, osc_index, inv: bool):
        self.fpga.write_register(register_name='inv_fb_'+str(osc_index), value=inv)
        self._inv[osc_index] = bool(inv)

    def setOscConfig(self, osc_index, int_gain=None, lpf_freq=None, inverted=None):
        """Apply and remember the configured per-oscillator loop settings. The
        cached values are reused by startupPLL so a lock uses the persisted
        settings. Low-level setters (setInt/setLPFFreq) stay uncached for the
        transient writes done during locking, sweeps and standby."""
        if int_gain is not None:
            self._int_gain[osc_index] = int_gain
            self.setInt(osc_index, int_gain)
        if lpf_freq is not None:
            self._lpf_freq[osc_index] = lpf_freq
            self.setLPFFreq(osc_index, lpf_freq)
        if inverted is not None:
            self.setInv(osc_index, inverted)

    def setOutputMode(self, mode = -1):
        if mode == -1:
            print("Output mode not set. These are the available modes:")
            print("0: The delta of the two inputs")
            print("1: Mock sinewave at the software-defined frequency (mock_sig_freq)")
            print("2: The mass mode frequency (fine)")
            print("3: The mass mode frequency (coarse)")
            print("4: The mass mode multiplier output")
            print("5: The mass mode LPF magnitude")
            print("6: The mass mode LPF phase")
            print("7: The temp mode frequency (fine)")
            print("8: The temp mode frequency (coarse)")
            print("9: The temp mode multiplier output")
            print("10: The temp mode LPF magnitude")
            print("11: The temp mode LPF phase")
        else:
            self.fpga.write_register(register_name='output_select', value=mode)
        
    # ===========================
    # getter methods
    # ===========================

    def reset(self):
        self.fpga.write_register(register_name='reset', value=1)
        self.fpga.write_register(register_name='reset', value=0)
        self.resetLockStats()  # the loop just moved; old phase errors say nothing about the new state
        
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
        """Phase (radians) this channel settles at once locked. The loop locks in
        quadrature, and inverting the feedback flips which of the two quadrature
        points (-pi/2 / +pi/2) is the stable one."""
        target = self.PHASE_LOCK_TARGET
        if not self._inv.get(osc_index, True):
            target = -target
        return target

    def _phaseError(self, osc_index, phase):
        """Shortest angular distance from this channel's lock point, wrap-safe."""
        target = self.getPhaseLockTarget(osc_index)
        return (phase - target + np.pi) % (2 * np.pi) - np.pi

    def sampleLock(self, osc_index, phase=None):
        """Feed one phase-error sample into the rolling window and return
        (locked, quality). Call once per acquisition cycle per channel: lock is
        judged from the window, never from a single reading."""
        if phase is None:
            phase = self.getPhase(osc_index)
        self._phase_err[osc_index].append(self._phaseError(osc_index, phase))
        return self.getLockDetect(osc_index), self.getLockQuality(osc_index)

    def resetLockStats(self, osc_index=None):
        """Drop the phase-error history for one or both channels. Called from
        reset(), so any deliberate disturbance of the loop starts the window
        fresh instead of judging the new state through the old samples."""
        for i in ((osc_index,) if osc_index is not None else (1, 2)):
            self._phase_err[i].clear()

    def getLockQuality(self, osc_index):
        """Standard deviation of the phase error (rad) over the rolling window —
        the "lock quality" figure, smaller is better. None until the window holds
        LOCK_MIN_SAMPLES, which is the honest answer rather than a flattering 0.

        This is what the amplitude threshold used to be for: it answers "is the
        phase reading trustworthy?" directly. Phase noise scales with 1/SNR, so a
        weak signal shows up here without anyone having to know what amplitude to
        expect at this temperature, on this crystal, under this much film."""
        buf = self._phase_err[osc_index]
        if len(buf) < self.LOCK_MIN_SAMPLES:
            return None
        return float(np.std(buf))

    def getLockDetect(self, osc_index):
        """True when the phase error is both centred on the lock point and quiet.

        The response is deliberately asymmetric: a single wild sample entering
        the window lifts the std straight away, so a lost lock is flagged within
        a sample or two, while clearing the flag needs the whole window to refill
        with quiet samples. Fast to distrust, slow to trust."""
        buf = self._phase_err[osc_index]
        if len(buf) < self.LOCK_MIN_SAMPLES:
            return False
        return (abs(float(np.mean(buf))) < self.LOCK_PHASE_TOLERANCE
                and float(np.std(buf)) < self.LOCK_PHASE_STD)

    def _fillLockWindows(self, osc_indices=(1, 2), interval=0.02):
        """Take a fresh burst of phase samples and report {osc: locked}. Used
        where no acquisition loop is feeding sampleLock — i.e. during startupPLL.

        Channels are interleaved rather than filled one after the other, so the
        burst costs one settling period instead of one per channel. The burst is
        faster than the acquisition loop's ~10 Hz, but the demodulator LPF
        (LPF_FREQ, 200 Hz by default) settles in far less than `interval`, so
        samples are independent either way and the two paths produce comparable
        standard deviations."""
        for i in osc_indices:
            self.resetLockStats(i)
        for _ in range(self.LOCK_WINDOW):
            for i in osc_indices:
                self.sampleLock(i)
            time.sleep(interval)
        return {i: self.getLockDetect(i) for i in osc_indices}


    # ===========================
    # Control methods
    # ===========================
    
    def standby(self, osc_index: int):
        self.setFreq(osc_index,0)
        self.setInt(osc_index,0)
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
        self.MAX_STARTUP_TRIES = 10  
        
        print(f"Starting up PLLs around frequencies {start_freq_mass} and {start_freq_temp}")

        ## Apply the configured per-oscillator settings (inversion + LPF cutoff)
        self.setInv(1, self._inv[1])
        self.setLPFFreq(1, self._lpf_freq[1])

        self.setInv(2, self._inv[2])
        self.setLPFFreq(2, self._lpf_freq[2])

        for t in range(self.MAX_STARTUP_TRIES): # try to lock for up to MAX_STARTUP_TRIES
            self.setFreq(1,start_freq_mass-self.WINDOW_SIZE/2)
            self.setInt(1,self.INT_GAIN_PRE_LOCK)
            
            self.setFreq(2,start_freq_temp-self.WINDOW_SIZE/2)
            self.setInt(2,self.INT_GAIN_PRE_LOCK)
            
            self.reset()  # Ensure we're starting from a known state each time
            
            time.sleep(1)  # wait a bit for PLL to respond

            # Both windows are filled before either is judged — evaluating them
            # with `and` would short-circuit and leave channel 2 empty, so it
            # could never read as locked.
            locked = self._fillLockWindows((1, 2))
            bothLocked = locked[1] and locked[2]
            if bothLocked:
                break

            print(f"Trying to lock... ( {t} / {self.MAX_STARTUP_TRIES} )", end='\r')
        
        if not bothLocked:
            print("Warning: PLLs did not lock within expected time. Check starting frequencies.")
        else:
            print("PLLs locked successfully at frequencies:")
            for i in (1, 2):
                q = self.getLockQuality(i)
                print(f"  Oscillator {i}: {self.getFreq(i)} Hz    Phase: {self.getPhase(i)}    "
                      f"Amplitude: {self.getMag(i)}    Quality: {'—' if q is None else f'{q:.4f} rad'}")

        # Settle to the configured (post-lock) integrator gain
        self.setInt(1, self._int_gain[1])
        self.setInt(2, self._int_gain[2])
        # Drop the acquisition-time samples: they were taken at the pre-lock gain,
        # and mixing them with post-lock ones would let the gain change itself show
        # up as phase noise and trip an immediate false "lock lost".
        self.resetLockStats()
        return bothLocked
        
    def getSettingsSnapshot(self) -> dict:
        """Loop/lock configuration as plain data, for recording alongside a run so
        a CSV can be interpreted later without guessing how it was acquired."""
        return {
            "oscillators": {
                str(i): {
                    "int_gain":          self._int_gain.get(i),
                    "lpf_freq":          self._lpf_freq.get(i),
                    "inverted":          self._inv.get(i),
                    "phase_lock_target": self.getPhaseLockTarget(i),
                }
                for i in (1, 2)
            },
            "lock_detect": {
                "phase_tolerance": self.LOCK_PHASE_TOLERANCE,
                "phase_std":       self.LOCK_PHASE_STD,
                "window":          self.LOCK_WINDOW,
            },
            "window_size": self.WINDOW_SIZE,
        }

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
            tooling_ratio=self.tooling_ratio,
            fM_start= self.fM_start,  # Hz
            fT_start= self.fT_start # Hz
        )

        print(f"Reference set: fM={self.fM_start}, fT={self.fT_start}, T={self.T_start}")
        
    def getTelemetry(self) -> TelemetryData:
        """One sample of everything the hardware reports directly. Needs no
        measurement reference and no calibration, so it is available in every
        state — which is what keeps the monitor charts live outside a run.

        Lock state and quality are read from the rolling window rather than
        sampled here; the acquisition loop owns the sampling (see sampleLock) so
        the window advances exactly once per cycle."""
        return TelemetryData(
            timestamp=time.time(),
            freq_mass_mode=self.getFreq(1),
            freq_temp_mode=self.getFreq(2),
            amp_mass=self.getMag(1),
            phase_mass=self.getPhase(1),
            amp_temp=self.getMag(2),
            phase_temp=self.getPhase(2),
            lock_mass=self.getLockDetect(1),
            lock_temp=self.getLockDetect(2),
            quality_mass=self.getLockQuality(1),
            quality_temp=self.getLockQuality(2),
        )

    def getMeasurement(self) -> MeasurementData:
        t = self.getTelemetry()
        T_calc, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq = \
            self.temp_comp.FreqToTemp(t.freq_temp_mode, t.freq_mass_mode)
        if np.isfinite(compensated_m_freq):
            self.setMockSigFreq(compensated_m_freq)
        return MeasurementData(
            **vars(t),
            uncompensated_thickness=uncompensated_thickness_nm,
            calculated_thickness=compensated_thickness_nm,
            calculated_temp=T_calc,
            compensated_freq=compensated_m_freq,
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


        
        
            


 
 
 







