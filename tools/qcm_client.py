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
(`pip install -r requirements-client.txt`, or `--no-index --find-links
vendor/wheels` on a PC with no internet — see OFFLINE.md).
"""

import os
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

    def _download(self, path: str, dest_path: str) -> str:
        r = requests.get(self.base + path, timeout=self.timeout)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(r.content)
        return dest_path

    # ------------------------------------------------------- measurement control
    def get_lock(self):
        return self._post("/measurement/get_lock")

    def start_measurement(self, ambient_temp: float = 23.0, mat_dens: float = 19320.0, z_ratio: float = 1.0):
        return self._post("/measurement/start", ambient_temp=ambient_temp, mat_dens=mat_dens, z_ratio=z_ratio)

    def stop_measurement(self):
        return self._post("/measurement/stop")

    def state(self) -> str:
        return self._get("/state")["state"]

    # ------------------------------------------------------------ target thickness
    def set_target_thickness(self, value: float):
        """Compensated thickness (nm) at which the target flag is raised; 0 = off.
        Reaching it sets the flag here and on the PLC but does not stop the run."""
        return self._post("/settings/target_thickness", value=value)

    def target(self) -> dict:
        """{target_thickness, enabled, reached, reached_at, thickness_at_target}."""
        return self._get("/measurement/target")

    # -------------------------------------------------- oscillator / lock settings
    def set_frequency(self, oscillator_idx: int, frequency: float):
        return self._post("/settings/frequency", oscillator_idx=oscillator_idx, frequency=frequency)

    def set_integrator_gain(self, oscillator_idx: int, gain: float):
        return self._post("/settings/integrator_gain", oscillator_idx=oscillator_idx, gain=gain)

    def set_lpf_freq(self, oscillator_idx: int, freq: float):
        """Demodulator low-pass cutoff frequency in Hz."""
        return self._post("/settings/lpf_freq", oscillator_idx=oscillator_idx, freq=freq)

    def set_inverted(self, oscillator_idx: int, inverted: bool):
        return self._post("/settings/inverted", oscillator_idx=oscillator_idx, inverted=bool(inverted))

    def set_agc(self, oscillator_idx: int, enabled: bool,
                manual_gain: float = 0.25, target_amp: float = 1024.0):
        """Drive gain for one oscillator (FPGA register output_gain_N, Fixed
        32.32). With `enabled` the gain is servoed while that channel is locked
        so the demodulated amplitude holds at `target_amp`, which keeps the
        loop's SNR — and the phase noise the lock detect judges on — steady as
        the crystal's response falls off with temperature and film loading.

        Unlocked, and in the manual override (`enabled` False), the drive is
        `manual_gain` instead: off resonance the amplitude coming back says
        nothing about the drive a resonance needs. Sweeps, acquisition and the
        capacitor adjustment therefore always run at the manual drive.

        `manual_gain` runs 0 to 1.0 — the register being all fraction, 1.0 is
        both the undivided drive the hardware put out before it existed and the
        ceiling; the default is a quarter of that. `target_amp` is in the
        same units as `amp_mass`/`amp_temp` in the telemetry — take one from a
        good lock rather than guessing, or the gain just runs to its limit."""
        return self._post("/settings/agc", oscillator_idx=oscillator_idx,
                          enabled=bool(enabled), manual_gain=manual_gain, target_amp=target_amp)

    def set_output_mode(self, mode: int):
        return self._post("/settings/output_mode", mode=mode)

    def set_lock_detect(self, phase_tolerance: float, phase_std: float):
        """Lock-detect limits, both in radians: max |mean phase error| and max
        phase-error standard deviation over the lock window (lock quality)."""
        return self._post("/settings/lock_detect", phase_tolerance=phase_tolerance, phase_std=phase_std)

    def set_auto_relock(self, enabled: bool):
        """Automatically re-acquire when lock is lost mid-measurement (default on)."""
        return self._post("/settings/auto_relock", enabled=bool(enabled))

    def set_temp_hold(self, enabled: bool):
        """Hold the last calculated temperature while the temp-mode lock is lost
        (default on), instead of solving the compensation from an fT that is no
        longer sitting on a resonance. The mass mode keeps being measured, so
        only the thermal correction freezes — the compensated thickness carries
        on following the film. Held samples are the ones with lock_temp = 0."""
        return self._post("/settings/temp_hold", enabled=bool(enabled))

    def set_freq_tracking(self, enabled: bool, interval: float = 10.0):
        """Follow the locked frequencies (default on, every 10 s): while both
        modes are locked, the re-lock start frequencies are re-pointed at what
        the PLLs are actually holding, so a lost lock is re-acquired from where
        the resonances are now rather than where the crystal profile says they
        were. The profile on disk is left alone — use save_current_to_crystal()
        to keep a tracked set. Minimum interval 1 s."""
        return self._post("/settings/freq_tracking", enabled=bool(enabled), interval=interval)

    def set_storage_reclaim(self, prune_old_runs: bool, trim_current_run: bool):
        """What the run logger may delete when the data partition runs low
        (both on by default). `prune_old_runs` gives up finished runs, oldest
        first; `trim_current_run` drops the oldest chunks of the run in
        progress, which always keeps the chunk it is writing. Both are
        permanent — a run CSV is the only copy of that run — and with both off a
        full disk stops the recording instead."""
        return self._post("/settings/storage_reclaim",
                          prune_old_runs=bool(prune_old_runs),
                          trim_current_run=bool(trim_current_run))

    def set_lock_frequencies(self, mass: float, temp: float):
        return self._post("/settings/lock_frequencies", mass=mass, temp=temp)

    def get_lock_frequencies(self):
        return self._get("/settings/lock_frequencies")

    def set_coefficients(self, fM_0, fM_1, fM_2, fM_3, fT_0, fT_1, fT_2, fT_3):
        return self._post("/settings/coefficients", fM_0=fM_0, fM_1=fM_1, fM_2=fM_2, fM_3=fM_3,
                          fT_0=fT_0, fT_1=fT_1, fT_2=fT_2, fT_3=fT_3)

    def settings(self) -> dict:
        return self._get("/settings")

    def get_coefficients(self) -> dict:
        """The compensation coefficients in force (from the active crystal)."""
        return self._get("/settings/coefficients")

    def set_measurement_params(self, ambient_temp: float, mat_dens: float, z_ratio: float,
                               target_thickness: float | None = None):
        """Persist the film/run parameters (and optionally the target) without
        starting a run. They take effect at the next start_measurement()."""
        return self._post("/settings/measurement_params", ambient_temp=ambient_temp,
                          mat_dens=mat_dens, z_ratio=z_ratio, target_thickness=target_thickness)

    # ------------------------------------------------------------------ defaults
    def defaults(self) -> dict:
        """The standard settings restore_defaults() returns to. They ship with
        the software (DEFAULT_SETTINGS in src/api/server.py), so they are the
        same on every instrument and cannot be changed over the API."""
        return self._get("/settings/defaults")

    def restore_defaults(self) -> dict:
        """Put the standard settings back into force immediately. The active
        crystal and the OPC connection describe this particular box, so they are
        left as they are."""
        return self._post("/settings/restore_defaults")

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

    def upload_crystal(self, path: str) -> dict:
        """Upload a profile JSON. The filename (minus .json) becomes its name;
        uploading does not activate it — call activate_crystal() for that."""
        with open(path, "rb") as f:
            r = requests.post(f"{self.base}/crystals/upload",
                              files={"file": (os.path.basename(path), f, "application/json")},
                              timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def download_crystal(self, name: str, dest_path: str):
        return self._download(f"/crystals/{name}/download", dest_path)

    # -------------------------------------------------------------------- sweep
    def start_sweep(self, oscillator_idx: int, start_freq: float, stop_freq: float,
                    step_size: float, settle_time: float):
        return self._post("/sweep/start", oscillator_idx=oscillator_idx, start_freq=start_freq,
                          stop_freq=stop_freq, step_size=step_size, settle_time=settle_time)

    def abort_sweep(self):
        return self._post("/sweep/abort")

    # Window and step used by the SWEEP fM / SWEEP fT buttons in the web UI.
    # Keep these in step with MODE_SWEEP_WINDOW / MODE_SWEEP_STEP in
    # src/static/index.html — reproducing those buttons is the whole point of
    # sweep_mode(), and the two have drifted apart once already.
    MODE_SWEEP_WINDOW = 50000.0  # Hz, total span (25 kHz either side of centre)
    MODE_SWEEP_STEP   = 100.0    # Hz

    def sweep_mode(self, oscillator_idx: int, settle_time: float = 0.05):
        """Sweep the standard window around the active crystal's lock frequency
        for one mode (1 = mass, 2 = temp) — the scripted form of the SWEEP fM /
        SWEEP fT buttons. Returns the sweep parameters that were started."""
        freqs = self.get_lock_frequencies()
        centre = freqs["mass"] if oscillator_idx == 1 else freqs["temp"]
        if not centre:
            raise RuntimeError(f"No lock frequency set for oscillator {oscillator_idx}")
        start = centre - self.MODE_SWEEP_WINDOW / 2
        stop = centre + self.MODE_SWEEP_WINDOW / 2
        self.start_sweep(oscillator_idx, start, stop, self.MODE_SWEEP_STEP, settle_time)
        return {"oscillator_idx": oscillator_idx, "start_freq": start, "stop_freq": stop,
                "step_size": self.MODE_SWEEP_STEP, "settle_time": settle_time}

    # --------------------------------------------------------- server-side sweeps
    def sweeps(self) -> list:
        return self._get("/sweeps")["sweeps"]

    def download_sweep(self, name: str, dest_path: str):
        return self._download(f"/sweeps/{name}/download", dest_path)

    def download_latest_sweep(self, dest_path: str):
        sweeps = self.sweeps()
        if not sweeps:
            raise RuntimeError("No server-side sweeps available")
        return self.download_sweep(sweeps[0]["name"], dest_path)

    def delete_sweep(self, name: str):
        return self._delete(f"/sweeps/{name}")

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

    def opc_connect(self, url: str, user: str = "", password: str | None = None,
                    base_node: str | None = None):
        """Point the bridge at an OPC-UA server and reconnect.

        password=None keeps the stored one. The server never hands the password
        back, so a client that always sent a value would wipe it every time only
        the URL or the user changed."""
        return self._post("/opc/connect", url=url, user=user, password=password, base_node=base_node)

    # ----------------------------------------------------------- server-side runs
    def runs(self) -> list:
        """Runs on the Pitaya, newest first. A long run is several CSVs on the
        box but one entry here: `size` totals them, `parts` counts them, and
        download_run() serves them back as a single CSV."""
        return self._get("/runs")["runs"]

    def runs_info(self) -> dict:
        """The whole /runs payload: the runs, the one being recorded right now,
        free/total bytes, and the free space a run log needs to open."""
        return self._get("/runs")

    def download_run(self, name: str, dest_path: str):
        return self._download(f"/runs/{name}/download", dest_path)

    def download_latest_run(self, dest_path: str):
        runs = self.runs()
        if not runs:
            raise RuntimeError("No server-side runs available")
        return self.download_run(runs[0]["name"], dest_path)

    def delete_run(self, name: str):
        """Delete a run and every file it consists of. It is the only copy of
        that run — nothing else holds it. Refused with HTTP 409 for the run being
        recorded right now (the run you just finished can be deleted)."""
        return self._delete(f"/runs/{name}")

    def retry_run_log(self):
        """Re-open the log for a run that is going unrecorded, once space has
        been freed. Records the rest of the run; what was lost stays lost."""
        return self._post("/runs/retry-log")

    # ------------------------------------------------------------------- system
    def reboot(self, force: bool = False):
        """Reboot the Red Pitaya. The FPGA bitstream reloads and the PLLs come up
        unlocked, so it is the way out of a wedged instrument. Refused with HTTP
        409 while a measurement is running unless force=True — a reboot ends that
        run where it stands."""
        return self._post("/system/reboot", force=force)

    def health(self) -> dict:
        """Liveness check — {"status": "ok"} while the server is up."""
        return self._get("/health")

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
