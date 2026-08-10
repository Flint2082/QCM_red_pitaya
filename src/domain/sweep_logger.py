"""Server-side per-sweep CSV logger.

The sweep counterpart to RunLogger: every point is written to a CSV on the Red
Pitaya as it is taken, so a sweep survives a WebSocket drop or a browser refresh
and can be downloaded afterwards (/sweeps/{name}/download) or plotted with
tools/sweep_plotter.py.

A sweep is small — a 20 kHz window at 100 Hz is 201 rows, some 8 kB — so unlike
a run it is never the thing that fills the card. It still refuses to open below
the same free-space floor, because writing into a full partition is what breaks
the *run* logs.

All writes happen on the worker thread (the only caller). A failure disables
logging for the rest of the sweep and is printed, but never interrupts the
sweep itself: the points still stream to the UI over the WebSocket.
"""

import csv
import json
import os
import shutil
from datetime import datetime, timezone

from domain.run_logger import MIN_FREE_BYTES, _mb

# data/sweeps/ at the repo root (this file is src/domain/sweep_logger.py).
SWEEPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "sweeps")

# Read by tools/sweep_plotter.py. The SETTINGS row carries the sweep parameters
# as JSON in event_detail, mirroring the run-log layout so both files parse the
# same way.
HEADERS = [
    "timestamp_iso", "timestamp_s", "frequency_hz", "amplitude", "phase",
    "event_type", "event_detail",
]


class SweepLogger:
    """on_start(filename) fires when a sweep log is opened; it is invoked on the
    worker thread — keep it to a queue.put()."""

    def __init__(self, directory: str = SWEEPS_DIR, on_start=None):
        self.directory = directory
        self._on_start = on_start
        self._file = None
        self._writer = None
        self.path = None
        self.name = None        # basename of the open file, for the download link
        self.last_error = None  # why this sweep is not being recorded, or None

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, params: dict) -> str | None:
        """Open a fresh timestamped CSV and write the header plus the sweep
        parameters. Returns the path, or None if this sweep cannot be recorded."""
        self.stop()  # defensive: never leak a previous handle
        self.last_error = None
        try:
            os.makedirs(self.directory, exist_ok=True)

            free = shutil.disk_usage(self.directory).free
            if free < MIN_FREE_BYTES:
                raise OSError(f"only {_mb(free)} MB free on the data partition "
                              f"(need {_mb(MIN_FREE_BYTES)} MB) — archive old runs")

            name = "qcm_sweep_" + datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
            path = os.path.join(self.directory, name)
            self._file = open(path, "w", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=HEADERS)
            self._writer.writeheader()
            self._file.flush()  # surface a full disk here, not silently mid-sweep
            self.path, self.name = path, name
            self.write_event("SETTINGS", json.dumps(params, sort_keys=True, separators=(",", ":")))
            print(f"[SweepLogger] Logging sweep to {path}")
            if self._on_start:
                self._on_start(name)
            return path
        except Exception as e:
            self.last_error = f"this sweep is NOT being recorded: {e}"
            print(f"[SweepLogger] {self.last_error}")
            self._close()
            return None

    def _close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        self._file = self._writer = self.path = None

    def _fail(self, reason: str) -> None:
        """Stop recording this sweep and say so once — retrying every point would
        put one line per step into the journal and the UI log."""
        self.last_error = reason
        self._close()
        print(f"[SweepLogger] {reason}")

    def write_point(self, frequency: float, amplitude: float, phase: float, timestamp: float) -> None:
        if self._writer is None:
            return
        try:
            self._writer.writerow({
                "timestamp_iso": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                "timestamp_s": timestamp,
                "frequency_hz": frequency,
                "amplitude": amplitude,
                "phase": phase,
                "event_type": "", "event_detail": "",
            })
            self._file.flush()
        except Exception as e:
            self._fail(f"recording stopped after a write error: {e}")

    def write_event(self, event_type: str, detail: str = "") -> None:
        if self._writer is None:
            return
        try:
            ts = datetime.now(timezone.utc)
            row = {h: "" for h in HEADERS}
            row.update({
                "timestamp_iso": ts.isoformat(), "timestamp_s": ts.timestamp(),
                "event_type": event_type, "event_detail": detail,
            })
            self._writer.writerow(row)
            self._file.flush()
        except Exception as e:
            self._fail(f"recording stopped after a write error: {e}")

    def stop(self) -> str | None:
        """Close the current sweep file (if any) and return its basename."""
        name = self.name
        self._close()
        self.name = None
        return name
