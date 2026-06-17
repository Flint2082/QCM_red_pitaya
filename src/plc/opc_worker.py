# Responsible for:
#
# Bridging the QCM system to the WAGO PLC over OPC-UA
# Writing measurement results to PLC READ nodes after each measurement
# Subscribing to PLC CTRL nodes for reliable edge detection (push, not poll)
# Polling PLC SET nodes and caching configuration values
# Emitting OpcStatusEvent to the status_queue on connect/disconnect and settings changes

import queue
import threading
import time

from domain.measurement import MeasurementData
from messaging.api_command import (
    StartMeasurementCommand,
    StopMeasurementCommand,
    StartupPLLCommand,
    SetFrequencyCommand,
)
from messaging.api_event import MeasurementEvent, OpcStatusEvent, StateEvent
from plc.wago_client import WagoClient

# --------------------------------------------------
# OPC-UA node key definitions
# --------------------------------------------------

# Keys written by this system → PLC reads them
_READ_KEYS = [
    "GVL_QCM.READ.MassFrequency",
    "GVL_QCM.READ.TempFrequency",
    "GVL_QCM.READ.MassAmplitude",
    "GVL_QCM.READ.TempAmplitude",
    "GVL_QCM.READ.Temperature",
    "GVL_QCM.READ.CompensatedThickness",
    "GVL_QCM.READ.UncompensatedThickness",
    "GVL_QCM.READ.CompensatedRate",           # stub – not yet in MeasurementData
    "GVL_QCM.READ.UncompensatedRate",          # stub – not yet in MeasurementData
    "GVL_QCM.READ.CompensatedMassFrequency",
    "GVL_QCM.READ.Timestamp",
    "GVL_QCM.READ.ErrorCode",
]

# Keys subscribed by this system — PLC writes them as control signals.
# Subscription means the server pushes a notification on every set_value(),
# so even a very short pulse is guaranteed to be delivered.
_CTRL_KEYS = [
    "GVL_QCM.CTRL.StartMeasurement",
    "GVL_QCM.CTRL.StopMeasurement",
    "GVL_QCM.CTRL.GetLock",
    "GVL_QCM.CTRL.SetZero",   # stub – not yet handled
    "GVL_QCM.CTRL.Sweep",     # stub – not yet handled
]

# Keys written by PLC → this system reads them as configuration (still polled)
_SET_KEYS = [
    "GVL_QCM.SET.AmbientTemp",
    "GVL_QCM.SET.StartFreqMass",
    "GVL_QCM.SET.StartFreqTemp",
    "GVL_QCM.SET.Density",      # stub – no corresponding command yet
    # GVL_QCM.SET.Z-ratio: not yet implemented on PLC backend
    # GVL_QCM.SET.Coeffecients: array type, requires separate handling
]

_RECONNECT_INTERVAL = 10.0   # seconds between reconnect attempts
_POLL_INTERVAL      =  0.5   # seconds between SET-node polls
_SUBSCRIPTION_MS    =  100   # OPC-UA subscription publishing interval (ms)


def _build_measurement_payload(data: MeasurementData) -> dict:
    return {
        "GVL_QCM.READ.MassFrequency":            float(data.freq_mass_mode),
        "GVL_QCM.READ.TempFrequency":            float(data.freq_temp_mode),
        "GVL_QCM.READ.MassAmplitude":            float(data.amp_mass),
        "GVL_QCM.READ.TempAmplitude":            float(data.amp_temp),
        "GVL_QCM.READ.Temperature":              float(data.calculated_temp),
        "GVL_QCM.READ.CompensatedThickness":     float(data.calculated_thickness),
        "GVL_QCM.READ.UncompensatedThickness":   float(data.uncompensated_thickness),
        "GVL_QCM.READ.CompensatedRate":          0.0,   # stub
        "GVL_QCM.READ.UncompensatedRate":        0.0,   # stub
        "GVL_QCM.READ.CompensatedMassFrequency": float(data.compensated_freq),
        "GVL_QCM.READ.Timestamp":                int(data.timestamp),
        "GVL_QCM.READ.ErrorCode":                "",
        "GVL_QCM.READ.LockMass":                 bool(data.lock_mass),
        "GVL_QCM.READ.LockTemp":                 bool(data.lock_temp),
    }


# --------------------------------------------------
# OPC-UA subscription handler (runs in library's internal thread)
# --------------------------------------------------

class _CtrlSubscriptionHandler:
    """
    Receives push notifications from the OPC-UA server whenever a CTRL node
    changes.  This runs in python-opcua's internal event thread, so only
    thread-safe operations are performed here (queue.put is safe).
    """

    def __init__(self, worker: "OPCUAWorker"):
        self._worker = worker

    def datachange_notification(self, node, val, data):
        if not val:
            return  # only act on rising edge (False → True)
        try:
            # node.nodeid.Identifier is the full string path for string NodeIds
            nid: str = node.nodeid.Identifier
        except Exception:
            nid = str(node.nodeid)

        if "StartMeasurement" in nid:
            ambient = float(self._worker._settings.get("GVL_QCM.SET.AmbientTemp", 20.0))
            self._worker.command_queue.put(StartMeasurementCommand(ambient_temp=ambient))
            print(f"[OPCUA] StartMeasurement (subscription, ambient={ambient}°C)")

        elif "StopMeasurement" in nid:
            self._worker.command_queue.put(StopMeasurementCommand())
            print("[OPCUA] StopMeasurement (subscription)")

        elif "GetLock" in nid:
            mass = float(self._worker._settings.get("GVL_QCM.SET.StartFreqMass", 5983000.0))
            temp = float(self._worker._settings.get("GVL_QCM.SET.StartFreqTemp", 6570000.0))
            self._worker.command_queue.put(StartupPLLCommand(start_freq_mass=mass, start_freq_temp=temp))
            print(f"[OPCUA] GetLock (subscription, mass={mass:.0f} Hz, temp={temp:.0f} Hz)")

        # SetZero / Sweep: stubs — add handling here when implemented

    def status_change_notification(self, status):
        print(f"[OPCUA] Subscription status changed: {status}")


# --------------------------------------------------
# Worker thread
# --------------------------------------------------

class OPCUAWorker(threading.Thread):
    def __init__(
        self,
        client: WagoClient,
        command_queue: queue.Queue,
        event_queue: queue.Queue,
        status_queue: queue.Queue,
    ):
        super().__init__(daemon=True, name="opcua-worker")
        self.client = client
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.status_queue = status_queue
        self.running = True

        self._settings: dict[str, object] = {}
        self._last_poll = 0.0
        self._last_reconnect = 0.0
        self._was_connected: bool | None = None  # None = not yet emitted
        self._subscription = None   # active OPC-UA subscription object

    # --------------------------------------------------
    # Thread lifecycle
    # --------------------------------------------------

    def stop(self):
        self.running = False

    def run(self):
        print("[OPCUA] Worker started")
        self._emit_status()

        if self.client.is_connected:
            self._setup_ctrl_subscription()

        while self.running:
            self._process_events()

            now = time.time()
            if not self.client.is_connected:
                self._teardown_ctrl_subscription()
                if now - self._last_reconnect >= _RECONNECT_INTERVAL:
                    self._last_reconnect = now
                    connected = self.client.reconnect()
                    if connected:
                        self._setup_ctrl_subscription()
                        self._emit_status()
                    elif self._was_connected is not False:
                        self._was_connected = False
                        self._emit_status()
            elif now - self._last_poll >= _POLL_INTERVAL:
                self._last_poll = now
                self._poll()

            time.sleep(0.02)

        self._teardown_ctrl_subscription()
        self.client.disconnect()
        self._emit_status()
        print("[OPCUA] Worker stopped")

    # --------------------------------------------------
    # CTRL subscription management
    # --------------------------------------------------

    def _setup_ctrl_subscription(self):
        """Subscribe to all CTRL nodes so the server pushes changes immediately."""
        self._teardown_ctrl_subscription()
        try:
            handler = _CtrlSubscriptionHandler(self)
            sub = self.client.client.create_subscription(_SUBSCRIPTION_MS, handler)
            nodes = []
            for key in _CTRL_KEYS:
                node_id = self.client.build_node_id(key)
                if node_id:
                    nodes.append(self.client.client.get_node(node_id))
            if nodes:
                sub.subscribe_data_change(nodes)
                self._subscription = sub
                print(f"[OPCUA] Subscribed to {len(nodes)} CTRL nodes")
            else:
                print("[OPCUA] No CTRL nodes found to subscribe to")
        except Exception as e:
            print(f"[OPCUA] Subscription setup failed (falling back to polling): {e}")
            self._subscription = None

    def _teardown_ctrl_subscription(self):
        if self._subscription is None:
            return
        # Only actively delete the subscription while still connected. If the
        # connection already dropped, WagoClient._drop_connection() has called
        # the library's disconnect(), which cleans the subscription up
        # server-side. Calling delete() over the dead socket would only spawn a
        # noisy CancelledError traceback from python-opcua's async layer.
        if self.client.is_connected:
            try:
                self._subscription.delete()
            except Exception:
                pass
        self._subscription = None

    # --------------------------------------------------
    # Status events → status_queue
    # --------------------------------------------------

    def _emit_status(self):
        connected = self.client.is_connected
        self._was_connected = connected
        self.status_queue.put(OpcStatusEvent(
            connected=connected,
            ambient_temp=self._settings.get("GVL_QCM.SET.AmbientTemp"),
            start_freq_mass=self._settings.get("GVL_QCM.SET.StartFreqMass"),
            start_freq_temp=self._settings.get("GVL_QCM.SET.StartFreqTemp"),
            density=self._settings.get("GVL_QCM.SET.Density"),
        ))

    # --------------------------------------------------
    # Event queue → PLC writes
    # --------------------------------------------------

    def _process_events(self):
        while True:
            try:
                event = self.event_queue.get_nowait()
                if isinstance(event, MeasurementEvent):
                    self._write_measurement(event.data)
                elif isinstance(event, StateEvent):
                    self._write_status(event.state)
            except queue.Empty:
                break

    def _write_status(self, state: str):
        if not self.client.is_connected:
            return
        self.client.write_by_key("GVL_QCM.READ.Status", state)

    def _write_measurement(self, data: MeasurementData):
        if not self.client.is_connected:
            return
        payload = _build_measurement_payload(data)
        if not self.client.batch_write_by_keys(payload):
            print("[OPCUA] Measurement write failed — will retry on reconnect")

    # --------------------------------------------------
    # PLC polls → command queue / status queue
    # --------------------------------------------------

    def _poll(self):
        self._poll_settings()
        # CTRL nodes are handled by subscription; poll only as fallback
        if self._subscription is None:
            self._poll_ctrl_fallback()

    def _poll_settings(self):
        result = self.client.batch_read_by_keys(_SET_KEYS)
        if result is None:
            return

        prev_mass = self._settings.get("GVL_QCM.SET.StartFreqMass")
        prev_temp = self._settings.get("GVL_QCM.SET.StartFreqTemp")
        prev_snapshot = dict(self._settings)

        self._settings.update({k: v for k, v in result.items() if v is not None})

        new_mass = self._settings.get("GVL_QCM.SET.StartFreqMass")
        new_temp = self._settings.get("GVL_QCM.SET.StartFreqTemp")
        if new_mass is not None and new_mass != prev_mass:
            self.command_queue.put(SetFrequencyCommand(oscillator_idx=1, frequency=float(new_mass)))
        if new_temp is not None and new_temp != prev_temp:
            self.command_queue.put(SetFrequencyCommand(oscillator_idx=2, frequency=float(new_temp)))

        if self._settings != prev_snapshot:
            self._emit_status()

    def _poll_ctrl_fallback(self):
        """Edge-detection polling used only when subscription setup failed."""
        result = self.client.batch_read_by_keys(_CTRL_KEYS)
        if result is None:
            return

        start = bool(result.get("GVL_QCM.CTRL.StartMeasurement", False))
        stop  = bool(result.get("GVL_QCM.CTRL.StopMeasurement", False))

        prev_start = bool(self._settings.get("_ctrl_start", False))
        prev_stop  = bool(self._settings.get("_ctrl_stop",  False))

        lock = bool(result.get("GVL_QCM.CTRL.GetLock", False))
        prev_lock = bool(self._settings.get("_ctrl_lock", False))

        if start and not prev_start:
            ambient = float(self._settings.get("GVL_QCM.SET.AmbientTemp", 20.0))
            self.command_queue.put(StartMeasurementCommand(ambient_temp=ambient))
            print("[OPCUA] StartMeasurement (poll fallback)")
        if stop and not prev_stop:
            self.command_queue.put(StopMeasurementCommand())
            print("[OPCUA] StopMeasurement (poll fallback)")
        if lock and not prev_lock:
            mass = float(self._settings.get("GVL_QCM.SET.StartFreqMass", 5983000.0))
            temp = float(self._settings.get("GVL_QCM.SET.StartFreqTemp", 6570000.0))
            self.command_queue.put(StartupPLLCommand(start_freq_mass=mass, start_freq_temp=temp))
            print("[OPCUA] GetLock (poll fallback)")

        self._settings["_ctrl_start"] = start
        self._settings["_ctrl_stop"]  = stop
        self._settings["_ctrl_lock"]  = lock
