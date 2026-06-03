# Responsible for:
#
# Bridging the QCM system to the WAGO PLC over OPC-UA
# Writing measurement results to PLC READ nodes after each measurement
# Polling PLC CTRL nodes and emitting API commands on rising edges
# Polling PLC SET nodes and caching configuration values
# Emitting OpcStatusEvent to the status_queue on connect/disconnect and settings changes

import queue
import threading
import time

from domain.measurement import MeasurementData
from messaging.api_command import (
    StartMeasurementCommand,
    StopMeasurementCommand,
    SetFrequencyCommand,
)
from messaging.api_event import MeasurementEvent, OpcStatusEvent
from plc.wago_client import WagoClient

# --------------------------------------------------
# OPC-UA node key definitions
# --------------------------------------------------

# Keys written by this system → PLC reads them
_READ_KEYS = [
    "QCM.READ.MassFrequency",
    "QCM.READ.TempFrequency",
    "QCM.READ.MassModeAmplitude",
    "QCM.READ.TempModeAmplitude",
    "QCM.READ.Temperature",
    "QCM.READ.CompensatedThickness",
    "QCM.READ.UncompensatedThickness",
    "QCM.READ.CompensatedRate",           # stub – not yet in MeasurementData
    "QCM.READ.UncompensatedRate",          # stub – not yet in MeasurementData
    "QCM.READ.CompensatedMassFrequency",
    "QCM.READ.Timestamp",
    "QCM.READ.ErrorCode",
]

# Keys written by PLC → this system reads them as control signals
_CTRL_KEYS = [
    "QCM.CTRL.StartMeasurement",
    "QCM.CTRL.StopMeasurement",
]

# Keys written by PLC → this system reads them as configuration
_SET_KEYS = [
    "QCM.SET.AmbientTemp",
    "QCM.SET.StartFreqMass",
    "QCM.SET.StartFreqTemp",
    "QCM.SET.Density",      # stub – no corresponding command yet
    "QCM.SET.Z-ratio",      # stub – no corresponding command yet
    # QCM.SET.Coefficients omitted: array type, requires separate handling
]

_RECONNECT_INTERVAL = 10.0   # seconds between reconnect attempts
_POLL_INTERVAL      =  0.5   # seconds between PLC polls


def _build_measurement_payload(data: MeasurementData) -> dict:
    return {
        "QCM.READ.MassFrequency":            float(data.freq_mass_mode),
        "QCM.READ.TempFrequency":            float(data.freq_temp_mode),
        "QCM.READ.MassModeAmplitude":        float(data.amp_mass),
        "QCM.READ.TempModeAmplitude":        float(data.amp_temp),
        "QCM.READ.Temperature":              float(data.calculated_temp),
        "QCM.READ.CompensatedThickness":     float(data.calculated_thickness),
        "QCM.READ.UncompensatedThickness":   float(data.uncompensated_thickness),
        "QCM.READ.CompensatedRate":          0.0,   # stub
        "QCM.READ.UncompensatedRate":        0.0,   # stub
        "QCM.READ.CompensatedMassFrequency": float(data.compensated_freq),
        "QCM.READ.Timestamp":                int(data.timestamp),
        "QCM.READ.ErrorCode":                "",
        "QCM.READ.LockMass":                 bool(data.lock_mass),
        "QCM.READ.LockTemp":                 bool(data.lock_temp),
    }


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

        self._prev_ctrl: dict[str, bool] = {}
        self._settings: dict[str, object] = {}
        self._last_poll = 0.0
        self._last_reconnect = 0.0
        self._was_connected: bool | None = None  # None = not yet emitted

    # --------------------------------------------------
    # Thread lifecycle
    # --------------------------------------------------

    def stop(self):
        self.running = False

    def run(self):
        print("[OPCUA] Worker started")

        # Emit initial disconnected status immediately
        self._emit_status()

        while self.running:
            self._process_events()

            now = time.time()
            if not self.client.is_connected:
                if now - self._last_reconnect >= _RECONNECT_INTERVAL:
                    self._last_reconnect = now
                    connected = self.client.reconnect()
                    if connected:
                        self._emit_status()
                    elif self._was_connected is not False:
                        self._was_connected = False
                        self._emit_status()
            elif now - self._last_poll >= _POLL_INTERVAL:
                self._last_poll = now
                self._poll()

            time.sleep(0.02)

        self.client.disconnect()
        self._emit_status()
        print("[OPCUA] Worker stopped")

    # --------------------------------------------------
    # Status events → status_queue
    # --------------------------------------------------

    def _emit_status(self):
        connected = self.client.is_connected
        self._was_connected = connected
        self.status_queue.put(OpcStatusEvent(
            connected=connected,
            ambient_temp=self._settings.get("QCM.SET.AmbientTemp"),
            start_freq_mass=self._settings.get("QCM.SET.StartFreqMass"),
            start_freq_temp=self._settings.get("QCM.SET.StartFreqTemp"),
            density=self._settings.get("QCM.SET.Density"),
            z_ratio=self._settings.get("QCM.SET.Z-ratio"),
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
            except queue.Empty:
                break

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
        self._poll_ctrl()

    def _poll_settings(self):
        result = self.client.batch_read_by_keys(_SET_KEYS)
        if result is None:
            return

        prev_mass = self._settings.get("QCM.SET.StartFreqMass")
        prev_temp = self._settings.get("QCM.SET.StartFreqTemp")
        prev_settings_snapshot = dict(self._settings)

        self._settings.update({k: v for k, v in result.items() if v is not None})

        # Apply frequency changes immediately when the PLC updates them
        new_mass = self._settings.get("QCM.SET.StartFreqMass")
        new_temp = self._settings.get("QCM.SET.StartFreqTemp")
        if new_mass is not None and new_mass != prev_mass:
            self.command_queue.put(SetFrequencyCommand(oscillator_idx=1, frequency=float(new_mass)))
        if new_temp is not None and new_temp != prev_temp:
            self.command_queue.put(SetFrequencyCommand(oscillator_idx=2, frequency=float(new_temp)))

        # Emit a status update whenever any setting value changes
        if self._settings != prev_settings_snapshot:
            self._emit_status()

    def _poll_ctrl(self):
        result = self.client.batch_read_by_keys(_CTRL_KEYS)
        if result is None:
            return

        start = bool(result.get("QCM.CTRL.StartMeasurement", False))
        stop  = bool(result.get("QCM.CTRL.StopMeasurement", False))

        # Rising-edge detection: only emit command on 0→1 transition
        if start and not self._prev_ctrl.get("start", False):
            ambient_temp = float(self._settings.get("QCM.SET.AmbientTemp", 20.0))
            self.command_queue.put(StartMeasurementCommand(ambient_temp=ambient_temp))
            print(f"[OPCUA] StartMeasurement received (ambient={ambient_temp}°C)")

        if stop and not self._prev_ctrl.get("stop", False):
            self.command_queue.put(StopMeasurementCommand())
            print("[OPCUA] StopMeasurement received")

        self._prev_ctrl = {"start": start, "stop": stop}
