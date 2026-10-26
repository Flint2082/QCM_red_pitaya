# Responsible for:
#
# Exposing REST endpoints for control commands
# Streaming measurement events to clients over WebSocket
# Translating HTTP/WS messages into app-layer commands/events
# No business logic — pure transport

import asyncio
import json
import logging
import math
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager

import app
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from domain.crystal import CrystalManager, CrystalProfile, sanitize_name
from domain.qcm_interface import OUTPUT_GAIN_MAX, WINDOW_SIZE
from domain.run_logger import MIN_FREE_BYTES, RUNS_DIR, part_number, run_base
from domain.sweep_logger import SWEEPS_DIR
from plc.opc_worker import _READ_KEYS as OPC_READ_KEYS, _CTRL_KEYS as OPC_CTRL_KEYS
from messaging.api_command import *
from messaging.api_event import LogEvent
from messaging.defines import OutputMode


# ==================================================
# Log capture — forwards print() and logging to WS clients
# ==================================================

_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;]*[A-Za-z]|\][^\x07]*\x07|[^[])")

def _clean(text: str) -> str:
    """Strip ANSI escape codes and stray carriage returns."""
    return _ANSI_RE.sub("", text).replace("\r", "")


class _StdoutForwarder:
    """Wraps sys.stdout so that print() lines are also pushed as LogEvents."""

    def __init__(self, original, event_queue: queue.Queue):
        self._original = original
        self._queue = event_queue
        self._buf = ""

    def write(self, text: str):
        if not text:
            return
        self._original.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                try:
                    self._queue.put_nowait(
                        LogEvent(level="INFO", message=_clean(line), timestamp=time.time())
                    )
                except Exception:
                    pass

    def flush(self):
        self._original.flush()

    def fileno(self):
        try:
            return self._original.fileno()
        except Exception:
            return -1

    def isatty(self):
        return getattr(self._original, "isatty", lambda: False)()


class _StderrForwarder(_StdoutForwarder):
    """Like _StdoutForwarder but emits ERROR-level LogEvents.
    Captures thread tracebacks and other error output."""

    def write(self, text: str):
        if not text:
            return
        self._original.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                try:
                    self._queue.put_nowait(
                        LogEvent(level="ERROR", message=_clean(line), timestamp=time.time())
                    )
                except Exception:
                    pass


class _LoggingForwarder(logging.Handler):
    """Forwards Python logging records to the event queue as LogEvents."""

    def __init__(self, event_queue: queue.Queue):
        super().__init__()
        self._queue = event_queue

    def emit(self, record: logging.LogRecord):
        try:
            self._queue.put_nowait(
                LogEvent(level=record.levelname, message=_clean(self.format(record)),
                         timestamp=record.created)
            )
        except Exception:
            pass


# ==================================================
# Persistent settings
# ==================================================

# Every key _save_settings writes. Anything else found in settings.json comes
# from an older version of this server: it is reported and dropped on load, so
# the file never accumulates keys that no longer mean anything (which otherwise
# makes it impossible to tell, while debugging, whether a value is in use).
_SETTING_KEYS = frozenset({
    "osc_settings", "output_mode", "lock_phase_tolerance", "lock_phase_std",
    "auto_relock", "temp_hold", "freq_tracking", "freq_track_interval", "measurement_mode",
    "prune_old_runs", "trim_current_run",
    "active_crystal", "ambient_temp", "mat_dens", "z_ratio",
    "target_thickness",
    "opc_enabled", "opc_url", "opc_user", "opc_password", "opc_base_node",
})

# Measurement modes. "compensated" locks both modes and solves the temperature
# from fT; "uncompensated" locks the mass mode alone and parks the temp
# oscillator, for a crystal with no usable temp mode or no calibration.
MEASUREMENT_MODES = ("compensated", "uncompensated")

# OPC keys, kept together because they are carried through verbatim when the
# bridge is disabled (see _save_settings).
_OPC_SETTING_KEYS = ("opc_enabled", "opc_url", "opc_user", "opc_password", "opc_base_node")

# The settings every instrument starts from and that RESTORE DEFAULTS returns
# to. They ship with the software rather than being captured per box, so every
# QCM behaves the same out of the box and a restore is reproducible: if these
# values need to change, they change here, in review, for everyone.
#
# This is also the single definition of each default — __init__ initialises the
# server state from it, so there is no second copy to drift.
#
# Two persisted settings are deliberately absent, which is what keeps these
# universal: the OPC connection is site wiring (resetting the PLC address would
# cut the link to the coater), and the active crystal is the consumable this
# particular box has fitted — no repo-wide default can name the right one, and
# clearing it would take the calibration with it. Both survive a restore.
DEFAULT_SETTINGS = {
    # Post-lock loop settings, matching QCMInterface's own defaults.
    #
    # The AGC ships off, at a manual drive gain of 0.25 — a quarter of the
    # undivided sinewave, which is what the hardware put out before output_gain_N
    # existed. That difference is worth knowing about: an instrument coming from
    # an older build is driven more gently than it was, so the amplitudes on the
    # Monitor tab drop with it, and any figure chosen against the old drive —
    # the target amplitude below above all — wants re-reading at the new one.
    #
    # It also sits halfway up the range, the gain register being capped at 0.5
    # (see OUTPUT_GAIN_MAX): the AGC starts every unlocked channel here and can
    # only double the drive from it before it rails. That is enough for the ~2x
    # the b-mode loses over 20 K and not much more, so a crystal that also needs
    # the drive raised for film loading wants a lower manual gain to start from.
    #
    # Switching the AGC on needs a target amplitude: 1024 is a typical
    # demodulated amplitude at a healthy lock, so it puts the servo in the right
    # decade to start from, but the figure that belongs here is whatever *this*
    # crystal returns — read one off a good lock rather than trusting the
    # default, or the gain can simply run to a rail on the first lock.
    "osc_settings": {
        "1": {"int_gain": 1e-7, "lpf_freq": 200.0, "inverted": True,
              "agc_enabled": False, "manual_gain": 0.25, "target_amp": 1024.0},
        "2": {"int_gain": 1e-7, "lpf_freq": 200.0, "inverted": True,
              "agc_enabled": False, "manual_gain": 0.25, "target_amp": 1024.0},
    },
    "output_mode": 0,               # OutputMode.DELTA — the error signal
    "lock_phase_tolerance": 0.1,    # rad, max |mean phase error|
    "lock_phase_std": 0.1,          # rad, max phase-error sigma ("lock quality")
    "auto_relock": True,            # re-acquire automatically on a lost lock
    "temp_hold": True,              # hold the last temperature while temp lock is lost
    "measurement_mode": "compensated",  # both modes; "uncompensated" = mass mode only
    "freq_tracking": True,          # follow the locked frequencies for re-lock
    "freq_track_interval": 10.0,    # s between lock-frequency updates
    "prune_old_runs": True,         # delete finished runs when the disk runs low
    "trim_current_run": True,       # drop the oldest parts of the run in progress
    "ambient_temp": 23.0,           # degC, room temperature
    "mat_dens": 19320.0,            # kg/m3, gold
    "z_ratio": 1.0,                 # 1.0 = plain Sauerbrey, no Z-match
    "target_thickness": 0.0,        # nm, 0 = no target
}


# ==================================================
# Run files on disk
# ==================================================

# Block size for streaming a multi-part run back out. 1 MB keeps the copy loop
# out of the way of the worker's own writes without holding much.
_DOWNLOAD_BLOCK = 1024 ** 2


def _run_files(base: str) -> list[str]:
    """The parts of one run that are still on disk, in order. A run is normally
    a single CSV; a long one is several, and the reclaim may have taken the
    earliest of them, so this is the authority on what a run currently consists
    of rather than the base name existing as a file."""
    try:
        names = [f for f in os.listdir(RUNS_DIR)
                 if f.endswith(".csv") and run_base(f) == base]
    except OSError:
        return []
    return sorted(names, key=part_number)


def _grouped_runs() -> list[dict]:
    """Runs on disk, newest first, with the parts of each folded into one entry:
    `size` totals them and `parts` counts them."""
    groups: dict[str, dict] = {}
    try:
        for f in os.listdir(RUNS_DIR):
            if not f.endswith(".csv"):
                continue
            try:
                st = os.stat(os.path.join(RUNS_DIR, f))
            except OSError:
                continue  # vanished under us (the reclaim runs concurrently)
            base = run_base(f)
            g = groups.setdefault(base, {"name": base, "size": 0,
                                         "modified": 0.0, "parts": 0})
            g["size"] += st.st_size
            g["modified"] = max(g["modified"], st.st_mtime)
            g["parts"] += 1
    except FileNotFoundError:
        pass
    return sorted(groups.values(), key=lambda r: r["modified"], reverse=True)


# ==================================================
# WebSocket connection manager
# ==================================================

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        try:
            self.active.remove(ws)
        except ValueError:
            pass  # already removed by a concurrent disconnect path

    async def broadcast(self, message: dict):
        for ws in list(self.active):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(ws)


# ==================================================
# Server class
# ==================================================

class RestServer:
    def __init__(self, api_command_queue: queue.Queue, api_event_queue: queue.Queue,
                 wago_client=None, opc_worker=None, version=None):
        self.command_queue = api_command_queue
        self.event_queue = api_event_queue
        self._wago_client = wago_client  # optional reference for live reconfiguration
        self._opc_worker = opc_worker    # switched on and off by the opc_enabled setting
        # Whether the OPC-UA bridge runs. Off unless this box's settings.json
        # says otherwise: an instrument without a PLC should not sit retrying a
        # connection to a coater that is not there. Kept with the connection
        # settings, outside DEFAULT_SETTINGS, so RESTORE DEFAULTS cannot cut the
        # link to a coater that does rely on it.
        self._opc_enabled: bool = False
        self._version = version  # domain.version.VersionInfo, served by GET /version
        self.manager = ConnectionManager()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._broadcaster_started = False  # guards against starting two broadcasters
        self._last_state: str = "IDLE"
        self._last_opc_status: dict | None = None
        self._last_temperatures: dict | None = None  # latest TemperatureEvent, as sent
        # Run log the worker is currently writing (None = not recording). Tracked
        # so the UI downloads the run it just took rather than whichever file in
        # data/runs/ happens to have the newest mtime.
        self._current_run: str | None = None
        # Whether that run is being written *right now*. _current_run keeps
        # naming the last run of the session (the download button wants it), so
        # it cannot on its own answer "is this file open" — which is what decides
        # whether a delete has to be refused.
        self._recording: bool = False
        self._run_log_error: str | None = None
        # Lock frequencies used by the GET LOCK command. These are NOT persisted:
        # the active crystal profile is the source of truth (applied on boot).
        # The values here are only a fallback until a crystal is applied.
        self._lock_freq_mass: float = 5983000.0
        self._lock_freq_temp: float = 6570000.0
        # Oscillator settings, output mode, lock-detect conditions, auto re-lock
        # and the per-run measurement parameters all start at DEFAULT_SETTINGS;
        # _load_settings then overrides whatever this box has saved. The
        # measurement parameters double as the values OPC-triggered starts use,
        # so OPC needs no settings of its own.
        self._apply_settings(DEFAULT_SETTINGS)
        # Target-thickness flag. The worker owns the comparison; these mirror it
        # so REST clients and the PLC can read the flag at any time rather than
        # having to catch the event.
        self._target_reached: bool = False
        self._target_reached_at: float | None = None
        self._target_thickness_at: float | None = None  # thickness when it tripped
        # Sweep the worker last recorded, for "download the sweep I just took".
        self._current_sweep: str | None = None
        # Coefficient cache — reflects the active crystal (no standalone CSV)
        self._coefficients: dict | None = None
        # Crystal profiles
        self._crystals = CrystalManager()
        self._active_crystal: str | None = None
        # Stats tracking (updated in event broadcaster)
        self._measurement_start_time: float | None = None
        self._session_thickness: float = 0.0
        # Persistent settings file
        self._settings_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "settings.json")
        # OPC parameters as read from the file. Only used when the bridge is
        # disabled (--no-opcua), where there is no client to read them back off.
        self._opc_settings: dict = {}
        self._load_settings()   # override the defaults with this box's saved values
        if self._opc_worker:
            self._opc_worker.set_enabled(self._opc_enabled)
        self._enqueue_settings("boot")  # push settings to the hardware on startup
        self.app = self._build_app()
        


    # --------------------------------------------------
    # Persistent settings
    # --------------------------------------------------

    def _instrument_settings(self) -> dict:
        """How this instrument is configured, as plain data. Inner dicts are
        copied so a caller holding the result (the defaults, in particular) does
        not alias live state and quietly change with it."""
        return {
            "osc_settings":   {str(k): dict(v) for k, v in self._osc_settings.items()},
            "output_mode":    self._output_mode,
            "lock_phase_tolerance": self._lock_phase_tolerance,
            "lock_phase_std":       self._lock_phase_std,
            "auto_relock":    self._auto_relock,
            "temp_hold":      self._temp_hold,
            "measurement_mode": self._measurement_mode,
            "freq_tracking":  self._freq_tracking,
            "freq_track_interval": self._freq_track_interval,
            "prune_old_runs":   self._prune_old_runs,
            "trim_current_run": self._trim_current_run,
            "active_crystal": self._active_crystal,
            "ambient_temp":   self._ambient_temp,
            "mat_dens":       self._mat_dens,
            "z_ratio":        self._z_ratio,
            "target_thickness": self._target_thickness,
        }

    def _apply_settings(self, d: dict):
        """Copy instrument settings out of a plain dict into live state. Shared by
        the boot load and by restore-defaults, so both honour exactly the same
        keys. Only the hardware push (_enqueue_settings) is left to the caller.

        "lock_amp_threshold" / "auto_amp_threshold" may still be present in an old
        settings.json from before lock detect went amplitude-free; unknown keys
        are ignored here and dropped on the next save."""
        if "osc_settings" in d:
            # Merged over the defaults rather than replacing them: a settings.json
            # written by an older build has no entry for a setting added since
            # (the AGC keys, most recently), and taking the file verbatim would
            # leave those oscillators with no value at all — the hardware push
            # would skip them and the settings panel would show blanks. Anything
            # the file does carry still wins.
            defaults = DEFAULT_SETTINGS["osc_settings"]
            self._osc_settings = {}
            for k, v in d["osc_settings"].items():
                merged = dict(defaults.get(str(k), {}))
                merged.update(v)
                self._osc_settings[int(k)] = merged
        if "output_mode" in d:
            self._output_mode = int(d["output_mode"])
        if "lock_phase_tolerance" in d:
            self._lock_phase_tolerance = float(d["lock_phase_tolerance"])
        if "lock_phase_std" in d:
            self._lock_phase_std = float(d["lock_phase_std"])
        if "auto_relock" in d:
            self._auto_relock = bool(d["auto_relock"])
        if "temp_hold" in d:
            self._temp_hold = bool(d["temp_hold"])
        if d.get("measurement_mode") in MEASUREMENT_MODES:
            self._measurement_mode = d["measurement_mode"]
        if "freq_tracking" in d:
            self._freq_tracking = bool(d["freq_tracking"])
        if "freq_track_interval" in d:
            self._freq_track_interval = max(1.0, float(d["freq_track_interval"]))
        if "prune_old_runs" in d:
            self._prune_old_runs = bool(d["prune_old_runs"])
        if "trim_current_run" in d:
            self._trim_current_run = bool(d["trim_current_run"])
        if "active_crystal" in d:
            self._active_crystal = d["active_crystal"]
        if "ambient_temp" in d:
            self._ambient_temp = float(d["ambient_temp"])
        if "mat_dens" in d:
            self._mat_dens = float(d["mat_dens"])
        if "z_ratio" in d:
            self._z_ratio = float(d["z_ratio"])
        if "target_thickness" in d:
            self._target_thickness = float(d["target_thickness"])

    def _load_settings(self):
        try:
            with open(self._settings_file) as f:
                d = json.load(f)
        except FileNotFoundError:
            return  # first run — keep defaults
        except Exception as e:
            print(f"[Settings] Load failed: {e}")
            return

        self._apply_settings(d)

        # Restore the OPC connection parameters. WagoClient is constructed — and
        # has usually already auto-connected using its compiled-in defaults —
        # before this runs, so assigning the attributes is not enough: the client
        # would keep talking to the default server while reporting the persisted
        # URL. set_connection drops the connection instead, and the OPC worker's
        # reconnect loop brings it straight back up with the saved settings.
        self._opc_settings = {k: d[k] for k in _OPC_SETTING_KEYS if k in d}
        self._opc_enabled = bool(d.get("opc_enabled", False))
        c = self._wago_client
        if c and "opc_url" in d:
            if d.get("opc_base_node"):
                c.set_base_node_path(d["opc_base_node"])
            url, user, pwd = d["opc_url"], d.get("opc_user", ""), d.get("opc_password", "")
            if (c.url, c.user, c.password) != (url, user, pwd):
                c.set_connection(url, user, pwd)
                print(f"[Settings] Applied persisted OPC connection {url}")

        stale = set(d) - _SETTING_KEYS
        if stale:
            print(f"[Settings] Dropping obsolete keys: {', '.join(sorted(stale))}")
            self._save_settings()

    @staticmethod
    def _write_json(path: str, data: dict, label: str) -> bool:
        """Write JSON to a temp file and rename it into place.

        Truncating the real file first means a full SD card (which this box has
        hit before) or a power cut mid-write leaves a half-written file — which
        fails to parse on the next boot and silently resets the instrument to
        defaults. os.replace is atomic within a filesystem, so the old file
        survives intact until the new one is complete.
        """
        tmp = path + ".tmp"
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return True
        except Exception as e:
            print(f"[{label}] Save failed: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass  # nothing to clean up
            return False

    def _save_settings(self):
        data = self._instrument_settings()
        # The live client owns these while the bridge is enabled. With --no-opcua
        # there is no client, so whatever was loaded from the file is written back
        # unchanged — otherwise one run without the bridge would erase the PLC
        # connection settings the moment any unrelated setting was saved.
        if self._wago_client:
            data["opc_enabled"]   = self._opc_enabled
            data["opc_url"]       = self._wago_client.url
            data["opc_user"]      = self._wago_client.user
            data["opc_password"]  = self._wago_client.password
            data["opc_base_node"] = self._wago_client.base_node_path
        else:
            data.update(self._opc_settings)
        self._write_json(self._settings_file, data, "Settings")

    # --------------------------------------------------
    # Defaults
    # --------------------------------------------------

    def _restore_defaults(self) -> dict:
        """Put DEFAULT_SETTINGS back into force — state, hardware and settings
        file. Takes effect immediately, exactly as if each value had been set by
        hand; the material parameters, as always, apply to the next run.

        The active crystal is left alone: it is what this box has fitted, and a
        restore that dropped it would take the calibration with it."""
        self._apply_settings(DEFAULT_SETTINGS)
        self._enqueue_settings("restore defaults")
        self._save_settings()
        return dict(DEFAULT_SETTINGS)

    # --------------------------------------------------
    # Control parameters (consumed by the OPC bridge for triggered actions)
    # --------------------------------------------------

    def get_control_params(self) -> dict:
        """Current REST-owned parameters used when OPC triggers an action."""
        return {
            "ambient_temp":   self._ambient_temp,
            "mat_dens":       self._mat_dens,
            "z_ratio":        self._z_ratio,
            "lock_freq_mass": self._lock_freq_mass,
            "lock_freq_temp": self._lock_freq_temp,
        }

    def _set_target_thickness(self, value: float):
        """Push a new target to the worker (which owns the comparison) and cache
        it here for GET /settings. Negative values mean "no target", same as 0."""
        self._target_thickness = max(0.0, float(value))
        self.command_queue.put(SetTargetThicknessCommand(self._target_thickness))

    def _apply_crystal(self, profile: CrystalProfile):
        """Push a crystal's settings into server state and the command queue.
        The active crystal is the source of truth for lock frequencies and
        calibration coefficients."""
        self._lock_freq_mass = profile.freq_mass
        self._lock_freq_temp = profile.freq_temp
        self._coefficients = {
            "fM_0": profile.fM_0, "fM_1": profile.fM_1,
            "fM_2": profile.fM_2, "fM_3": profile.fM_3,
            "fT_0": profile.fT_0, "fT_1": profile.fT_1,
            "fT_2": profile.fT_2, "fT_3": profile.fT_3,
        }
        self.command_queue.put(SetCoefficientsCommand(
            profile.fM_0, profile.fM_1, profile.fM_2, profile.fM_3,
            profile.fT_0, profile.fT_1, profile.fT_2, profile.fT_3,
        ))
        self.command_queue.put(SetSensorParamsCommand(profile.mass_sensitivity, profile.sens_area, profile.freq_virgin, profile.tooling_ratio))

    def _enqueue_settings(self, reason: str = "boot"):
        """Push the current settings to the hardware so stored values take effect
        without a manual 'apply' click. At startup these commands buffer on the
        queue until the worker thread starts and drains them.

        Coefficients and lock frequencies come from the active crystal profile
        (the only persisted crystal state is which one is active), so the active
        crystal is applied here too.
        """
        for osc, s in self._osc_settings.items():
            if "int_gain" in s:
                self.command_queue.put(SetIntegratorGainCommand(osc, s["int_gain"]))
            if "lpf_freq" in s:
                self.command_queue.put(SetLPFFreqCommand(osc, s["lpf_freq"]))
            if "inverted" in s:
                self.command_queue.put(SetInvertedCommand(osc, bool(s["inverted"])))
            # One command, because the three AGC values only mean anything
            # together: a mode with no manual gain to fall back on, or a target
            # with no mode, would be half a configuration.
            if {"agc_enabled", "manual_gain", "target_amp"} <= s.keys():
                # A gain saved by a build whose register was Fixed 32.24 can be
                # anything up to 128; 32.32 stops at 0.5. Clamp the stored value,
                # not just the word written to hardware, so /settings and the UI
                # report the drive actually in force rather than one the next POST
                # would be rejected for.
                gain = min(max(float(s["manual_gain"]), 0.0), OUTPUT_GAIN_MAX)
                if gain != s["manual_gain"]:
                    print(f"[Settings] osc {osc} manual_gain {s['manual_gain']} is out of "
                          f"range for the 32.32 gain register — clamped to {gain:g} ({reason})")
                    s["manual_gain"] = gain
                self.command_queue.put(SetAGCCommand(
                    osc, bool(s["agc_enabled"]), gain, float(s["target_amp"])))

        try:
            self.command_queue.put(SetOutputModeCommand(OutputMode(self._output_mode)))
        except ValueError:
            print(f"[Settings] Skipping invalid output_mode={self._output_mode} ({reason})")

        self.command_queue.put(SetLockDetectCommand(self._lock_phase_tolerance, self._lock_phase_std))
        self.command_queue.put(SetAutoRelockCommand(self._auto_relock))
        self.command_queue.put(SetTempHoldCommand(self._temp_hold))
        self.command_queue.put(SetMeasurementModeCommand(self._measurement_mode == "compensated"))
        self.command_queue.put(SetFreqTrackingCommand(self._freq_tracking, self._freq_track_interval))
        self.command_queue.put(SetStorageReclaimCommand(self._prune_old_runs, self._trim_current_run))
        self.command_queue.put(SetTargetThicknessCommand(self._target_thickness))

        # Apply the active crystal's coefficients + lock frequencies, if any.
        if self._active_crystal:
            profile = self._crystals.load(self._active_crystal)
            if profile:
                self._apply_crystal(profile)
                print(f"[Settings] Applied active crystal '{self._active_crystal}' ({reason})")
            else:
                print(f"[Settings] Active crystal '{self._active_crystal}' not found; skipping")

        print(f"[Settings] Queued settings to apply ({reason})")

    # --------------------------------------------------
    # System control
    # --------------------------------------------------

    @staticmethod
    def _do_reboot():
        """Reboot the box, off the request thread so the HTTP response is
        delivered first — otherwise the UI sees a dropped connection instead of
        an acknowledgement and cannot tell a reboot from a crash."""
        time.sleep(0.5)
        # systemctl is the clean path (it stops the units first); the plain
        # reboot binary is the fallback when systemd is not the init. Each is
        # run to completion so a non-zero exit falls through to the next one —
        # Popen alone would "succeed" for a command that then refuses. Requires
        # root, which the service already runs as.
        for cmd in (["systemctl", "reboot"], ["/sbin/reboot"], ["reboot"]):
            if shutil.which(cmd[0]) is None and not os.path.exists(cmd[0]):
                continue
            try:
                # A reboot request returns promptly and the machine goes down
                # afterwards, so a short timeout is a hang, not normal operation.
                result = subprocess.run(cmd, timeout=30, capture_output=True, text=True)
                if result.returncode == 0:
                    return
                print(f"[API] {' '.join(cmd)} exited {result.returncode}: {result.stderr.strip()}")
            except Exception as e:
                print(f"[API] {' '.join(cmd)} failed: {e}")
        print("[API] Reboot failed — no working reboot command (are we running as root?)")

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    def start(self):
        self._setup_log_capture()
        self._thread = threading.Thread(target=self._run, daemon=True, name="api-server")
        self._thread.start()

    def _setup_log_capture(self):
        """Forward print() and Python logging (including stderr) to WebSocket clients."""
        sys.stdout = _StdoutForwarder(sys.stdout, self.event_queue)
        # Capture stderr so thread crashes / tracebacks appear in the GUI log tab
        sys.stderr = _StderrForwarder(sys.stderr, self.event_queue)

        handler = _LoggingForwarder(self.event_queue)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(name)s — %(message)s"))
        logging.getLogger().addHandler(handler)

    def stop(self):
        # Ask uvicorn to shut down gracefully so serve() returns and
        # run_until_complete() finishes cleanly. Stopping the loop directly
        # made run_until_complete raise "Event loop stopped before Future
        # completed" and crashed the api-server thread on shutdown.
        if self._server is not None:
            self._server.should_exit = True

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Background task: drain the event queue and broadcast to WS clients
        self._loop.create_task(self._event_broadcaster())

        config = uvicorn.Config(
            app=self.app,
            host="0.0.0.0",
            port=8000,
            loop="asyncio",
            ws="websockets", # explicitly tell uvicorn which WS library to use
            # Keepalive: ping every 20 s (keeps NAT/firewall mappings alive and
            # detects dead peers) but tolerate up to 120 s without a pong before
            # dropping, so a brief event-loop stall or network blip doesn't kill
            # an otherwise-healthy connection. Default ws_ping_timeout is only 20 s.
            ws_ping_interval=20.0,
            ws_ping_timeout=120.0,
            log_level="info",
        )
        self._server = uvicorn.Server(config)
        self._loop.run_until_complete(self._server.serve())

    # --------------------------------------------------
    # Event broadcaster (event_queue → WebSocket clients)
    # --------------------------------------------------

    async def _event_broadcaster(self):
        # This is scheduled from both _run() and the FastAPI lifespan; guard so
        # only one instance ever runs. Two broadcasters would race on the same
        # queue (each get_nowait() steals events from the other).
        if self._broadcaster_started:
            return
        self._broadcaster_started = True
        while True:
            try:
                event = self.event_queue.get_nowait()
                msg = self._serialise_event(event)

                if msg.get("type") == "StateEvent":
                    new_state = msg.get("state", "IDLE")
                    self._last_state = new_state
                    # A run spans its re-locks, which go RUNNING -> LOCKING ->
                    # RUNNING. Keyed on the bare RUNNING edges, every re-lock used
                    # to close the run's stats and open new ones — adding the
                    # thickness so far to the crystal's total twice, and marking
                    # the run as no longer recording, so it could be deleted.
                    in_run = self._measurement_start_time is not None
                    # Start tracking when measurement begins
                    if new_state == "RUNNING" and not in_run:
                        self._measurement_start_time = time.time()
                        self._session_thickness = 0.0
                    # Flush stats when measurement ends
                    elif in_run and new_state not in ("RUNNING", "LOCKING"):
                        self._recording = False  # the worker closes the log when a run ends
                        if self._measurement_start_time is not None and self._active_crystal:
                            elapsed_h = (time.time() - self._measurement_start_time) / 3600
                            profile = self._crystals.load(self._active_crystal)
                            if profile:
                                profile.hours_active    += elapsed_h
                                profile.total_deposited += self._session_thickness
                                self._crystals.save(profile)
                        self._measurement_start_time = None

                elif msg.get("type") == "MeasurementEvent":
                    thickness = msg.get("calculated_thickness")
                    if thickness is not None:
                        self._session_thickness = max(self._session_thickness, float(thickness or 0))

                elif msg.get("type") == "TargetReachedEvent":
                    self._target_reached = bool(msg.get("reached"))
                    self._target_reached_at = time.time() if self._target_reached else None
                    self._target_thickness_at = msg.get("thickness") if self._target_reached else None

                elif msg.get("type") == "StartFreqAutoUpdatedEvent":
                    # Whether it came from the periodic tracking or the end of a
                    # run, this is now the best guess at where the resonances are.
                    # GET LOCK and the OPC-triggered lock both read these, so
                    # they get the tracked values rather than the ones the crystal
                    # profile was written with. The profile on disk is untouched.
                    self._lock_freq_mass = msg.get("freq_mass", self._lock_freq_mass)
                    self._lock_freq_temp = msg.get("freq_temp", self._lock_freq_temp)

                elif msg.get("type") == "SweepCompleteEvent":
                    # None when the sweep could not be recorded — keep it None so
                    # the UI greys the download rather than offering a stale file.
                    self._current_sweep = msg.get("name")

                elif msg.get("type") == "RunLogStartedEvent":
                    # Also fires when a long run rolls over into its next part;
                    # the name is then that part, and run_base() maps it back to
                    # the run it belongs to.
                    self._current_run = msg.get("name")
                    self._recording = True
                    self._run_log_error = None

                elif msg.get("type") == "RunLogFailedEvent":
                    self._current_run = None
                    self._recording = False
                    self._run_log_error = msg.get("reason")

                elif msg.get("type") == "OpcStatusEvent":
                    self._last_opc_status = msg

                elif msg.get("type") == "TemperatureEvent":
                    self._last_temperatures = msg

                await self.manager.broadcast(msg)
            except queue.Empty:
                await asyncio.sleep(0.01)

    @staticmethod
    def _serialise_event(event) -> dict:
        """Convert an event object to a JSON-serialisable dict, flattening nested dataclasses."""
        result = {"type": type(event).__name__}
        for k, v in vars(event).items():
            if hasattr(v, "__dataclass_fields__"):
                result.update(vars(v))
            else:
                result[k] = v
        return result

    # --------------------------------------------------
    # FastAPI app
    # --------------------------------------------------

    def _build_app(self) -> FastAPI:

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            asyncio.get_event_loop().create_task(self._event_broadcaster())
            yield

        app_version = self._version.software.name() if self._version else "unknown"
        app = FastAPI(title="QCM API", version=app_version, lifespan=lifespan)
        
        # ---- WebSocket ----

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await self.manager.connect(ws)
            opened = time.time()
            client = f"{ws.client.host}:{ws.client.port}" if ws.client else "?"
            await ws.send_json({"type": "StateEvent", "state": self._last_state})
            if self._last_opc_status:
                await ws.send_json(self._last_opc_status)
            if self._last_temperatures:
                await ws.send_json(self._last_temperatures)
            try:
                while True:
                    await ws.receive_text()   # keep connection alive; we only push, not receive
            except WebSocketDisconnect as e:
                # Diagnostics for frequent drops: 1000=clean, 1001=going away,
                # 1006=abnormal (no close frame), 1011=server error.
                print(f"[WS] {client} disconnected code={e.code} up={time.time()-opened:.1f}s")
                self.manager.disconnect(ws)
            except Exception as e:
                print(f"[WS] {client} error {type(e).__name__}: {e} up={time.time()-opened:.1f}s")
                self.manager.disconnect(ws)

        # ---- Measurement control ----

        @app.post("/measurement/start")
        def start_measurement(ambient_temp: float, mat_dens: float = 19320.0, z_ratio: float = 1.0):
            # Remember the params so OPC-triggered starts reuse them, and persist.
            self._ambient_temp, self._mat_dens, self._z_ratio = ambient_temp, mat_dens, z_ratio
            self._save_settings()
            self.command_queue.put(StartMeasurementCommand(
                ambient_temp=ambient_temp, mat_dens=mat_dens, z_ratio=z_ratio))
            return {"status": "ok"}

        @app.post("/cap_adjust/start")
        def cap_adjust_start():
            self.command_queue.put(StartCapAdjustCommand(
                freq_mass=self._lock_freq_mass, freq_temp=self._lock_freq_temp))
            return {"status": "ok"}

        @app.post("/cap_adjust/stop")
        def cap_adjust_stop():
            self.command_queue.put(StopCapAdjustCommand())
            return {"status": "ok"}
        
        @app.post("/measurement/get_lock")
        def get_lock():
            self.command_queue.put(StartupPLLCommand(self._lock_freq_mass, self._lock_freq_temp))
            return {"status": "ok"}

        @app.post("/measurement/stop")
        def stop_measurement():
            self.command_queue.put(StopMeasurementCommand())
            return {"status": "ok"}

        # ---- Settings ----

        @app.post("/settings/frequency")
        def set_frequency(oscillator_idx: int, frequency: float):
            self.command_queue.put(SetFrequencyCommand(oscillator_idx, frequency))
            return {"status": "ok"}

        @app.post("/settings/integrator_gain")
        def set_integrator_gain(oscillator_idx: int, gain: float):
            self._osc_settings.setdefault(oscillator_idx, {})["int_gain"] = gain
            self.command_queue.put(SetIntegratorGainCommand(oscillator_idx, gain))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/lpf_freq")
        def set_lpf_freq(oscillator_idx: int, freq: float):
            self._osc_settings.setdefault(oscillator_idx, {})["lpf_freq"] = freq
            self.command_queue.put(SetLPFFreqCommand(oscillator_idx, freq))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/inverted")
        def set_inverted(oscillator_idx: int, inverted: bool):
            self._osc_settings.setdefault(oscillator_idx, {})["inverted"] = inverted
            self.command_queue.put(SetInvertedCommand(oscillator_idx, inverted))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/agc")
        def set_agc(oscillator_idx: int, enabled: bool, manual_gain: float = 0.25,
                    target_amp: float = 1024.0):
            """Drive gain for one oscillator (FPGA register output_gain_N).

            With `enabled`, the gain is servoed while that channel is locked so
            the demodulated amplitude holds at `target_amp` — which keeps the
            loop's SNR, and the phase noise the lock detect judges on, steady as
            the crystal's response falls off with temperature and film loading.
            Unlocked, and in the manual override (`enabled` false), the drive is
            `manual_gain` instead: off resonance the returned amplitude says
            nothing about the drive a resonance needs, so there is nothing there
            worth servoing on.

            `manual_gain` runs from 0 to 0.5. The register is all fraction
            (Fixed 32.32) and capped at half scale, so the drive tops out at half
            of the 1.0 the hardware put out before the register existed; the
            default, 0.25, is halfway up that range — worth knowing when the AGC
            is on, since it can only double the drive before it rails.
            `target_amp` is in the same units as the amplitude
            on the Monitor tab — read one off a good lock rather than guessing,
            or the gain simply runs to its limit."""
            if not 0.0 <= manual_gain <= OUTPUT_GAIN_MAX:
                raise HTTPException(400, f"manual_gain must be between 0 and {OUTPUT_GAIN_MAX:g}")
            if target_amp <= 0:
                raise HTTPException(400, "target_amp must be greater than 0")
            s = self._osc_settings.setdefault(oscillator_idx, {})
            s["agc_enabled"], s["manual_gain"], s["target_amp"] = enabled, manual_gain, target_amp
            self.command_queue.put(SetAGCCommand(oscillator_idx, enabled, manual_gain, target_amp))
            self._save_settings()
            return {"status": "ok"}

        @app.get("/settings/lock_frequencies")
        def get_lock_frequencies():
            return {"mass": self._lock_freq_mass, "temp": self._lock_freq_temp}

        @app.post("/settings/lock_frequencies")
        def set_lock_frequencies(mass: float, temp: float):
            # Transient: feeds the GET LOCK command from the crystal form. Not
            # persisted — the active crystal profile owns the lock frequencies.
            self._lock_freq_mass = mass
            self._lock_freq_temp = temp
            return {"status": "ok"}

        @app.get("/settings/coefficients")
        def get_coefficients():
            # Coefficients come from the active crystal profile.
            if self._coefficients is None and self._active_crystal:
                profile = self._crystals.load(self._active_crystal)
                if profile:
                    self._apply_crystal(profile)
            return self._coefficients or {}

        @app.post("/settings/coefficients")
        def set_coefficients(
            fM_0: float, fM_1: float, fM_2: float, fM_3: float,
            fT_0: float, fT_1: float, fT_2: float, fT_3: float,
        ):
            self._coefficients = dict(fM_0=fM_0, fM_1=fM_1, fM_2=fM_2, fM_3=fM_3,
                                      fT_0=fT_0, fT_1=fT_1, fT_2=fT_2, fT_3=fT_3)
            self.command_queue.put(SetCoefficientsCommand(fM_0, fM_1, fM_2, fM_3, fT_0, fT_1, fT_2, fT_3))
            return {"status": "ok"}

        @app.post("/settings/output_mode")
        def set_output_mode(mode: int):
            # Validate before storing: an unknown mode used to be written to
            # _output_mode and only then raise, leaving the server reporting a
            # mode the hardware never received.
            try:
                parsed = OutputMode(mode)
            except ValueError:
                raise HTTPException(400, f"Unknown output mode {mode}")
            self._output_mode = mode
            self.command_queue.put(SetOutputModeCommand(parsed))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/lock_detect")
        def set_lock_detect(phase_tolerance: float, phase_std: float):
            self._lock_phase_tolerance = phase_tolerance
            self._lock_phase_std = phase_std
            self.command_queue.put(SetLockDetectCommand(phase_tolerance, phase_std))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/auto_relock")
        def set_auto_relock(enabled: bool):
            self._auto_relock = enabled
            self.command_queue.put(SetAutoRelockCommand(enabled))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/temp_hold")
        def set_temp_hold(enabled: bool):
            """Hold the last calculated temperature while the temp-mode lock is
            lost, instead of solving the compensation from an fT that is no
            longer sitting on a resonance. The mass mode keeps being measured, so
            only the thermal correction freezes."""
            self._temp_hold = enabled
            self.command_queue.put(SetTempHoldCommand(enabled))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/measurement_mode")
        def set_measurement_mode(mode: str):
            """"compensated" (both modes locked, temperature solved from fT) or
            "uncompensated" (mass mode only: GET LOCK locks just the mass mode,
            parks the temp oscillator, and the run reports the uncompensated
            thickness with no temperature). GET LOCK picks it up straight away; a
            run in progress keeps the mode it started in."""
            if mode not in MEASUREMENT_MODES:
                raise HTTPException(400, f"mode must be one of {', '.join(MEASUREMENT_MODES)}")
            self._measurement_mode = mode
            self.command_queue.put(SetMeasurementModeCommand(mode == "compensated"))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/freq_tracking")
        def set_freq_tracking(enabled: bool, interval: float = 10.0):
            """Follow the locked frequencies: while both modes are locked, the
            re-lock start frequencies are re-pointed at what the PLLs are
            actually holding every `interval` seconds, so a lost lock is
            re-acquired from where the resonances are now."""
            if interval < 1.0:
                raise HTTPException(400, "interval must be at least 1 s")
            self._freq_tracking = enabled
            self._freq_track_interval = interval
            self.command_queue.put(SetFreqTrackingCommand(enabled, interval))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/storage_reclaim")
        def set_storage_reclaim(prune_old_runs: bool, trim_current_run: bool):
            """What the run logger may delete when the data partition runs low.
            `prune_old_runs` gives up finished runs, oldest first;
            `trim_current_run` drops the oldest parts of the run in progress,
            which always keeps the part it is writing. Both are permanent — a run
            CSV is the only copy of that run — and with both off a full disk stops
            the recording instead, as it always did."""
            self._prune_old_runs = prune_old_runs
            self._trim_current_run = trim_current_run
            self.command_queue.put(SetStorageReclaimCommand(prune_old_runs, trim_current_run))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/measurement_params")
        def set_measurement_params(ambient_temp: float, mat_dens: float, z_ratio: float,
                                   target_thickness: float | None = None):
            # Film/run parameters shown in the settings panel. /measurement/start
            # also captures these from the live form; persisting them on APPLY too
            # means an edit survives a restart even with no measurement in between.
            self._ambient_temp, self._mat_dens, self._z_ratio = ambient_temp, mat_dens, z_ratio
            if target_thickness is not None:
                self._set_target_thickness(target_thickness)
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/target_thickness")
        def set_target_thickness(value: float):
            """Compensated thickness in nm at which the target flag is raised.
            0 disables the target. Takes effect immediately, mid-run included."""
            self._set_target_thickness(value)
            self._save_settings()
            return {"status": "ok", "target_thickness": self._target_thickness}

        @app.get("/measurement/target")
        def get_target():
            """The target-thickness flag, for clients that poll instead of
            listening for TargetReachedEvent on the WebSocket."""
            return {
                "target_thickness": self._target_thickness,
                "enabled":          self._target_thickness > 0,
                "reached":          self._target_reached,
                "reached_at":       self._target_reached_at,
                "thickness_at_target": self._target_thickness_at,
            }

        # ---- Defaults ----

        @app.get("/settings/defaults")
        def get_defaults():
            """The settings RESTORE DEFAULTS returns to. They ship with the
            software and are the same on every instrument — there is no endpoint
            to change them, only a commit."""
            return {"settings": DEFAULT_SETTINGS}

        @app.post("/settings/restore_defaults")
        def restore_defaults():
            """Put the default settings back into force immediately. Covers
            everything in the settings panel except the OPC connection and the
            active crystal, both of which describe this particular box."""
            settings = self._restore_defaults()
            return {"status": "ok", "settings": settings}

        def _finite(v):
            return v if isinstance(v, float) and math.isfinite(v) else (None if isinstance(v, float) else v)

        @app.get("/settings")
        def get_settings():
            return {
                "oscillators":      self._osc_settings,
                "output_mode":      self._output_mode,
                "measurement": {
                    "mode":             self._measurement_mode,
                    "ambient_temp":     self._ambient_temp,
                    "mat_dens":         self._mat_dens,
                    "z_ratio":          self._z_ratio,
                    "target_thickness": self._target_thickness,
                },
                # Derived from the PLL capture window so the UI never hard-codes it.
                "pll": {
                    "window_size":   WINDOW_SIZE,
                    "capture_range": WINDOW_SIZE / 2,
                },
                "lock_detect": {
                    "phase_tolerance": self._lock_phase_tolerance,
                    "phase_std":       self._lock_phase_std,
                },
                "auto_relock": self._auto_relock,
                "temp_hold":   self._temp_hold,
                "freq_tracking": {
                    "enabled":  self._freq_tracking,
                    "interval": self._freq_track_interval,
                },
                "storage_reclaim": {
                    "prune_old_runs":   self._prune_old_runs,
                    "trim_current_run": self._trim_current_run,
                },
                "lock_frequencies": {
                    "mass": _finite(self._lock_freq_mass),
                    "temp": _finite(self._lock_freq_temp),
                },
                "coefficients": {k: _finite(v) for k, v in (self._coefficients or {}).items()},
            }

        # ---- Crystal profiles ----

        @app.get("/crystals")
        def list_crystals():
            return {"crystals": self._crystals.list_names(), "active": self._active_crystal}

        @app.post("/crystals/upload")
        async def upload_crystal(file: UploadFile = File(...)):
            name = sanitize_name(file.filename.removesuffix(".json"))
            if not name:
                raise HTTPException(400, "Invalid filename")
            raw = await file.read()
            try:
                data = json.loads(raw)
            except Exception:
                raise HTTPException(400, "Invalid JSON")
            data["name"] = name
            valid = {k: data[k] for k in CrystalProfile.__dataclass_fields__ if k in data}
            profile = CrystalProfile(**valid)
            self._crystals.save(profile)
            return {"status": "ok", "name": name}

        @app.get("/crystals/{name}")
        def get_crystal(name: str):
            profile = self._crystals.load(name)
            if not profile:
                raise HTTPException(404, "Crystal not found")
            from dataclasses import asdict
            return asdict(profile)

        @app.post("/crystals")
        def create_crystal(name: str):
            name = sanitize_name(name)
            if not name:
                raise HTTPException(400, "Invalid name")
            profile = CrystalProfile(
                name=name,
                freq_mass=self._lock_freq_mass,
                freq_temp=self._lock_freq_temp,
                fM_0=self._coefficients.get("fM_0", 0) if self._coefficients else 0,
                fM_1=self._coefficients.get("fM_1", 0) if self._coefficients else 0,
                fM_2=self._coefficients.get("fM_2", 0) if self._coefficients else 0,
                fM_3=self._coefficients.get("fM_3", 0) if self._coefficients else 0,
                fT_0=self._coefficients.get("fT_0", 0) if self._coefficients else 0,
                fT_1=self._coefficients.get("fT_1", 0) if self._coefficients else 0,
                fT_2=self._coefficients.get("fT_2", 0) if self._coefficients else 0,
                fT_3=self._coefficients.get("fT_3", 0) if self._coefficients else 0,
            )
            self._crystals.save(profile)
            return {"status": "ok", "name": name}

        @app.post("/crystals/{name}/apply")
        def apply_crystal_form(
            name: str,
            freq_mass: float, freq_temp: float,
            fM_0: float, fM_1: float, fM_2: float, fM_3: float,
            fT_0: float, fT_1: float, fT_2: float, fT_3: float,
            mass_sensitivity: float = -13.3e-8, sens_area: float = 5.25e-5,
            freq_virgin: float = 6000000.0, tooling_ratio: float = 1.0,
        ):
            """Save explicit crystal data from the settings form and apply it immediately."""
            profile = self._crystals.load(name) or CrystalProfile(name=name)
            profile.freq_mass = freq_mass
            profile.freq_temp = freq_temp
            profile.freq_virgin = freq_virgin
            profile.fM_0, profile.fM_1, profile.fM_2, profile.fM_3 = fM_0, fM_1, fM_2, fM_3
            profile.fT_0, profile.fT_1, profile.fT_2, profile.fT_3 = fT_0, fT_1, fT_2, fT_3
            profile.mass_sensitivity = mass_sensitivity
            profile.sens_area = sens_area
            profile.tooling_ratio = tooling_ratio
            self._crystals.save(profile)
            self._apply_crystal(profile)
            self._active_crystal = name
            self._save_settings()
            return {"status": "ok"}

        @app.post("/crystals/{name}/activate")
        def activate_crystal(name: str):
            profile = self._crystals.load(name)
            if not profile:
                raise HTTPException(404, "Crystal not found")
            self._apply_crystal(profile)
            self._active_crystal = name
            self._save_settings()
            return {"status": "ok"}

        @app.post("/crystals/{name}/save_current")
        def save_current_to_crystal(name: str):
            profile = self._crystals.load(name)
            if not profile:
                raise HTTPException(404, "Crystal not found")
            profile.freq_mass = self._lock_freq_mass
            profile.freq_temp = self._lock_freq_temp
            if self._coefficients:
                for k in ("fM_0","fM_1","fM_2","fM_3","fT_0","fT_1","fT_2","fT_3"):
                    setattr(profile, k, self._coefficients.get(k, 0))
            self._crystals.save(profile)
            return {"status": "ok"}

        @app.delete("/crystals/{name}")
        def delete_crystal(name: str):
            if not self._crystals.delete(name):
                raise HTTPException(404, "Crystal not found")
            if self._active_crystal == name:
                self._active_crystal = None
                self._save_settings()
            return {"status": "ok"}

        @app.get("/crystals/{name}/download")
        def download_crystal(name: str):
            path = self._crystals._path(name)
            if not os.path.exists(path):
                raise HTTPException(404, "Crystal not found")
            return FileResponse(path, filename=f"{name}.json", media_type="application/json")

        # ---- Server-side run logs (written to disk by the worker) ----

        @app.get("/runs")
        def list_runs():
            """Runs saved on the Pitaya, newest first.

            `current` is the run being recorded right now (or the last one this
            session) — the UI downloads that in preference to "newest by mtime";
            `recording` says which of the two it is. `free_bytes` drives the
            disk-full warning.

            A run long enough to have rolled over is several CSVs on disk (see
            run_logger.run_base) but one run here: `size` is the total across its
            parts and `parts` says how many there are. Nothing above this layer
            needs to know about parts — download and delete both take the run
            name and act on all of them.
            """
            out = _grouped_runs()
            try:
                usage = shutil.disk_usage(RUNS_DIR)
                free, total = usage.free, usage.total
            except OSError:
                free = total = None
            return {
                "runs": out,
                "current": run_base(self._current_run) if self._current_run else None,
                "recording": self._recording,
                "log_error": self._run_log_error,
                "free_bytes": free,
                "total_bytes": total,
                "min_free_bytes": MIN_FREE_BYTES,
            }

        @app.delete("/runs/{name}")
        def delete_run(name: str):
            """Delete a run and every part of it. Operator-driven: this is the
            confirmed "delete the oldest run" button in the UI. The logger also
            deletes on its own when the partition runs low, but through its own
            reclaim (see run_logger) rather than through here.

            Refused for the run being recorded right now. `current` keeps naming
            the last run of the session after it ends, so the refusal is keyed on
            whether the log is actually open — deleting the run you just finished
            is allowed."""
            base = run_base(os.path.basename(name))
            parts = _run_files(base)
            if not parts:
                raise HTTPException(404, "Run not found")
            if self._recording and self._current_run and run_base(self._current_run) == base:
                raise HTTPException(409, "That run is being recorded right now")
            freed, deleted, failed = 0, [], []
            for part in parts:
                path = os.path.join(RUNS_DIR, part)
                try:
                    size = os.path.getsize(path)
                    os.remove(path)
                except OSError as e:
                    # Keep going: the point of this button is free space, and the
                    # parts that did go are progress. The reply says what stuck.
                    print(f"[API] Could not delete {part}: {e}")
                    failed.append(part)
                    continue
                freed += size
                deleted.append(part)
            print(f"[API] Deleted run {base} ({len(deleted)} file(s), "
                  f"{freed / 1024 ** 2:.0f} MB) to free space")
            try:
                free = shutil.disk_usage(RUNS_DIR).free
            except OSError:
                free = None
            return {"status": "ok", "deleted": base, "parts": deleted, "failed": failed,
                    "freed_bytes": freed, "free_bytes": free}

        @app.post("/runs/retry-log")
        def retry_run_log():
            """Re-open the log for a measurement that is running unrecorded (after
            freeing space). Records the rest of the run; the lost part stays lost."""
            self.command_queue.put(RetryRunLogCommand())
            return {"status": "ok"}

        @app.get("/runs/{name}/download")
        def download_run(name: str):
            # run_base() strips any path components as well as the part suffix,
            # so there is no traversal and any part of a run resolves to the run.
            base = run_base(os.path.basename(name))
            parts = _run_files(base)
            if not parts:
                raise HTTPException(404, "Run not found")
            if len(parts) == 1:
                return FileResponse(os.path.join(RUNS_DIR, parts[0]),
                                    filename=base, media_type="text/csv")
            # A run that outgrew one file is still one run: serve the parts as a
            # single CSV, header once. Streamed rather than joined in memory —
            # the parts are tens of MB each and the box has 512 MB of RAM. If the
            # reclaim has since eaten the earliest parts, what is left downloads
            # as the run it now is; the remaining rows are untouched and each
            # part still carries the run's SETTINGS row.
            def stitched():
                header_sent = False
                for part in parts:
                    try:
                        fh = open(os.path.join(RUNS_DIR, part), "rb")
                    except OSError:
                        # Reclaimed between listing the parts and reading them:
                        # serve the rest rather than breaking the download over a
                        # file that no longer exists.
                        print(f"[API] {part} vanished mid-download (reclaimed?)")
                        continue
                    with fh:
                        # Keep the header from whichever part is served first —
                        # keyed on what has actually been sent, not on the part's
                        # position, or a first part that vanished just above would
                        # take the only header with it.
                        if header_sent:
                            fh.readline()  # drop the header this part repeats
                        header_sent = True
                        while True:
                            block = fh.read(_DOWNLOAD_BLOCK)
                            if not block:
                                break
                            yield block
            return StreamingResponse(stitched(), media_type="text/csv", headers={
                "Content-Disposition": f'attachment; filename="{base}"'})

        # ---- Sweep ----

        @app.post("/sweep/start")
        def start_sweep(oscillator_idx: int, step_size: float, settle_time: float,
                        center_freq: float | None = None, span: float | None = None,
                        start_freq: float | None = None, stop_freq: float | None = None):
            """Sweep one oscillator open-loop. Give the range as `center_freq` +
            `span` (what the web UI sends) or as `start_freq` + `stop_freq`.

            A centred sweep is laid out symmetrically on the step grid around the
            centre, so the centre is always one of the points: a span that is not
            a whole number of steps loses the remainder at both ends equally,
            rather than all of it at the top."""
            if step_size <= 0:
                raise HTTPException(400, "step_size must be greater than 0")
            if center_freq is not None and span is not None:
                if span < 0:
                    raise HTTPException(400, "span must not be negative")
                half = math.floor(span / 2 / step_size) * step_size
                start_freq, stop_freq = center_freq - half, center_freq + half
            elif start_freq is None or stop_freq is None:
                raise HTTPException(400, "give center_freq and span, or start_freq and stop_freq")
            if start_freq < 0 or stop_freq < start_freq:
                raise HTTPException(400, "the sweep must run upwards from 0 Hz or more")
            self.command_queue.put(StartSweepCommand(oscillator_idx, start_freq, stop_freq, step_size, settle_time))
            return {"status": "ok", "start_freq": start_freq, "stop_freq": stop_freq,
                    "n_points": int(math.floor((stop_freq - start_freq) / step_size)) + 1}

        @app.post("/sweep/abort")
        def abort_sweep():
            self.command_queue.put(AbortSweepCommand())
            return {"status": "ok"}

        # ---- Server-side sweep logs (written to disk by the worker) ----

        @app.get("/sweeps")
        def list_sweeps():
            """Sweep CSVs saved on the Pitaya, newest first. `current` is the
            sweep this session recorded last — the UI downloads that rather than
            whichever file happens to have the newest mtime."""
            out = []
            try:
                for f in os.listdir(SWEEPS_DIR):
                    if not f.endswith(".csv"):
                        continue
                    try:
                        st = os.stat(os.path.join(SWEEPS_DIR, f))
                        out.append({"name": f, "size": st.st_size, "modified": st.st_mtime})
                    except OSError:
                        pass
            except FileNotFoundError:
                pass
            out.sort(key=lambda r: r["modified"], reverse=True)
            return {"sweeps": out, "current": self._current_sweep}

        @app.get("/sweeps/{name}/download")
        def download_sweep(name: str):
            safe = os.path.basename(name)  # strip any path components (no traversal)
            path = os.path.join(SWEEPS_DIR, safe)
            if not safe.endswith(".csv") or not os.path.exists(path):
                raise HTTPException(404, "Sweep not found")
            return FileResponse(path, filename=safe, media_type="text/csv")

        @app.delete("/sweeps/{name}")
        def delete_sweep(name: str):
            """Delete one sweep CSV. Only ever called after the operator confirms
            the specific file in the UI."""
            safe = os.path.basename(name)
            path = os.path.join(SWEEPS_DIR, safe)
            if not safe.endswith(".csv") or not os.path.exists(path):
                raise HTTPException(404, "Sweep not found")
            size = os.path.getsize(path)
            os.remove(path)
            if safe == self._current_sweep:
                self._current_sweep = None
            print(f"[API] Deleted sweep {safe} ({size / 1024:.0f} kB)")
            return {"status": "ok", "deleted": safe, "freed_bytes": size}

        @app.get("/state")
        def get_state():
            return {"state": self._last_state}

        @app.get("/temperatures")
        def get_temperatures():
            """The latest read of the LTC2986 temperature sensors (°C), or why
            there is none. See domain/temperature_sensors.py for the channels."""
            if self._last_temperatures is None:
                return {"available": False, "sensors": [], "error": "no reading yet",
                        "timestamp": None}
            return {k: v for k, v in self._last_temperatures.items() if k != "type"}

        # ---- OPC-UA connection management ----

        @app.get("/opc/settings")
        def get_opc_settings():
            c = self._wago_client
            return {
                # `available` is false only when the server was started with
                # --no-opcua; `enabled` is the on/off setting.
                "available": self._opc_worker is not None,
                "enabled":   self._opc_enabled,
                "url":       c.url       if c else "",
                "user":      c.user      if c else "",
                "base_node": c.base_node_path if c else "",
                "connected": c.is_connected if c else False,
                # The password itself is never handed back out; the UI only needs
                # to know whether one is stored so it can say so in the field.
                "has_password": bool(c.password) if c else False,
            }

        @app.post("/opc/enabled")
        def set_opc_enabled(enabled: bool):
            """Switch the OPC-UA bridge on or off, immediately and persistently.
            Off (the default) it holds no connection, so the PLC can neither read
            the QCM nor trigger GET LOCK / START / STOP. The connection settings
            are kept either way."""
            if not self._opc_worker:
                raise HTTPException(503, "OPC-UA bridge not available (server started with --no-opcua)")
            self._opc_enabled = enabled
            self._opc_worker.set_enabled(enabled)
            self._save_settings()
            return {"status": "ok", "enabled": enabled}

        @app.post("/opc/connect")
        def opc_connect(url: str, user: str = "", password: str | None = None,
                        base_node: str | None = None):
            """Set the connection and reconnect with it. With the bridge switched
            off the settings are saved and used once it is switched on."""
            if not self._wago_client:
                raise HTTPException(503, "OPC-UA bridge not available (server started with --no-opcua)")
            if base_node is not None:
                self._wago_client.set_base_node_path(base_node)
            # An omitted password means "keep the stored one". The UI never
            # receives the saved password back, so its field is empty after every
            # page load — sending that blank verbatim used to wipe the stored
            # password whenever only the URL or username was edited.
            pwd = self._wago_client.password if password is None else password
            self._wago_client.set_connection(url, user, pwd)
            self._save_settings()
            return {"status": "ok", "url": url, "enabled": self._opc_enabled}

        @app.get("/opc/nodes")
        def opc_nodes():
            """The OPC-UA node tree this system uses, so it can be replicated on
            the PLC's OPC server. node_id is the string identifier (the 's=' part)."""
            base = self._wago_client.base_node_path if self._wago_client else ""
            def entries(keys, direction):
                return [{"key": k, "direction": direction, "node_id": base + k} for k in keys]
            return {
                "base_node_path": base,
                # READ = published by the QCM (PLC reads). CTRL = PLC writes a
                # rising edge to trigger the action (params come from REST).
                "read": entries(OPC_READ_KEYS, "QCM → PLC"),
                "ctrl": entries(OPC_CTRL_KEYS, "PLC → QCM"),
            }

        # ---- System ----

        @app.post("/system/reboot")
        def reboot(force: bool = False):
            """Reboot the Red Pitaya. Everything restarts: the FPGA bitstream is
            reloaded, the PLLs come up unlocked and the OPC link reconnects, so
            it is the way out of a wedged FPGA or a hung network stack.

            A reboot during a measurement ends that run where it stands, so it
            takes an explicit force — the UI asks a second time before sending it.
            """
            if self._last_state == "RUNNING" and not force:
                raise HTTPException(409, "A measurement is running — stop it first, or pass force=true")
            print("[API] Reboot requested — going down now")
            threading.Thread(target=self._do_reboot, daemon=True, name="reboot").start()
            return {"status": "rebooting"}

        @app.get("/version")
        def version():
            """What this box is running: the software commit the server started
            from (named by git describe, with restart_needed when the checkout
            has moved on since) and the bitstream it loaded. See domain/version.py."""
            if self._version is None:
                raise HTTPException(503, "Version information is not available")
            return self._version.as_dict()

        # ---- Health ----

        @app.get("/health")
        def health():
            return {"status": "ok"}

        # ---- Static files (for frontend) ----
                
        # StaticFiles types a response from `mimetypes`, which is seeded from the
        # host: on Linux from /etc/mime.types, on Windows from the registry —
        # where .js can come back as text/plain and .woff2 as nothing at all
        # (starlette then sends text/plain). Browsers tolerate that today only
        # because these responses carry no nosniff header. Nail the two types the
        # vendored offline assets depend on, so the UI does not rest on the MIME
        # database of whichever box happens to be serving it.
        mimetypes.add_type("text/javascript", ".js")
        mimetypes.add_type("font/woff2", ".woff2")

        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


        return app