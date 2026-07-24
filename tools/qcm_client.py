"""Python control client for the QCM Red Pitaya.

A thin wrapper that translates the web/REST commands into Python methods, so the
instrument can be driven from a script instead of the browser. Every method maps
to one HTTP endpoint of the running control software.

    from qcm_client import QCMClient
    qcm = QCMClient("192.168.1.50")        # host of the Red Pitaya
    qcm.get_lock()
    qcm.start_measurement(ambient_temp=23)
    for m in qcm.stream_measurements(limit=10):
        print(m.calculated_thickness, m.calculated_temp)
    qcm.stop_measurement()

Requires `requests`; live streaming additionally needs `websocket-client`
(`pip install requests websocket-client`).
"""

from types import SimpleNamespace

import requests


class QCMClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 8000, timeout: float = 5.0):
        self.base = f"http://{host}:{port}"
        self.ws_url = f"ws://{host}:{port}/ws"
        self.timeout = timeout

    # ------------------------------------------------------------------ HTTP
    def _request(self, method: str, path: str, **params):
        # The server takes its parameters as query string args, not JSON bodies.
        clean = {k: v for k, v in params.items() if v is not None}
        r = requests.request(method, self.base + path, params=clean or None, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else None

    def _get(self, path, **p):    return self._request("GET", path, **p)
    def _post(self, path, **p):   return self._request("POST", path, **p)
    def _delete(self, path, **p): return self._request("DELETE", path, **p)

    # ------------------------------------------------------- measurement control
    def get_lock(self):
        return self._post("/measurement/get_lock")

    def start_measurement(self, ambient_temp: float = 23.0, mat_dens: float = 19320.0, z_ratio: float = 1.0):
        return self._post("/measurement/start", ambient_temp=ambient_temp, mat_dens=mat_dens, z_ratio=z_ratio)

    def stop_measurement(self):
        return self._post("/measurement/stop")

    def state(self) -> str:
        return self._get("/state")["state"]

    # -------------------------------------------------- oscillator / lock settings
    def set_frequency(self, oscillator_idx: int, frequency: float):
        return self._post("/settings/frequency", oscillator_idx=oscillator_idx, frequency=frequency)

    def set_integrator_gain(self, oscillator_idx: int, gain: float):
        return self._post("/settings/integrator_gain", oscillator_idx=oscillator_idx, gain=gain)

    def set_proportional_gain(self, oscillator_idx: int, gain: float):
        return self._post("/settings/proportional_gain", oscillator_idx=oscillator_idx, gain=gain)

    def set_lpf_freq(self, oscillator_idx: int, freq: float):
        """Demodulator low-pass cutoff frequency in Hz."""
        return self._post("/settings/lpf_freq", oscillator_idx=oscillator_idx, freq=freq)

    def set_inverted(self, oscillator_idx: int, inverted: bool):
        return self._post("/settings/inverted", oscillator_idx=oscillator_idx, inverted=bool(inverted))

    def set_phase_detect(self, oscillator_idx: int, mode: int):
        """Phase-detector type (FPGA mult_sel): 0 = ATAN (default), 1 = multiplier."""
        return self._post("/settings/phase_detect", oscillator_idx=oscillator_idx, mode=int(mode))

    def set_output_mode(self, mode: int):
        return self._post("/settings/output_mode", mode=mode)

    def set_lock_detect(self, amp_threshold: float, phase_tolerance: float):
        return self._post("/settings/lock_detect", amp_threshold=amp_threshold, phase_tolerance=phase_tolerance)

    def set_auto_relock(self, enabled: bool):
        """Automatically re-acquire when lock is lost mid-measurement (default on)."""
        return self._post("/settings/auto_relock", enabled=bool(enabled))

    def set_auto_amp_threshold(self, enabled: bool):
        """Auto-set the lock amplitude threshold to 80% of the end-of-run amplitude (default on)."""
        return self._post("/settings/auto_amp_threshold", enabled=bool(enabled))

    def set_lock_frequencies(self, mass: float, temp: float):
        return self._post("/settings/lock_frequencies", mass=mass, temp=temp)

    def get_lock_frequencies(self):
        return self._get("/settings/lock_frequencies")

    def set_coefficients(self, fM_0, fM_1, fM_2, fM_3, fT_0, fT_1, fT_2, fT_3):
        return self._post("/settings/coefficients", fM_0=fM_0, fM_1=fM_1, fM_2=fM_2, fM_3=fM_3,
                          fT_0=fT_0, fT_1=fT_1, fT_2=fT_2, fT_3=fT_3)

    def settings(self) -> dict:
        return self._get("/settings")

    # ----------------------------------------------------------------- crystals
    def crystals(self) -> dict:
        return self._get("/crystals")

    def crystal(self, name: str) -> dict:
        return self._get(f"/crystals/{name}")

    def create_crystal(self, name: str):
        return self._post("/crystals", name=name)

    def activate_crystal(self, name: str):
        return self._post(f"/crystals/{name}/activate")

    def apply_crystal(self, name: str, **fields):
        """fields: freq_mass, freq_temp, fM_0..fT_3, mass_sensitivity, sens_area, freq_virgin, tooling_ratio."""
        return self._post(f"/crystals/{name}/apply", **fields)

    def save_current_to_crystal(self, name: str):
        return self._post(f"/crystals/{name}/save_current")

    def delete_crystal(self, name: str):
        return self._delete(f"/crystals/{name}")

    # -------------------------------------------------------------------- sweep
    def start_sweep(self, oscillator_idx: int, start_freq: float, stop_freq: float,
                    step_size: float, settle_time: float):
        return self._post("/sweep/start", oscillator_idx=oscillator_idx, start_freq=start_freq,
                          stop_freq=stop_freq, step_size=step_size, settle_time=settle_time)

    def abort_sweep(self):
        return self._post("/sweep/abort")

    # ------------------------------------------------------------ capacitor adjust
    def start_cap_adjust(self):
        return self._post("/cap_adjust/start")

    def stop_cap_adjust(self):
        return self._post("/cap_adjust/stop")

    # ---------------------------------------------------------------------- OPC
    def opc_settings(self) -> dict:
        return self._get("/opc/settings")

    def opc_nodes(self) -> dict:
        return self._get("/opc/nodes")

    def opc_connect(self, url: str, user: str = "", password: str = "", base_node: str | None = None):
        return self._post("/opc/connect", url=url, user=user, password=password, base_node=base_node)

    # ----------------------------------------------------------- server-side runs
    def runs(self) -> list:
        return self._get("/runs")["runs"]

    def download_run(self, name: str, dest_path: str):
        r = requests.get(f"{self.base}/runs/{name}/download", timeout=self.timeout)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return dest_path

    def download_latest_run(self, dest_path: str):
        runs = self.runs()
        if not runs:
            raise RuntimeError("No server-side runs available")
        return self.download_run(runs[0]["name"], dest_path)

    # ------------------------------------------------------------- live streaming
    def stream(self, types=("MeasurementEvent",), limit=None):
        """Yield live events from the WebSocket as attribute-access objects.

        `types` filters by event type; `limit` stops after N matching events.
        Needs `websocket-client` (pip install websocket-client)."""
        try:
            import websocket  # websocket-client
        except ImportError as e:
            raise RuntimeError("Live streaming needs websocket-client (pip install websocket-client)") from e

        import json
        ws = websocket.create_connection(self.ws_url)
        count = 0
        try:
            while True:
                ev = json.loads(ws.recv())
                if types is None or ev.get("type") in types:
                    yield SimpleNamespace(**ev)
                    count += 1
                    if limit is not None and count >= limit:
                        return
        finally:
            ws.close()

    def stream_measurements(self, limit=None):
        """Convenience: yield only MeasurementEvents (with .calculated_thickness,
        .calculated_temp, .freq_mass_mode, .compensated_freq, ... attributes)."""
        return self.stream(types=("MeasurementEvent",), limit=limit)
