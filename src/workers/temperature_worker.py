# Responsible for:
#
# owning the LTC2986 temperature sensors (domain/temperature_sensors.py)
# reading them on a fixed interval, independent of the QCM worker
# reporting each read, or why there is none, as a TemperatureEvent
#
# A thread of its own because a round of conversions takes most of a second,
# which the QCM worker's 10 Hz loop cannot spare. Nothing else touches the SPI
# bus, so no locking is needed.

import queue
import threading
import time

from domain.temperature_sensors import TemperatureSensors
from messaging.api_event import TemperatureEvent


class TemperatureWorker(threading.Thread):
    READ_INTERVAL  = 1.0    # s between the starts of two rounds of conversions
    RETRY_INTERVAL = 10.0   # s between attempts to bring an absent chip up

    def __init__(self, event_queue: queue.Queue, open_sensors=TemperatureSensors.open):
        super().__init__(daemon=True, name="temperature")
        self.event_queue = event_queue
        self._open_sensors = open_sensors   # replaceable, to run against a fake chip
        self._stop_event = threading.Event()
        self._sensors = None
        self._last_error: str | None = None
        self._last_faults: dict[str, list[str]] = {}

    def stop(self):
        self._stop_event.set()

    def run(self):
        print("Temperature worker started")
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                self._read_once()
                wait = self.READ_INTERVAL - (time.monotonic() - started)
            except Exception as e:
                self._fail(str(e) or type(e).__name__)
                wait = self.RETRY_INTERVAL
            self._stop_event.wait(max(0.0, wait))
        if self._sensors:
            self._sensors.close()
        print("Temperature worker stopped")

    def _read_once(self):
        if self._sensors is None:
            self._sensors = self._open_sensors()
            print("[TEMP] LTC2986 configured")
        elif not self._sensors.configured():
            # Its configuration lives in RAM: the board lost power and came back.
            print("[TEMP] LTC2986 lost its configuration — reprogramming it")
            self._sensors.configure()
        readings = self._sensors.read_all()
        self._last_error = None
        self._log_fault_changes(readings)
        self.event_queue.put(TemperatureEvent(available=True, sensors=readings,
                                              timestamp=time.time()))

    def _fail(self, reason: str):
        if self._sensors:
            try:
                self._sensors.close()
            except Exception:
                pass
            self._sensors = None
        # Said once per distinct problem: an instrument without the board would
        # otherwise repeat it every RETRY_INTERVAL for as long as it runs.
        if reason != self._last_error:
            print(f"[TEMP] Temperature sensors unavailable: {reason}")
            self._last_error = reason
        self._last_faults = {}
        self.event_queue.put(TemperatureEvent(available=False, error=reason,
                                              timestamp=time.time()))

    def _log_fault_changes(self, readings: list[dict]):
        for r in readings:
            faults = r["faults"]
            if faults != self._last_faults.get(r["key"], []):
                print(f"[TEMP] {r['name']}: {', '.join(faults) if faults else 'OK again'}")
            self._last_faults[r["key"]] = faults
