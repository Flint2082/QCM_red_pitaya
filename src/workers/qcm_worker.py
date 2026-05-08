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

from messaging.worker_event import *
from messaging.worker_command import *  

class QCMWorker(threading.Thread):
    def __init__(self, qcm, command_queue, event_queue):
        super().__init__()
        self.qcm = qcm
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.running = True
        
    def stop(self):
        self.running = False
        self.command_queue.put(None) # unblock queue.get()

    def run(self):
        print("QCM worker started")

        while self.running:
            try:
                command = self.command_queue.get()

                if command is None:
                    continue

                self.handle_command(command)

            except Exception as e:
                self.event_queue.put(
                    ErrorEvent(str(e))
                )

        print("QCM worker stopped")

    def handle_command(self, command):

        # ============================
        # Control commands
        # ============================
        
        if isinstance(command, StartMeasurementCommand):
            self.qcm.start_measurement()
        elif isinstance(command, StopMeasurementCommand):
            self.qcm.stop_measurement()
        elif isinstance(command, StartSweepCommand):
            self.qcm.start_sweep(command.start_freq, command.stop_freq, command.step_size, command.settle_time)

        # ============================
        # Setting commands
        # ============================    
            
        elif isinstance(command, SetFrequencyCommand):
            self.qcm.set_frequency(command.oscillator_idx, command.frequency)
        elif isinstance(command, SetIntegratorGainCommand):
            self.qcm.set_integrator_gain(command.oscillator_idx, command.gain)
        else:
            raise ValueError(f"Unknown command type: {type(command)}")