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

from messaging.defines import WorkerState
from messaging.worker_event import *
from messaging.worker_command import *

class QCMWorker(threading.Thread):
    def __init__(self, qcm, command_queue, event_queue):
        super().__init__()
        self.qcm = qcm
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.running = True
        self.state = WorkerState.IDLE
        
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

        print("QCM worker stopped")

    def _set_state(self, new_state: WorkerState):
        self.state = new_state
        self.event_queue.put(StateEvent(state=new_state))

    def handle_command(self, command):

        # ============================
        # Control commands
        # ============================

        if isinstance(command, StartupPLLCommand):
            self._set_state(WorkerState.LOCKING)
            self.qcm.startupPLL(command.start_freq_mass, command.start_freq_temp)
            self._set_state(WorkerState.IDLE)

        # Start measurement
        elif isinstance(command, StartMeasurementCommand) and self.state == WorkerState.IDLE:
            self.qcm.setMeasurementReference(T=command.ambient_temp)
            self._set_state(WorkerState.MEASURING)

        # Stop measurement
        elif isinstance(command, StopMeasurementCommand) and self.state == WorkerState.MEASURING:
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
            self.qcm.setInt(command.oscillator_idx, command.gain)
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
        # make sure IQ filter gain is set to the default for sweeps to keep things consistent
        self.qcm.setIQGain(command.oscillator_idx, self.qcm.IQ_GAIN)
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
            amplitude, phase = self.qcm.getAmpAndPhase(command.oscillator_idx)
            self.event_queue.put(SweepPointEvent(frequency=freq, amplitude=amplitude, phase=phase))
        self.event_queue.put(SweepCompleteEvent())

    def update(self):
        
        # Perform measurement acquisition if in measuring state
        if self.state == WorkerState.MEASURING:
            self.event_queue.put(MeasurementEvent(data=self.qcm.getMeasurement()))
            # self.qcm.moveWindow(fM, fT)