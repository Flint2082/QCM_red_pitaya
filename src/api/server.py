# Responsible for:
#
# Exposing REST endpoints for control commands
# Streaming measurement events to clients over WebSocket
# Translating HTTP/WS messages into app-layer commands/events
# No business logic — pure transport

import asyncio
import os
import queue
import threading
from contextlib import asynccontextmanager

import app
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from messaging.worker_command import *


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
        self.active.remove(ws)

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
    def __init__(self, app_command_queue: queue.Queue, app_event_queue: queue.Queue):
        self.command_queue = app_command_queue
        self.event_queue = app_event_queue
        self.manager = ConnectionManager()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.app = self._build_app()
        


    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="api-server")
        self._thread.start()

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
                await self.manager.broadcast(self._serialise_event(event))
            except queue.Empty:
                await asyncio.sleep(0.01)

    @staticmethod
    def _serialise_event(event) -> dict:
        """Convert an event object to a JSON-serialisable dict."""
        return {
            "type": type(event).__name__,
            **vars(event),
        }

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
            try:
                while True:
                    await ws.receive_text()   # keep connection alive; we only push, not receive
            except WebSocketDisconnect:
                self.manager.disconnect(ws)

        # ---- Measurement control ----

        @app.post("/measurement/start")
        def start_measurement():
            self.command_queue.put(StartMeasurementCommand())
            return {"status": "ok"}
        
        @app.post("/measurement/get_lock")
        def get_lock():
            self.command_queue.put(StartupPLLCommand())
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
            self.command_queue.put(SetIntegratorGainCommand(oscillator_idx, gain))
            return {"status": "ok"}

        # ---- Sweep ----

        @app.post("/sweep/start")
        def start_sweep(start_freq: float, stop_freq: float, step_size: float, settle_time: float):
            self.command_queue.put(StartSweepCommand(start_freq, stop_freq, step_size, settle_time))
            return {"status": "ok"}

        # ---- Health ----

        @app.get("/health")
        def health():
            return {"status": "ok"}
        
        # ---- Static files (for frontend) ----
                
        static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")


        return app