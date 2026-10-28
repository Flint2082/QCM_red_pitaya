"""Server-side per-run CSV logger.

Writes every measurement to a CSV on the Red Pitaya as it is produced, so the
recording is independent of the browser/WebSocket (a WS drop, a browser refresh,
or closing the laptop no longer punches holes in the data). It is the only copy
of a run — "SAVE CSV" in the UI downloads this file back, it does not build one
from what the browser received.

Each run also records the settings it was acquired with as a single SETTINGS
event row (JSON in event_detail) — see write_settings.

A run is one CSV until it passes SEGMENT_BYTES, after which it continues in a
numbered part ("<base>.p02.csv", then .p03.csv, ...). Every part repeats the
header and the run's SETTINGS row, so each one stands on its own; outside this
module a run is still identified by its base name, and the REST layer stitches
the parts back together on download (see run_base). Parts exist for the sake of
the reclaim below: dropping a whole part is instant, where cutting the oldest
rows out of the front of one multi-hundred-MB file means rewriting everything
after them — tens of seconds of solid SD I/O on the worker thread, in the middle
of a deposition.

When the data partition runs low the logger deletes CSVs to keep recording,
oldest first: other runs before this one, and this run's earlier parts before
the part being written, which is never deleted. Both halves are switchable
(set_reclaim); with both off the logger behaves as it always did and simply
stops recording once the disk is full. A deleted run cannot be recovered — this
file is the only copy — so every deletion is printed, reported to the UI through
on_reclaim, and written into the run log as a STORAGE_RECLAIM event row.

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
import re
import shutil
import time
from datetime import datetime, timezone

from domain.temperature_sensors import SENSORS

# data/runs/ at the repo root (this file is src/domain/run_logger.py).
RUNS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "runs")

# The LTC2986 sensors' readings in °C, one column per sensor: pt100_c,
# thermocouple_c, cold_junction_c. temperature_c is something else — the
# crystal temperature the compensation solves from the two modes.
TEMPERATURE_COLUMNS = [f"{s.key}_c" for s in SENSORS]

# Read by tools/csv_plotter.py. This file is the only export — the browser keeps
# no copy of a run, it just downloads this one back via /runs/{name}/download.
HEADERS = [
    "timestamp_iso", "timestamp_s", "freq_mass_hz", "freq_temp_hz", "comp_mass_freq_hz",
    "thickness_comp_nm", "thickness_uncomp_nm", "temperature_c", "amp_mass", "phase_mass",
    "amp_temp", "phase_temp", "lock_mass", "lock_temp", "quality_mass", "quality_temp",
    *TEMPERATURE_COLUMNS, "temp_sensor_faults",
    "event_type", "event_detail",
]

# A sensor reading older than this is not put beside a measurement. The sensors
# are read once a second, so it only trips when their worker has stopped.
TEMPERATURE_MAX_AGE = 5.0  # seconds

# A run writes one ~200-byte row per measurement cycle, roughly 1 kB/s or 100 MB
# per day. Refuse to open a log below MIN_FREE (about a day of headroom) and warn
# below WARN_FREE, so a filling card is noticed before it swallows a run.
MIN_FREE_BYTES = 100 * 1024 ** 2
WARN_FREE_BYTES = 500 * 1024 ** 2

# Size at which a run rolls over into its next part: ~18 hours of data, so an
# ordinary run is still a single file and only the multi-day ones are split.
# It is also the granularity of the reclaim — the oldest part of a run in
# progress is what gets dropped — so it doubles as the amount of history such a
# run is guaranteed to keep (the open part is never deleted).
SEGMENT_BYTES = 64 * 1024 ** 2

# Below RECLAIM_BELOW free bytes the logger starts deleting CSVs, and keeps going
# until RECLAIM_TARGET is free. The gap between the two is deliberate: reclaiming
# back to exactly the trigger would delete something on every single check.
RECLAIM_BELOW_BYTES = MIN_FREE_BYTES
RECLAIM_TARGET_BYTES = 2 * MIN_FREE_BYTES

# How often the housekeeping actually looks at the disk and the file size. A
# disk_usage() and a stat() per sample would be pure overhead at 10 Hz, and
# neither the free space nor the file size moves anywhere near that fast.
SPACE_CHECK_INTERVAL = 10.0   # seconds
_SIZE_CHECK_ROWS = 200        # samples between file-size checks (~20 s at 10 Hz)

# Upper bound on a run file that contains nothing but its header row.
_HEADER_BYTES = len(",".join(HEADERS)) + 2  # + CRLF

# "<base>.csv", "<base>.p02.csv", "<base>.p03.csv", ... The base name is the
# run's identity everywhere outside this module (the REST API, the UI, the
# download); the parts are an implementation detail of keeping the log trimmable.
_PART_RE = re.compile(r"^(?P<stem>.+?)(?:\.p(?P<part>\d{2,3}))?\.csv$")


def run_base(name: str) -> str:
    """The run a CSV belongs to: 'x.p03.csv' -> 'x.csv', 'x.csv' -> 'x.csv'.

    Note that the base name is a run identity, not a promise that the file
    exists: the first part is as reclaimable as any other once a run has rolled
    over, so callers must resolve a run to whichever of its parts are still on
    disk rather than assuming the base name is one of them.
    """
    m = _PART_RE.match(os.path.basename(name))
    return (m.group("stem") + ".csv") if m else os.path.basename(name)


def part_number(name: str) -> int:
    """Which part of its run a CSV is; the first part carries no suffix."""
    m = _PART_RE.match(os.path.basename(name))
    part = m.group("part") if m else None
    return int(part) if part else 1


def part_name(base: str, part: int) -> str:
    """Filename of part `part` of the run whose base name is `base`."""
    return base if part <= 1 else f"{base[:-len('.csv')]}.p{part:02d}.csv"


def _mb(n: int) -> int:
    return int(n / 1024 ** 2)


class RunLogger:
    """on_error(reason) is called once per run when recording is unavailable or
    stops; on_start(filename) when a run log is successfully opened — including
    each time the run rolls over into a new part; on_reclaim(summary, free_bytes)
    when CSVs were deleted to make room. All three are invoked on the worker
    thread — keep them to a queue.put().

    temperatures() returns the newest TemperatureEvent from the LTC2986 worker,
    or None; its readings go into the TEMPERATURE_COLUMNS of every row."""

    def __init__(self, directory: str = RUNS_DIR, on_error=None, on_start=None,
                 on_reclaim=None, temperatures=None):
        self.directory = directory
        self._on_error = on_error
        self._on_start = on_start
        self._on_reclaim = on_reclaim
        self._temperatures = temperatures
        self._temperature_error = None  # last failure reading them, said once
        self._file = None
        self._writer = None
        self.path = None
        self.last_error = None  # reason recording is off, or None while healthy
        # Automatic space reclaim, both switchable from the settings panel.
        # Deleting old runs and cutting the oldest parts off the run in progress
        # are separate switches on purpose: an operator may well be happy to lose
        # last week's runs and not this one.
        self.prune_old_runs = True
        self.trim_current_run = True
        # Current run: its identity (the base name), which part is open, which of
        # its parts still exist, and the settings row every part repeats.
        self._base = None
        self._part = 1
        self._parts = []
        self._settings = None
        self._rows_since_size_check = 0
        self._last_space_check = 0.0

    @property
    def active(self) -> bool:
        return self._file is not None

    @property
    def run_name(self) -> str | None:
        """Base name of the run being written — its identity outside this class,
        whichever part happens to be open."""
        return self._base

    def set_reclaim(self, prune_old_runs: bool, trim_current_run: bool) -> None:
        """Switch the two halves of the automatic reclaim. Takes effect at the
        next check, mid-run included."""
        self.prune_old_runs = bool(prune_old_runs)
        self.trim_current_run = bool(trim_current_run)
        print(f"[RunLogger] Reclaim: delete old runs={self.prune_old_runs}, "
              f"trim run in progress={self.trim_current_run}")

    def start(self) -> str | None:
        """Open a fresh timestamped CSV for a new run and write the header.

        Returns the path, or None if this run cannot be recorded — in which case
        last_error says why and on_error has fired. The measurement itself is
        never blocked; the operator is told instead.
        """
        self.stop()  # defensive: never leak a previous handle
        self.last_error = None
        # Forget the previous run before reclaiming, so its parts are judged as
        # what they now are — somebody else's finished run.
        self._base, self._part, self._parts, self._settings = None, 1, [], None
        path = None
        try:
            os.makedirs(self.directory, exist_ok=True)

            # A run that cannot even open is the same disk-full problem as one
            # that fills up half way through it, so reclaim first and judge the
            # free space afterwards.
            self._ensure_space(force=True)

            free = shutil.disk_usage(self.directory).free
            if free < MIN_FREE_BYTES:
                raise OSError(f"only {_mb(free)} MB free on the data partition "
                              f"(need {_mb(MIN_FREE_BYTES)} MB) — archive old runs")

            self._base = self._fresh_base()
            path = self._open_part(self._base)
            if free < WARN_FREE_BYTES:
                print(f"[RunLogger] WARNING: only {_mb(free)} MB free — "
                      f"archive old runs in {self.directory}")
            return path
        except Exception as e:
            self._fail(f"this run is NOT being recorded: {e}", discard=path)
            return None

    def _fresh_base(self) -> str:
        """A run name no run on disk is already using. The timestamp only goes to
        the second, and two runs started inside the same second would otherwise
        write over each other — part for part, now that a run can be several
        files."""
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        try:
            taken = {run_base(f) for f in os.listdir(self.directory) if f.endswith(".csv")}
        except OSError:
            taken = set()
        base = f"qcm_run_{stamp}.csv"
        n = 2
        while base in taken:
            base = f"qcm_run_{stamp}-{n}.csv"
            n += 1
        return base

    def _open_part(self, name: str) -> str:
        """Open one part of the current run and write its header. Every part also
        repeats the run's SETTINGS row, so a part that outlives its siblings is
        still a readable, self-describing CSV."""
        path = os.path.join(self.directory, name)
        # Explicit UTF-8: the default depends on the locale the service happens
        # to start with, and everything that reads these files back (pandas, the
        # browser download, the plotters) assumes UTF-8. Under a C/POSIX locale
        # the default can be ASCII, where one non-ASCII character in an event
        # detail would raise mid-write and take the recording down with it.
        self._file = open(path, "w", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=HEADERS)
        self._writer.writeheader()
        self._file.flush()  # surface a full disk here, not silently mid-run
        self.path = path
        self._parts.append(name)
        self._rows_since_size_check = 0
        print(f"[RunLogger] Logging run to {path}")
        if self._settings is not None:
            self.write_settings(self._settings)
        if self._on_start:
            self._on_start(name)
        return path

    def _roll_over(self) -> None:
        """Continue the run in its next part. The run keeps its identity; only
        the file being appended to changes. Each side of the seam names the
        other, so the parts can be put back in order without trusting mtimes."""
        previous = os.path.basename(self.path or "")
        nxt = part_name(self._base, self._part + 1)
        self.write_event("PART_END", f"{_mb(SEGMENT_BYTES)} MB reached; continues in {nxt}")
        self._close()
        self._part += 1
        self._open_part(nxt)
        self.write_event("PART_START", f"part {self._part} of {self._base}; continues {previous}")

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

    # ==================================================
    # Reclaiming space
    # ==================================================

    def _free(self) -> int | None:
        try:
            return shutil.disk_usage(self.directory).free
        except OSError:
            return None

    def _reclaim_candidates(self) -> list[tuple[str, str, int]]:
        """(name, path, size) for every CSV that may be deleted, in the order
        they should go: other runs oldest first, then this run's earlier parts
        oldest first. The part being written is never a candidate — a run always
        keeps the data it is taking right now."""
        open_name = os.path.basename(self.path) if self.path else None
        others, mine = [], []
        for name in os.listdir(self.directory):
            if not name.endswith(".csv") or name == open_name:
                continue
            path = os.path.join(self.directory, name)
            try:
                st = os.stat(path)
            except OSError:
                continue  # vanished under us, or unreadable: not ours to reclaim
            entry = (st.st_mtime, part_number(name), name, path, st.st_size)
            if self._base and run_base(name) == self._base:
                mine.append(entry)
            else:
                others.append(entry)
        # Age decides which run goes first; the part number breaks ties, because
        # this box's clock steps when it picks up NTP and two parts can end up
        # with mtimes in the wrong order.
        others.sort(key=lambda e: (e[0], e[1]))
        mine.sort(key=lambda e: (e[1], e[0]))
        ordered = (others if self.prune_old_runs else []) + (mine if self.trim_current_run else [])
        return [(name, path, size) for _, _, name, path, size in ordered]

    def _reclaim(self, need_free: int) -> None:
        """Delete CSVs in candidate order until need_free bytes are free."""
        deleted, freed = [], 0
        for name, path, size in self._reclaim_candidates():
            free = self._free()
            if free is None or free >= need_free:
                break
            try:
                os.remove(path)
            except OSError as e:
                print(f"[RunLogger] Could not delete {name}: {e}")
                continue
            freed += size
            deleted.append(name)
            if name in self._parts:
                self._parts.remove(name)
        if not deleted:
            return
        free = self._free()
        mine = [n for n in deleted if self._base and run_base(n) == self._base]
        summary = (f"deleted {len(deleted)} run file(s) to make room - {_mb(freed)} MB freed, "
                   f"{'?' if free is None else _mb(free)} MB now free")
        if mine:
            summary += f"; {len(mine)} of them the oldest part(s) of the run in progress"
        print(f"[RunLogger] {summary}: {', '.join(deleted)}")
        self.write_event("STORAGE_RECLAIM", f"{summary}: {' '.join(deleted)}")
        if self._on_reclaim:
            try:
                self._on_reclaim(summary, free)
            except Exception as e:
                print(f"[RunLogger] Failed to report the reclaim: {e}")

    def _ensure_space(self, force: bool = False) -> None:
        """Keep the partition above RECLAIM_BELOW_BYTES by deleting old data.

        Housekeeping must never be the thing that stops a run being recorded, so
        a failure in here is printed and dropped: if space could not be freed the
        next write fails on its own, with the message and the modal the operator
        already knows.
        """
        if not (self.prune_old_runs or self.trim_current_run):
            return
        now = time.time()
        if not force and now - self._last_space_check < SPACE_CHECK_INTERVAL:
            return
        self._last_space_check = now
        try:
            free = self._free()
            if free is None or free >= RECLAIM_BELOW_BYTES:
                return
            self._reclaim(RECLAIM_TARGET_BYTES)
        except Exception as e:
            print(f"[RunLogger] Could not reclaim space: {e}")

    # ==================================================
    # Writing
    # ==================================================

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
                **self._temperature_columns(ts),
                "event_type": "", "event_detail": "",
            })
            self._file.flush()
            self._housekeeping()
        except Exception as e:
            self._fail(f"recording stopped after a write error: {e}")

    def _temperature_columns(self, ts: float) -> dict:
        """The LTC2986 readings to write beside the measurement taken at ts: the
        newest round of them, repeated on every row until the next one arrives
        (they are read once a second, the rows come at ~10 Hz).

        All blank when there is no current reading — no board, the chip
        unavailable, or its worker stalled. A sensor with a hard fault has no
        temperature and is blank too; every fault, soft ones included, is named
        in temp_sensor_faults as "key: fault, fault; key: fault".

        A problem in here costs these columns only, never the row: the run log
        is the one copy of the measurement."""
        out = dict.fromkeys(TEMPERATURE_COLUMNS, "")
        out["temp_sensor_faults"] = ""
        if self._temperatures is None:
            return out
        try:
            reading = self._temperatures()
            if (reading is None or not reading.available or reading.timestamp is None
                    or abs(ts - reading.timestamp) > TEMPERATURE_MAX_AGE):
                return out
            faults = []
            for s in reading.sensors:
                column = f"{s['key']}_c"
                if column in out and s["temperature"] is not None:
                    out[column] = round(s["temperature"], 4)
                if s["faults"]:
                    faults.append(f"{s['key']}: {', '.join(s['faults'])}")
            out["temp_sensor_faults"] = "; ".join(faults)
            self._temperature_error = None
        except Exception as e:
            if str(e) != self._temperature_error:
                print(f"[RunLogger] Could not add the temperature sensor readings: {e}")
                self._temperature_error = str(e)
            out = dict.fromkeys(out, "")
        return out

    def _housekeeping(self) -> None:
        """Roll the part over once it is big enough, and keep the partition above
        the reclaim threshold. Runs on the measurement path, so both checks are
        rate-limited (see SPACE_CHECK_INTERVAL / _SIZE_CHECK_ROWS)."""
        self._rows_since_size_check += 1
        if self._rows_since_size_check >= _SIZE_CHECK_ROWS:
            self._rows_since_size_check = 0
            try:
                if self.path and os.path.getsize(self.path) >= SEGMENT_BYTES:
                    self._roll_over()
            except OSError as e:
                print(f"[RunLogger] Could not check the log size: {e}")
        self._ensure_space()

    def write_settings(self, settings: dict) -> None:
        """Record the settings a run was acquired with as a single SETTINGS event
        row, with the nested dict JSON-encoded into event_detail. Deliberately an
        event row rather than new columns or a header block, so the layout is
        unchanged and existing readers keep working.

        Kept and repeated at the top of every part, so a part that outlives the
        first one can still be interpreted on its own."""
        self._settings = settings
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
