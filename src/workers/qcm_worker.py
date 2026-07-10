# Responsible for:

# executing commands
# owning hardware access
# running sweeps
# acquisition loops

# async 
# Listens for commands from application, executes them
# Sends events back to application (e.g. measurement complete, sweep step done, etc.)

import math
import time
import threading
import queue

from domain.qcm_interface import QCMInterface
from domain.run_logger import RunLogger
from messaging.defines import WorkerState
from messaging.worker_event import *
from messaging.worker_command import *

class QCMWorker(threading.Thread):
    def __init__(self, qcm: QCMInterface, command_queue: queue.Queue, event_queue: queue.Queue):
        super().__init__()
        self.qcm = qcm
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.running = True
        self.state = WorkerState.IDLE
        # Durable server-side CSV log of each run, independent of the WebSocket.
        self.logger = RunLogger()
        # Lock-status emission throttle: send on change, plus a periodic
        # heartbeat so new WS clients converge. Unthrottled this produced
        # ~10 events/sec of WS traffic even when idle.
        self.LOCK_STATUS_HEARTBEAT = 1.0  # seconds
        self._last_lock_status: tuple | None = None
        self._last_lock_emit = 0.0
        # Automatic re-lock: if both modes stay unlocked this long during a
        # measurement, re-acquire around the last lock frequencies.
        self.AUTO_RELOCK_AFTER = 1.0  # seconds
        self._lock_freqs: tuple | None = None  # (mass, temp) from the last GET LOCK
        self._lock_lost_since: float | None = None
        
    def stop(self):
        self.running = False
        self.command_queue.put(None) # unblock queue.get()

    def run(self):
        print("QCM worker started")

        while self.running:
            try:
                try:
                    command = self.command_queue.get(timeout=0.1) # non-blocking with timeout

                    if command is not None:
                        self.handle_command(command)

                except queue.Empty:
                    pass

                self.update()

            except Exception as e:
                self.event_queue.put(
                    ErrorEvent(str(e))
                )

        self.logger.stop()  # flush & close any open run log on shutdown
        print("QCM worker stopped")

    def _set_state(self, new_state: WorkerState):
        self.state = new_state
        self.event_queue.put(StateEvent(state=new_state))

    def _acquire_lock(self, mass: float, temp: float):
        """Run the PLL startup around (mass, temp). If a measurement was running
        this re-locks without stopping it (the measurement reference is left
        untouched, so the deposition baseline and recorded data carry through);
        otherwise it returns to IDLE. Used by GET LOCK and by the automatic
        re-lock. Caches the frequencies for the auto re-lock to reuse."""
        self._lock_freqs = (mass, temp)
        resume_measuring = (self.state == WorkerState.MEASURING)
        self._set_state(WorkerState.LOCKING)
        locked = self.qcm.startupPLL(mass, temp)
        if not locked:
            self.event_queue.put(LockFailedEvent())
        if resume_measuring:
            self.logger.write_event("RELOCK", "lock re-established" if locked else "re-lock failed")
        self._lock_lost_since = None  # reset the loss timer after any attempt
        self._set_state(WorkerState.MEASURING if resume_measuring else WorkerState.IDLE)
        return locked

    def handle_command(self, command: WorkerCommand):

        # ============================
        # Control commands
        # ============================

        if isinstance(command, StartupPLLCommand):
            self._acquire_lock(command.start_freq_mass, command.start_freq_temp)

        # Capacitor adjustment (open-loop tones for nulling the trim cap)
        elif isinstance(command, StartCapAdjustCommand) and self.state == WorkerState.IDLE:
            self.qcm.startCapAdjust(command.freq_mass, command.freq_temp)
            self._set_state(WorkerState.CAP_ADJUST)
        elif isinstance(command, StopCapAdjustCommand) and self.state == WorkerState.CAP_ADJUST:
            self.qcm.standby(1)
            self.qcm.standby(2)
            self._set_state(WorkerState.IDLE)

        # Start measurement
        elif isinstance(command, StartMeasurementCommand) and self.state == WorkerState.IDLE:
            self.qcm.setMeasurementReference(T=command.ambient_temp, mat_dens=command.mat_dens, z_ratio=command.z_ratio)
            self.logger.start()
            self.logger.write_event("RUN_START", f"T={command.ambient_temp} mat_dens={command.mat_dens} z_ratio={command.z_ratio}")
            self._set_state(WorkerState.MEASURING)

        # Stop measurement
        elif isinstance(command, StopMeasurementCommand) and self.state == WorkerState.MEASURING:
            # Auto-update start frequencies to current locked frequencies (rounded to 1 kHz)
            lock_mass = self.qcm.getLockDetect(1)
            lock_temp = self.qcm.getLockDetect(2)
            if lock_mass and lock_temp:
                freq_mass = round(self.qcm.getFreq(1) / 1000) * 1000
                freq_temp = round(self.qcm.getFreq(2) / 1000) * 1000
                self.event_queue.put(StartFreqAutoUpdatedEvent(freq_mass=freq_mass, freq_temp=freq_temp))
            self.logger.write_event("RUN_STOP")
            self.logger.stop()
            self._set_state(WorkerState.IDLE)

        # Sweep
        elif isinstance(command, StartSweepCommand) and self.state == WorkerState.IDLE:
            self._set_state(WorkerState.SWEEPING)
            self._run_sweep(command)
            self._set_state(WorkerState.IDLE)

        # ============================
        # Setting commands
        # ============================    
            
        elif isinstance(command, SetFrequencyCommand):
            self.qcm.setFreq(command.oscillator_idx, command.frequency)
        elif isinstance(command, SetIntegratorGainCommand):
            self.qcm.setOscConfig(command.oscillator_idx, int_gain=command.gain)
        elif isinstance(command, SetProportionalGainCommand):
            self.qcm.setOscConfig(command.oscillator_idx, prop_gain=command.gain)
        elif isinstance(command, SetLPFFreqCommand):
            self.qcm.setOscConfig(command.oscillator_idx, lpf_freq=command.freq)
        elif isinstance(command, SetInvertedCommand):
            self.qcm.setOscConfig(command.oscillator_idx, inverted=command.inverted)
        elif isinstance(command, SetOutputModeCommand):
            self.qcm.setOutputMode(command.mode.value)
        elif isinstance(command, SetLockDetectCommand):
            self.qcm.setLockDetect(command.amp_threshold, command.phase_tolerance)
        elif isinstance(command, SetSensorParamsCommand):
            self.qcm.setSensorParams(command.mass_sensitivity, command.sens_area, command.freq_virgin)
        elif isinstance(command, SetCoefficientsCommand):
            self.qcm.setCoefficients(
                command.fM_0, command.fM_1, command.fM_2, command.fM_3,
                command.fT_0, command.fT_1, command.fT_2, command.fT_3,
            )
        else:
            raise ValueError(f"Unknown command type: {type(command)}")
        
        
        
    def _run_sweep(self, command: StartSweepCommand):
        self.qcm.standby(1)
        self.qcm.standby(2)
        # make sure the LPF cutoff is set to the default for sweeps to keep things consistent
        self.qcm.setLPFFreq(command.oscillator_idx, self.qcm.LPF_FREQ)
        n_points = int(math.floor((command.stop_freq - command.start_freq) / command.step_size)) + 1
        for i in range(n_points):
            # Check for abort between points
            try:
                cmd = self.command_queue.get_nowait()
                if isinstance(cmd, AbortSweepCommand):
                    self.event_queue.put(SweepCompleteEvent())
                    return
            except queue.Empty:
                pass

            freq = command.start_freq + i * command.step_size
            self.qcm.setFreq(command.oscillator_idx, freq)
            self.qcm.reset() # resets the PLL integratos, making sure the frequency is the desired one.
            time.sleep(command.settle_time)
            amplitude = self.qcm.getMag(command.oscillator_idx)
            phase = self.qcm.getPhase(command.oscillator_idx)
            self.event_queue.put(SweepPointEvent(frequency=freq, amplitude=amplitude, phase=phase))
        self.event_queue.put(SweepCompleteEvent())

    def update(self):
        # Emit lock status on change (immediate UI feedback) or as a periodic
        # heartbeat — not every loop iteration, which floods the WS clients.
        lock_mass = self.qcm.getLockDetect(1)
        lock_temp = self.qcm.getLockDetect(2)
        now = time.time()
        status = (lock_mass, lock_temp)
        if status != self._last_lock_status or now - self._last_lock_emit >= self.LOCK_STATUS_HEARTBEAT:
            self.event_queue.put(LockStatusEvent(lock_mass=lock_mass, lock_temp=lock_temp))
            self._last_lock_status = status
            self._last_lock_emit = now

        # Live amplitude feedback while adjusting the trim capacitor.
        if self.state == WorkerState.CAP_ADJUST:
            self.event_queue.put(CapAdjustEvent(amp_mass=self.qcm.getMag(1), amp_temp=self.qcm.getMag(2)))
            return

        # Perform measurement acquisition if in measuring state
        if self.state == WorkerState.MEASURING:
            # Automatic re-lock: if both modes stay unlocked past the threshold,
            # re-acquire around the last lock frequencies (blocks until done).
            if lock_mass and lock_temp:
                self._lock_lost_since = None
            elif self._lock_freqs is not None:
                if self._lock_lost_since is None:
                    self._lock_lost_since = now
                elif now - self._lock_lost_since >= self.AUTO_RELOCK_AFTER:
                    print("[QCM] Lock lost > 1 s — attempting automatic re-lock")
                    self._acquire_lock(*self._lock_freqs)
                    return  # state/timing changed during re-lock; resume next cycle

            data = self.qcm.getMeasurement()
            self.logger.write_measurement(data)  # durable on-disk record (WS-independent)
            self.event_queue.put(MeasurementEvent(data=data))
            # self.qcm.moveWindow(fM, fT)