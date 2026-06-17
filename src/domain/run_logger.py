"""Server-side per-run CSV logger.

Writes every measurement to a CSV on the Red Pitaya as it is produced, so the
recording is independent of the browser/WebSocket (a WS drop, a browser refresh,
or closing the laptop no longer punches holes in the data). The column layout
matches the UI export exactly, so tools/csv_plotter.py reads both kinds of file.

All writes happen on the worker thread (the only caller), so no locking is
needed. Writes are flushed immediately and failures are swallowed with a warning
so logging can never interrupt the measurement loop.
"""

import csv
import os
import time
from datetime import datetime, timezone

# data/runs/ at the repo root (this file is src/domain/run_logger.py).
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "runs")

# Same columns/order as the UI export (see buildRunCsv in static/index.html).
HEADERS = [
    "timestamp_iso", "timestamp_s", "freq_mass_hz", "freq_temp_hz", "comp_mass_freq_hz",
    "thickness_comp_nm", "thickness_uncomp_nm", "temperature_c", "amp_mass", "phase_mass",
    "amp_temp", "phase_temp", "lock_mass", "lock_temp", "event_type", "event_detail",
]


class RunLogger:
    def __init__(self, directory: str = RUNS_DIR):
        self.directory = directory
        self._file = None
        self._writer = None
        self.path = None

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self) -> str | None:
        """Open a fresh timestamped CSV for a new run and write the header."""
        self.stop()  # defensive: never leak a previous handle
        try:
            os.makedirs(self.directory, exist_ok=True)
            name = "qcm_run_" + datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
            self.path = os.path.join(self.directory, name)
            self._file = open(self.path, "w", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=HEADERS)
            self._writer.writeheader()
            self._file.flush()
            print(f"[RunLogger] Logging run to {self.path}")
            return self.path
        except Exception as e:
            print(f"[RunLogger] Failed to start log: {e}")
            self._file = self._writer = self.path = None
            return None

    def write_measurement(self, data) -> None:
        if self._writer is None:
            return
        try:
            ts = data.timestamp
            self._writer.writerow({
                "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "timestamp_s": ts,
                "freq_mass_hz": data.freq_mass_mode,
                "freq_temp_hz": data.freq_temp_mode,
                "comp_mass_freq_hz": data.compensated_freq,
                "thickness_comp_nm": data.calculated_thickness,
                "thickness_uncomp_nm": data.uncompensated_thickness,
                "temperature_c": data.calculated_temp,
                "amp_mass": data.amp_mass, "phase_mass": data.phase_mass,
                "amp_temp": data.amp_temp, "phase_temp": data.phase_temp,
                "lock_mass": 1 if data.lock_mass else 0,
                "lock_temp": 1 if data.lock_temp else 0,
                "event_type": "", "event_detail": "",
            })
            self._file.flush()
        except Exception as e:
            print(f"[RunLogger] Measurement write failed: {e}")

    def write_event(self, event_type: str, detail: str = "") -> None:
        if self._writer is None:
            return
        try:
            ts = time.time()
            row = {h: "" for h in HEADERS}
            row.update({
                "timestamp_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "timestamp_s": ts, "event_type": event_type, "event_detail": detail,
            })
            self._writer.writerow(row)
            self._file.flush()
        except Exception as e:
            print(f"[RunLogger] Event write failed: {e}")

    def stop(self) -> str | None:
        """Close the current run file (if any) and return its path."""
        path = self.path
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        self._file = self._writer = None
        self.path = None
        return path
