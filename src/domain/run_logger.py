"""Server-side per-run CSV logger.

Writes every measurement to a CSV on the Red Pitaya as it is produced, so the
recording is independent of the browser/WebSocket (a WS drop, a browser refresh,
or closing the laptop no longer punches holes in the data). It is the only copy
of a run — "SAVE CSV" in the UI downloads this file back, it does not build one
from what the browser received.

Each run also records the settings it was acquired with as a single SETTINGS
event row (JSON in event_detail) — see write_settings.

All writes happen on the worker thread (the only caller), so no locking is
needed. Writes are flushed immediately, and a failure never interrupts the
measurement loop — but it is *reported*, via the on_error callback, rather than
swallowed: a run that silently fails to record looks identical to a healthy one
in the live UI (plotting comes over the WebSocket, not from this file), which is
how a full SD card once cost two runs' worth of data.
"""

import csv
import json
import os
import shutil
import time
from datetime import datetime, timezone

# data/runs/ at the repo root (this file is src/domain/run_logger.py).
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "runs")

# Read by tools/csv_plotter.py. This file is the only export — the browser keeps
# no copy of a run, it just downloads this one back via /runs/{name}/download.
HEADERS = [
    "timestamp_iso", "timestamp_s", "freq_mass_hz", "freq_temp_hz", "comp_mass_freq_hz",
    "thickness_comp_nm", "thickness_uncomp_nm", "temperature_c", "amp_mass", "phase_mass",
    "amp_temp", "phase_temp", "lock_mass", "lock_temp", "quality_mass", "quality_temp",
    "event_type", "event_detail",
]

# A run writes one ~170-byte row per measurement cycle, roughly 1 kB/s or 100 MB
# per day. Refuse to open a log below MIN_FREE (about a day of headroom) and warn
# below WARN_FREE, so a filling card is noticed before it swallows a run.
MIN_FREE_BYTES = 100 * 1024 ** 2
WARN_FREE_BYTES = 500 * 1024 ** 2

# Upper bound on a run file that contains nothing but its header row.
_HEADER_BYTES = len(",".join(HEADERS)) + 2  # + CRLF


def _mb(n: int) -> int:
    return int(n / 1024 ** 2)


class RunLogger:
    """on_error(reason) is called once per run when recording is unavailable or
    stops; on_start(filename) when a run log is successfully opened. Both are
    invoked on the worker thread — keep them to a queue.put()."""

    def __init__(self, directory: str = RUNS_DIR, on_error=None, on_start=None):
        self.directory = directory
        self._on_error = on_error
        self._on_start = on_start
        self._file = None
        self._writer = None
        self.path = None
        self.last_error = None  # reason recording is off, or None while healthy

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self) -> str | None:
        """Open a fresh timestamped CSV for a new run and write the header.

        Returns the path, or None if this run cannot be recorded — in which case
        last_error says why and on_error has fired. The measurement itself is
        never blocked; the operator is told instead.
        """
        self.stop()  # defensive: never leak a previous handle
        self.last_error = None
        path = None
        try:
            os.makedirs(self.directory, exist_ok=True)

            free = shutil.disk_usage(self.directory).free
            if free < MIN_FREE_BYTES:
                raise OSError(f"only {_mb(free)} MB free on the data partition "
                              f"(need {_mb(MIN_FREE_BYTES)} MB) — archive old runs")

            name = "qcm_run_" + datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".csv"
            path = os.path.join(self.directory, name)
            self._file = open(path, "w", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=HEADERS)
            self._writer.writeheader()
            self._file.flush()  # surface a full disk here, not silently mid-run
            self.path = path
            print(f"[RunLogger] Logging run to {path}")
            if free < WARN_FREE_BYTES:
                print(f"[RunLogger] WARNING: only {_mb(free)} MB free — "
                      f"archive old runs in {self.directory}")
            if self._on_start:
                self._on_start(name)
            return path
        except Exception as e:
            self._fail(f"this run is NOT being recorded: {e}", discard=path)
            return None

    def _fail(self, reason: str, discard: str | None = None) -> None:
        """Turn logging off for the rest of the run and report it once.

        Retrying every row would put one line per measurement into the journal
        and the UI log (which mirrors stdout), so the first failure stops the
        logging; whatever reached the disk before it stays intact.
        """
        self.last_error = reason
        self._close()
        # A run that failed to open holds a header at most — empty or header-only,
        # it is pure noise in the runs list, and being the newest it is exactly
        # what a "latest run" download picks up. The size bound is a guard: only
        # start() passes discard, so there is never data here to lose.
        if discard:
            try:
                if os.path.exists(discard) and os.path.getsize(discard) <= _HEADER_BYTES:
                    os.remove(discard)
            except OSError:
                pass
        print(f"[RunLogger] {reason}")
        if self._on_error:
            try:
                self._on_error(reason)
            except Exception as e:
                print(f"[RunLogger] Failed to report logging error: {e}")

    def _close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except Exception:
                pass
        self._file = self._writer = self.path = None

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
                # Phase-error std (rad) the lock decision was made on — blank
                # until the window fills, so a lock flag can be second-guessed
                # against the evidence behind it after the fact.
                "quality_mass": "" if data.quality_mass is None else data.quality_mass,
                "quality_temp": "" if data.quality_temp is None else data.quality_temp,
                "event_type": "", "event_detail": "",
            })
            self._file.flush()
        except Exception as e:
            self._fail(f"recording stopped after a write error: {e}")

    def write_settings(self, settings: dict) -> None:
        """Record the settings a run was acquired with as a single SETTINGS event
        row, with the nested dict JSON-encoded into event_detail. Deliberately an
        event row rather than new columns or a header block, so the layout is
        unchanged and existing readers keep working."""
        try:
            self.write_event("SETTINGS", json.dumps(settings, sort_keys=True, separators=(",", ":")))
        except Exception as e:
            print(f"[RunLogger] Settings write failed: {e}")

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
            self._fail(f"recording stopped after a write error: {e}")

    def stop(self) -> str | None:
        """Close the current run file (if any) and return its path."""
        path = self.path
        self._close()
        return path
