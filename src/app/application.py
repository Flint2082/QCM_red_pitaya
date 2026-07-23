# Responsible for:
#
# Orchestration
# State updates
# Interpreting worker events and forwarding them up to the API layer
# Receiving commands from the API layer and forwarding them down to the worker
# Job tracking and coordination logic

import queue
import threading
import time

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
    WorkerState.CAP_ADJUST:  "CAP_ADJUST",
}


class Application(threading.Thread):
    def __init__(
        self,
        # Queues facing the worker (below)
        worker_command_queue: queue.Queue,
        worker_event_queue: queue.Queue,
        # Queues facing the REST API layer (above)
        api_command_queue: queue.Queue,
        api_event_queue: queue.Queue,
        # Queues facing the OPC-UA bridge (optional)
        opc_command_queue: queue.Queue | None = None,
        opc_event_queue:   queue.Queue | None = None,
        opc_status_queue:  queue.Queue | None = None,

        system_state=None,
    ):
        super().__init__(daemon=True, name="application")

        self.worker_command_queue = worker_command_queue
        self.worker_event_queue = worker_event_queue
        self._command_queues: list[queue.Queue] = [q for q in (api_command_queue, opc_command_queue) if q]
        self._event_queues:   list[queue.Queue] = [q for q in (api_event_queue,   opc_event_queue)   if q]
        self._opc_status_queue: queue.Queue | None = opc_status_queue
        self.system_state = system_state
        self.running = True

    def stop(self):
        self.running = False

    def _emit(self, event):
        for q in self._event_queues:
            q.put(event)

    # --------------------------------------------------
    # Main loop
    # --------------------------------------------------

    def run(self):
        import traceback
        print("Application started")

        while self.running:
            try:
                did_work = self._process_api_commands()
                did_work |= self._process_worker_events()
                did_work |= self._process_opc_status()
                # Sleep briefly when idle — without this the loop busy-spins at
                # 100% CPU, starving the worker and the uvicorn event loop (GIL
                # contention), which makes the UI laggy and drops WebSockets.
                if not did_work:
                    time.sleep(0.005)
            except Exception:
                print(f"[Application] Unhandled exception:\n{traceback.format_exc()}")

        print("Application stopped")

    # --------------------------------------------------
    # Inbound: OPC status events → forward to REST/WS clients
    # --------------------------------------------------

    def _process_opc_status(self) -> bool:
        if self._opc_status_queue is None:
            return False
        did_work = False
        try:
            while True:
                self._emit(self._opc_status_queue.get_nowait())
                did_work = True
        except queue.Empty:
            pass
        return did_work

    # --------------------------------------------------
    # Inbound: commands from API → forward to worker
    # --------------------------------------------------

    def _process_api_commands(self) -> bool:
        did_work = False
        for q in self._command_queues:
            try:
                while True:
                    self._handle_api_command(q.get_nowait())
                    did_work = True
            except queue.Empty:
                pass
        return did_work

    def _handle_api_command(self, command):
        """
        Translate API-layer commands into worker commands.
        Add any orchestration logic here before forwarding.
        """
        if isinstance(command, ac.StartMeasurementCommand):
            self.worker_command_queue.put(wc.StartMeasurementCommand(
                ambient_temp=command.ambient_temp, mat_dens=command.mat_dens, z_ratio=command.z_ratio))
        elif isinstance(command, ac.StopMeasurementCommand):
            self.worker_command_queue.put(wc.StopMeasurementCommand())
        elif isinstance(command, ac.StartupPLLCommand):
            self.worker_command_queue.put(wc.StartupPLLCommand(command.start_freq_mass, command.start_freq_temp))
        elif isinstance(command, ac.StartSweepCommand):
            self.worker_command_queue.put(wc.StartSweepCommand(command.oscillator_idx, command.start_freq, command.stop_freq, command.step_size, command.settle_time))
        elif isinstance(command, ac.AbortSweepCommand):
            self.worker_command_queue.put(wc.AbortSweepCommand())
        elif isinstance(command, ac.SetFrequencyCommand):
            self.worker_command_queue.put(wc.SetFrequencyCommand(command.oscillator_idx, command.frequency))
        elif isinstance(command, ac.SetIntegratorGainCommand):
            self.worker_command_queue.put(wc.SetIntegratorGainCommand(command.oscillator_idx, command.gain))
        elif isinstance(command, ac.SetProportionalGainCommand):
            self.worker_command_queue.put(wc.SetProportionalGainCommand(command.oscillator_idx, command.gain))
        elif isinstance(command, ac.SetInvertedCommand):
            self.worker_command_queue.put(wc.SetInvertedCommand(command.oscillator_idx, command.inverted))
        elif isinstance(command, ac.SetPhaseDetectCommand):
            self.worker_command_queue.put(wc.SetPhaseDetectCommand(command.oscillator_idx, command.mode))
        elif isinstance(command, ac.SetLPFFreqCommand):
            self.worker_command_queue.put(wc.SetLPFFreqCommand(command.oscillator_idx, command.freq))
        elif isinstance(command, ac.SetOutputModeCommand):
            self.worker_command_queue.put(wc.SetOutputModeCommand(command.oscillator_idx, command.mode))
        elif isinstance(command, ac.SetLockDetectCommand):
            self.worker_command_queue.put(wc.SetLockDetectCommand(command.amp_threshold, command.phase_tolerance))
        elif isinstance(command, ac.SetSensorParamsCommand):
            self.worker_command_queue.put(wc.SetSensorParamsCommand(command.mass_sensitivity, command.sens_area, command.freq_virgin))
        elif isinstance(command, ac.StartCapAdjustCommand):
            self.worker_command_queue.put(wc.StartCapAdjustCommand(command.freq_mass, command.freq_temp))
        elif isinstance(command, ac.StopCapAdjustCommand):
            self.worker_command_queue.put(wc.StopCapAdjustCommand())
        elif isinstance(command, ac.SetCoefficientsCommand):
            self.worker_command_queue.put(wc.SetCoefficientsCommand(
                command.fM_0, command.fM_1, command.fM_2, command.fM_3,
                command.fT_0, command.fT_1, command.fT_2, command.fT_3,
            ))

        else:
            print(f"[Application] Unknown command type: {type(command)}")

    # --------------------------------------------------
    # Inbound: events from worker → forward to API layer
    # --------------------------------------------------

    def _process_worker_events(self) -> bool:
        did_work = False
        try:
            while True:
                event = self.worker_event_queue.get_nowait()
                self._handle_worker_event(event)
                did_work = True
        except queue.Empty:
            pass
        return did_work

    def _handle_worker_event(self, event):
        """
        React to worker events, update system state, then forward up to the API layer.
        Add any business logic here before forwarding.
        """
        if isinstance(event, we.StateEvent):
            self._emit(ae.StateEvent(state=_STATE_MAP.get(event.state, "IDLE")))

        elif isinstance(event, we.SweepPointEvent):
            self._emit(ae.SweepPointEvent(
                frequency=event.frequency, amplitude=event.amplitude, phase=event.phase))

        elif isinstance(event, we.SweepCompleteEvent):
            self._emit(ae.SweepCompleteEvent())

        elif isinstance(event, we.MeasurementEvent):
            if self.system_state:
                self.system_state.update(event)
            self._emit(ae.MeasurementEvent(data=event.data))

        elif isinstance(event, we.LockFailedEvent):
            print("[Application] PLL lock failed")
            self._emit(ae.LockFailedEvent())

        elif isinstance(event, we.LockStatusEvent):
            self._emit(ae.LockStatusEvent(lock_mass=event.lock_mass, lock_temp=event.lock_temp))

        elif isinstance(event, we.CapAdjustEvent):
            self._emit(ae.CapAdjustEvent(amp_mass=event.amp_mass, amp_temp=event.amp_temp))

        elif isinstance(event, we.StartFreqAutoUpdatedEvent):
            print(f"[Application] Auto-updated start freqs: mass={event.freq_mass:.0f} Hz, temp={event.freq_temp:.0f} Hz")
            self._emit(ae.StartFreqAutoUpdatedEvent(freq_mass=event.freq_mass, freq_temp=event.freq_temp))

        elif isinstance(event, we.ErrorEvent):
            print(f"[Application] Worker error: {event.message}")
            self._emit(ae.ErrorEvent(event.message))

