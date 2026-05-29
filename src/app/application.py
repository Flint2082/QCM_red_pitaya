# Responsible for:
#
# Orchestration
# State updates
# Interpreting worker events and forwarding them up to the API layer
# Receiving commands from the API layer and forwarding them down to the worker
# Job tracking and coordination logic

import queue
import threading

import messaging.api_command as ac
import messaging.api_event as ae
import messaging.worker_command as wc
import messaging.worker_event as we
from messaging.defines import WorkerState

_STATE_MAP = {
    WorkerState.IDLE:        "IDLE",
    WorkerState.LOCKING:     "LOCKING",
    WorkerState.MEASURING:   "RUNNING",
    WorkerState.SWEEPING:    "SWEEPING",
    WorkerState.CALIBRATING: "IDLE",
}


class Application(threading.Thread):
    def __init__(
        self,
        # Queues facing the worker (below)
        worker_command_queue: queue.Queue,
        worker_event_queue: queue.Queue,
        # Queues facing the API layer (above)
        api_command_queue: queue.Queue,
        api_event_queue: queue.Queue,
        
        system_state=None,
    ):
        super().__init__(daemon=True, name="application")

        self.worker_command_queue = worker_command_queue
        self.worker_event_queue = worker_event_queue
        self.api_command_queue = api_command_queue
        self.api_event_queue = api_event_queue
        self.system_state = system_state
        self.running = True
        
        self.mass_mode_frequency = 5983000
        self.temp_mode_frequency = 6570000

    def stop(self):
        self.running = False

    # --------------------------------------------------
    # Main loop
    # --------------------------------------------------

    def run(self):
        print("Application started")

        while self.running:
            self._process_api_commands()
            self._process_worker_events()

        print("Application stopped")

    # --------------------------------------------------
    # Inbound: commands from API → forward to worker
    # --------------------------------------------------

    def _process_api_commands(self):
        try:
            while True:
                command = self.api_command_queue.get_nowait()
                self._handle_api_command(command)
        except queue.Empty:
            pass

    def _handle_api_command(self, command):
        """
        Translate API-layer commands into worker commands.
        Add any orchestration logic here before forwarding.
        """
        if isinstance(command, ac.StartMeasurementCommand):
            self.worker_command_queue.put(wc.StartMeasurementCommand(ambient_temp=command.ambient_temp))
        elif isinstance(command, ac.StopMeasurementCommand):
            self.worker_command_queue.put(wc.StopMeasurementCommand())
        elif isinstance(command, ac.StartupPLLCommand):
            self.worker_command_queue.put(wc.StartupPLLCommand(self.mass_mode_frequency, self.temp_mode_frequency))
        elif isinstance(command, ac.StartSweepCommand):
            self.worker_command_queue.put(wc.StartSweepCommand(command.oscillator_idx, command.start_freq, command.stop_freq, command.step_size, command.settle_time))
        elif isinstance(command, ac.SetFrequencyCommand):
            self.worker_command_queue.put(wc.SetFrequencyCommand(command.oscillator_idx, command.frequency))
        elif isinstance(command, ac.SetIntegratorGainCommand):
            self.worker_command_queue.put(wc.SetIntegratorGainCommand(command.oscillator_idx, command.gain))
        elif isinstance(command, ac.SetInvertedCommand):
            self.worker_command_queue.put(wc.SetInvertedCommand(command.oscillator_idx, command.inverted))
        elif isinstance(command, ac.SetIQGainCommand):
            self.worker_command_queue.put(wc.SetIQGainCommand(command.oscillator_idx, command.gain))
        elif isinstance(command, ac.SetOutputModeCommand):
            self.worker_command_queue.put(wc.SetOutputModeCommand(command.oscillator_idx, command.mode))

        else:
            print(f"[Application] Unknown command type: {type(command)}")

    # --------------------------------------------------
    # Inbound: events from worker → forward to API layer
    # --------------------------------------------------

    def _process_worker_events(self):
        try:
            while True:
                event = self.worker_event_queue.get_nowait()
                self._handle_worker_event(event)
        except queue.Empty:
            pass

    def _handle_worker_event(self, event):
        """
        React to worker events, update system state, then forward up to the API layer.
        Add any business logic here before forwarding.
        """
        if isinstance(event, we.StateEvent):
            self.api_event_queue.put(ae.StateEvent(state=_STATE_MAP.get(event.state, "IDLE")))

        elif isinstance(event, we.SweepPointEvent):
            self.api_event_queue.put(ae.SweepPointEvent(
                frequency=event.frequency, amplitude=event.amplitude, phase=event.phase))

        elif isinstance(event, we.SweepCompleteEvent):
            self.api_event_queue.put(ae.SweepCompleteEvent())

        elif isinstance(event, we.MeasurementEvent):
            if self.system_state:
                self.system_state.update(event)
            self.api_event_queue.put(ae.MeasurementEvent(data=event.data))

        elif isinstance(event, we.ErrorEvent):
            print(f"[Application] Worker error: {event.message}")
            self.api_event_queue.put(ae.ErrorEvent(event.message))

