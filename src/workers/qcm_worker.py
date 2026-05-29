# Responsible for:

# executing commands
# owning hardware access
# running sweeps
# acquisition loops

# async 
# Listens for commands from application, executes them
# Sends events back to application (e.g. measurement complete, sweep step done, etc.)

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

    def handle_command(self, command):

        # ============================
        # Control commands
        # ============================
        
        if isinstance(command, StartupPLLCommand):
            self.qcm.startupPLL(command.start_freq_mass, command.start_freq_temp)
        
        # Start measurement
        elif isinstance(command, StartMeasurementCommand) and self.state == WorkerState.IDLE:
            self.state = WorkerState.MEASURING
            self.qcm.setMeasurementReference()
            
        # Stop measurement
        elif isinstance(command, StopMeasurementCommand) and self.state == WorkerState.MEASURING:
            self.state = WorkerState.IDLE
            # self.qcm.stop_measurement()
            
        # Sweep
        elif isinstance(command, StartSweepCommand) and self.state == WorkerState.IDLE:
            self.state = WorkerState.SWEEPING
            self.qcm.sweep(command.start_freq, command.stop_freq, command.step_size, command.settle_time)
            self.state = WorkerState.IDLE

        # ============================
        # Setting commands
        # ============================    
            
        elif isinstance(command, SetFrequencyCommand):
            self.qcm.setFreq(command.oscillator_idx, command.frequency)
        elif isinstance(command, SetIntegratorGainCommand):
            self.qcm.setInt(command.oscillator_idx, command.gain)
        else:
            raise ValueError(f"Unknown command type: {type(command)}")
        
        
        
    def update(self):
        
        # Perform measurement acquisition if in measuring state
        if self.state == WorkerState.MEASURING:
            fM, fT, T_calc, uncomp_thickness_nm, comp_thickness_nm, comp_m_freq, amp_mass, phase_mass, amp_temp, phase_temp = self.qcm.getMeasurement()
            self.event_queue.put(
                MeasurementEvent(
                    freq_mass_mode=fM,
                    freq_temp_mode=fT,
                    uncompensated_thickness=uncomp_thickness_nm,
                    calculated_thickness=comp_thickness_nm,
                    calculated_temp=T_calc,
                    compensated_freq=comp_m_freq,
                    amp_mass=amp_mass,
                    phase_mass=phase_mass,
                    amp_temp=amp_temp,
                    phase_temp=phase_temp,
                )
            )
            # self.qcm.moveWindow(fM, fT)