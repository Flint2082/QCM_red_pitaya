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
import os
import queue
import re
import sys
import threading
import time
from contextlib import asynccontextmanager

import app
import uvicorn
from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from domain.crystal import CrystalManager, CrystalProfile, sanitize_name
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
                 wago_client=None):
        self.command_queue = api_command_queue
        self.event_queue = api_event_queue
        self._wago_client = wago_client  # optional reference for live reconfiguration
        self.manager = ConnectionManager()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_state: str = "IDLE"
        self._last_opc_status: dict | None = None
        # Lock frequencies used by the GET LOCK command
        self._lock_freq_mass: float = 5983000.0
        self._lock_freq_temp: float = 6570000.0
        # Oscillator settings cache — defaults match QCMInterface post-lock state
        self._osc_settings: dict = {
            1: {"int_gain": 0.00001, "iq_gain": 0.00001, "inverted": True},
            2: {"int_gain": 0.00001, "iq_gain": 0.00001, "inverted": True},
        }
        self._output_mode: int = 0
        # Coefficient cache (lazy-loaded from CSV on first GET)
        self._coeff_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "coeffecients.csv")
        self._coefficients: dict | None = None
        # Crystal profiles
        self._crystals = CrystalManager()
        self._active_crystal: str | None = None
        # Stats tracking (updated in event broadcaster)
        self._measurement_start_time: float | None = None
        self._session_thickness: float = 0.0
        # Persistent settings file
        self._settings_file = os.path.join(os.path.dirname(__file__), "..", "..", "data", "settings.json")
        self._load_settings()   # override defaults with last saved values
        self.app = self._build_app()
        


    # --------------------------------------------------
    # Persistent settings
    # --------------------------------------------------

    def _load_settings(self):
        try:
            with open(self._settings_file) as f:
                d = json.load(f)
        except FileNotFoundError:
            return  # first run — keep defaults
        except Exception as e:
            print(f"[Settings] Load failed: {e}")
            return

        if "osc_settings" in d:
            self._osc_settings = {int(k): v for k, v in d["osc_settings"].items()}
        if "output_mode" in d:
            self._output_mode = int(d["output_mode"])
        if "lock_freq_mass" in d:
            self._lock_freq_mass = float(d["lock_freq_mass"])
        if "lock_freq_temp" in d:
            self._lock_freq_temp = float(d["lock_freq_temp"])
        if "active_crystal" in d:
            self._active_crystal = d["active_crystal"]
        # Restore OPC connection parameters without triggering a reconnect
        if self._wago_client and "opc_url" in d:
            self._wago_client.url      = d["opc_url"]
            self._wago_client.user     = d.get("opc_user", "")
            self._wago_client.password = d.get("opc_password", "")

    def _save_settings(self):
        data = {
            "osc_settings":   {str(k): v for k, v in self._osc_settings.items()},
            "output_mode":    self._output_mode,
            "lock_freq_mass": self._lock_freq_mass,
            "lock_freq_temp": self._lock_freq_temp,
            "active_crystal": self._active_crystal,
        }
        if self._wago_client:
            data["opc_url"]      = self._wago_client.url
            data["opc_user"]     = self._wago_client.user
            data["opc_password"] = self._wago_client.password
        try:
            os.makedirs(os.path.dirname(self._settings_file), exist_ok=True)
            with open(self._settings_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Settings] Save failed: {e}")

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
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

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
            log_level="info",
        )
        server = uvicorn.Server(config)
        self._loop.run_until_complete(server.serve())

    # --------------------------------------------------
    # Event broadcaster (event_queue → WebSocket clients)
    # --------------------------------------------------

    async def _event_broadcaster(self):
        while True:
            try:
                event = self.event_queue.get_nowait()
                msg = self._serialise_event(event)

                if msg.get("type") == "StateEvent":
                    new_state = msg.get("state", "IDLE")
                    old_state = self._last_state
                    self._last_state = new_state
                    # Start tracking when measurement begins
                    if new_state == "RUNNING" and old_state != "RUNNING":
                        self._measurement_start_time = time.time()
                        self._session_thickness = 0.0
                    # Flush stats when measurement ends
                    elif old_state == "RUNNING" and new_state != "RUNNING":
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

                elif msg.get("type") == "OpcStatusEvent":
                    self._last_opc_status = msg

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

        app = FastAPI(title="QCM API", version="0.1.0", lifespan=lifespan)
        
        # ---- WebSocket ----

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await self.manager.connect(ws)
            await ws.send_json({"type": "StateEvent", "state": self._last_state})
            if self._last_opc_status:
                await ws.send_json(self._last_opc_status)
            try:
                while True:
                    await ws.receive_text()   # keep connection alive; we only push, not receive
            except WebSocketDisconnect:
                self.manager.disconnect(ws)

        # ---- Measurement control ----

        @app.post("/measurement/start")
        def start_measurement(ambient_temp: float):
            self.command_queue.put(StartMeasurementCommand(ambient_temp=ambient_temp))
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

        @app.post("/settings/iq_gain")
        def set_iq_gain(oscillator_idx: int, gain: float):
            self._osc_settings.setdefault(oscillator_idx, {})["iq_gain"] = gain
            self.command_queue.put(SetIQGainCommand(oscillator_idx, gain))
            self._save_settings()
            return {"status": "ok"}

        @app.post("/settings/inverted")
        def set_inverted(oscillator_idx: int, inverted: bool):
            self._osc_settings.setdefault(oscillator_idx, {})["inverted"] = inverted
            self.command_queue.put(SetInvertedCommand(oscillator_idx, inverted))
            self._save_settings()
            return {"status": "ok"}

        @app.get("/settings/lock_frequencies")
        def get_lock_frequencies():
            return {"mass": self._lock_freq_mass, "temp": self._lock_freq_temp}

        @app.post("/settings/lock_frequencies")
        def set_lock_frequencies(mass: float, temp: float):
            self._lock_freq_mass = mass
            self._lock_freq_temp = temp
            self._save_settings()
            return {"status": "ok"}

        @app.get("/settings/coefficients")
        def get_coefficients():
            if self._coefficients is None:
                import csv as _csv
                try:
                    with open(self._coeff_file) as f:
                        reader = _csv.DictReader(f)
                        self._coefficients = {
                            row['Name']: (lambda v: v if math.isfinite(v) else None)(float(row['value']))
                            for row in reader
                        }
                except Exception:
                    self._coefficients = {}
            return self._coefficients

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
            self._output_mode = mode
            self.command_queue.put(SetOutputModeCommand(1, OutputMode(mode)))
            self._save_settings()
            return {"status": "ok"}

        def _finite(v):
            return v if isinstance(v, float) and math.isfinite(v) else (None if isinstance(v, float) else v)

        @app.get("/settings")
        def get_settings():
            return {
                "oscillators":      self._osc_settings,
                "output_mode":      self._output_mode,
                "lock_frequencies": {
                    "mass": _finite(self._lock_freq_mass),
                    "temp": _finite(self._lock_freq_temp),
                },
                "coefficients": {k: _finite(v) for k, v in (self._coefficients or {}).items()},
            }

        # ---- Crystal profiles ----

        def _apply_crystal(profile: CrystalProfile):
            """Push a crystal's settings into server state and command queue."""
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
        ):
            """Save explicit crystal data from the settings form and apply it immediately."""
            profile = self._crystals.load(name) or CrystalProfile(name=name)
            profile.freq_mass = freq_mass
            profile.freq_temp = freq_temp
            profile.fM_0, profile.fM_1, profile.fM_2, profile.fM_3 = fM_0, fM_1, fM_2, fM_3
            profile.fT_0, profile.fT_1, profile.fT_2, profile.fT_3 = fT_0, fT_1, fT_2, fT_3
            self._crystals.save(profile)
            _apply_crystal(profile)
            self._active_crystal = name
            self._save_settings()
            return {"status": "ok"}

        @app.post("/crystals/{name}/activate")
        def activate_crystal(name: str):
            profile = self._crystals.load(name)
            if not profile:
                raise HTTPException(404, "Crystal not found")
            _apply_crystal(profile)
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

        # ---- Sweep ----

        @app.post("/sweep/start")
        def start_sweep(oscillator_idx: int, start_freq: float, stop_freq: float, step_size: float, settle_time: float):
            self.command_queue.put(StartSweepCommand(oscillator_idx, start_freq, stop_freq, step_size, settle_time))
            return {"status": "ok"}

        @app.post("/sweep/abort")
        def abort_sweep():
            self.command_queue.put(AbortSweepCommand())
            return {"status": "ok"}

        @app.get("/state")
        def get_state():
            return {"state": self._last_state}

        # ---- OPC-UA connection management ----

        @app.get("/opc/settings")
        def get_opc_settings():
            c = self._wago_client
            return {
                "url":       c.url       if c else "",
                "user":      c.user      if c else "",
                "connected": c.is_connected if c else False,
            }

        @app.post("/opc/connect")
        def opc_connect(url: str, user: str = "", password: str = ""):
            if not self._wago_client:
                raise HTTPException(503, "OPC-UA bridge not enabled")
            self._wago_client.set_connection(url, user, password)
            self._save_settings()
            return {"status": "ok", "url": url}

        # ---- Health ----

        @app.get("/health")
        def health():
            return {"status": "ok"}

        # ---- Static files (for frontend) ----
                
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


        return app