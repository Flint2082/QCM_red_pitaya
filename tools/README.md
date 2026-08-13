# Controlling the QCM from Python

The QCM control software on the Red Pitaya exposes an HTTP + WebSocket API (the
same one the web UI uses). [`qcm_client.py`](qcm_client.py) is a thin Python
wrapper around that API, so you can drive the instrument from a script instead
of the browser: lock the PLLs, start/stop a measurement, change settings,
manage crystal profiles, and stream live data.

- **REST (HTTP)** — every action and setting. One method per endpoint.
- **WebSocket** (`/ws`) — live push stream of measurements and state changes.

---

## 1. Prerequisites

The control software must already be running on the Red Pitaya. If you set it
up as a service it is always running; otherwise start it over SSH:

```bash
ssh root@<red_pitaya_ip>
cd /root/QCM_red_pitaya
.venv/bin/python src/main.py          # add --no-opcua if there is no PLC
```

See [src/README.md](../src/README.md) for full setup and the systemd service
(`systemctl status qcm`).

The server listens on **port 8000** on all interfaces, so from your PC it is
reachable at `http://<red_pitaya_ip>:8000`. Quick sanity check:

```bash
curl http://<red_pitaya_ip>:8000/health        # -> {"status":"ok"}
```

## 2. Install the client dependencies (on your PC)

The client needs `requests`; live streaming additionally needs
`websocket-client`:

```bash
pip install requests websocket-client
```

Both are listed at the bottom of [requirements.txt](../requirements.txt).

## 3. Connect

`qcm_client.py` has no package install step — just import it from the `tools/`
folder (run your script from there, or add `tools/` to `sys.path`).

```python
from qcm_client import QCMClient

qcm = QCMClient("192.168.1.50")     # host of the Red Pitaya; port defaults to 8000
print(qcm.state())                  # "IDLE", "LOCKING", "RUNNING", ...
```

Every method maps to one endpoint and returns the parsed JSON response (most
write calls return `{"status": "ok"}`). HTTP errors raise
`requests.HTTPError`.

---

## 4. Typical workflow

The normal sequence is **get lock → start measurement → read data → stop**.

```python
import time
from qcm_client import QCMClient

qcm = QCMClient("192.168.1.50")

# 1. Pick the crystal whose calibration you want to use (lock frequencies +
#    polynomial coefficients come from this profile).
qcm.activate_crystal("AT_cut_6MHz")

# 2. Lock the two PLLs onto the crystal's resonances.
qcm.get_lock()
time.sleep(2)                       # give the loops time to settle
print("state:", qcm.state())        # -> "RUNNING" once locked

# 3. Start a measurement run. ambient_temp is required; the others have
#    sensible defaults (gold density, Z-ratio 1.0).
qcm.start_measurement(ambient_temp=23.0, mat_dens=19320.0, z_ratio=0.381)

# 4. Stream live measurements over the WebSocket.
for m in qcm.stream_measurements(limit=20):
    print(f"{m.calculated_thickness:8.3f} nm   {m.calculated_temp:6.2f} °C")

# 5. Stop and pull the server-side CSV log of the whole run.
qcm.stop_measurement()
qcm.download_latest_run("run.csv")
```

A ready-to-run version of this is [`qcm_demo.py`](qcm_demo.py):

```bash
python qcm_demo.py 192.168.1.50         # run from the tools/ folder
```

### Oscillator index convention

Settings that take an `oscillator_idx` use:

| index | mode                      |
|-------|---------------------------|
| `1`   | mass-sensitive mode       |
| `2`   | temperature-sensitive mode|

### System states

`qcm.state()` (and the pushed `StateEvent`) returns one of:
`IDLE`, `LOCKING`, `RUNNING`, `SWEEPING`, `CAP_ADJUST`.

---

## 5. Live streaming over the WebSocket

`stream_measurements()` yields one object per measurement, with attribute
access to the fields of `MeasurementData`:

| attribute                | meaning                                  |
|--------------------------|------------------------------------------|
| `timestamp`              | epoch seconds                            |
| `freq_mass_mode`         | mass-mode frequency (Hz)                 |
| `freq_temp_mode`         | temp-mode frequency (Hz)                 |
| `compensated_freq`       | temperature-compensated mass frequency   |
| `uncompensated_thickness`| raw Sauerbrey thickness (nm)             |
| `calculated_thickness`   | temperature-compensated thickness (nm)   |
| `calculated_temp`        | crystal temperature from the model (°C)  |
| `amp_mass`, `amp_temp`   | demodulated amplitudes                   |
| `phase_mass`, `phase_temp`| demodulated phases                      |
| `lock_mass`, `lock_temp` | per-mode lock status (bool)              |
| `quality_mass`, `quality_temp` | phase-error σ over the lock window (rad), lower is better; `None` until the window fills |

Everything above the compensated values is also published on its own as
`TelemetryEvent`, which the worker emits whenever a run is *not* in progress —
those fields need no measurement reference, so they stay live while idle. During
a run they ride along on `MeasurementEvent` instead, never both.

`stream()` is the general form — pass `types=None` to receive every event, or a
tuple to filter. Event `type` values you can receive:
`StateEvent`, `MeasurementEvent`, `TelemetryEvent`, `LockStatusEvent`, `LockFailedEvent`,
`SweepPointEvent`, `SweepCompleteEvent`, `CapAdjustEvent`, `TargetReachedEvent`,
`StartFreqAutoUpdatedEvent`, `ErrorEvent`, `OpcStatusEvent`.

```python
# Watch state changes and lock events instead of measurements:
for ev in qcm.stream(types=("StateEvent", "LockStatusEvent", "LockFailedEvent")):
    print(ev.type, vars(ev))
```

The stream is read-only (the server only pushes), and `limit=None` streams
forever — break out of the loop or set a `limit` to stop.

---

## 6. API reference

All methods are on `QCMClient`. Frequencies are in Hz unless noted.

### Measurement control
| method | endpoint | notes |
|--------|----------|-------|
| `get_lock()` | `POST /measurement/get_lock` | lock both PLLs at the active crystal's frequencies |
| `start_measurement(ambient_temp, mat_dens=19320.0, z_ratio=1.0)` | `POST /measurement/start` | `ambient_temp` required; `mat_dens` kg/m³ |
| `stop_measurement()` | `POST /measurement/stop` | |
| `state()` | `GET /state` | returns the state string |
| `set_target_thickness(value)` | `POST /settings/target_thickness` | nm; 0 = no target |
| `target()` | `GET /measurement/target` | current target + whether it has been reached |

### Target thickness

Set a target and the system raises a flag the moment the compensated thickness
reaches it: a `TargetReachedEvent` on the WebSocket, a popup in the web UI,
`GVL_QCM.READ.TargetReached` on the PLC, and `reached: true` from
`GET /measurement/target`. **The measurement is not stopped** — the flag says
the film is thick enough, and stopping is left to whoever is watching (the
operator's STOP button, or the PLC acting on the node).

The flag clears when the next run starts, or if the target is raised above the
current thickness. `0` disables the target entirely.

```python
qcm.set_target_thickness(250)          # nm
qcm.start_measurement(ambient_temp=23)
for ev in qcm.stream(types=("TargetReachedEvent",), limit=1):
    print(f"reached {ev.thickness:.1f} nm of {ev.target:.1f} nm")
qcm.stop_measurement()
```

### Oscillator / lock settings
| method | endpoint |
|--------|----------|
| `set_frequency(oscillator_idx, frequency)` | `POST /settings/frequency` |
| `set_integrator_gain(oscillator_idx, gain)` | `POST /settings/integrator_gain` |
| `set_lpf_freq(oscillator_idx, freq)` | `POST /settings/lpf_freq` (demod low-pass cutoff) |
| `set_inverted(oscillator_idx, inverted)` | `POST /settings/inverted` |
| `set_output_mode(mode)` | `POST /settings/output_mode` (DAC debug tap, int 0–11) |
| `set_lock_detect(phase_tolerance, phase_std)` | `POST /settings/lock_detect` (both rad: max mean phase error, max phase-error σ) |
| `set_auto_relock(enabled)` | `POST /settings/auto_relock` (auto re-acquire on lost lock, default on) |
| `set_lock_frequencies(mass, temp)` | `POST /settings/lock_frequencies` (transient, feeds `get_lock`) |
| `get_lock_frequencies()` | `GET /settings/lock_frequencies` |
| `set_coefficients(fM_0..fM_3, fT_0..fT_3)` | `POST /settings/coefficients` (compensation polynomial) |
| `settings()` | `GET /settings` (everything at once) |

### Crystal profiles
A crystal profile stores its lock frequencies, compensation coefficients and
sensor parameters. Activating one applies all of them.

| method | endpoint |
|--------|----------|
| `crystals()` | `GET /crystals` → `{"crystals": [...], "active": name}` |
| `crystal(name)` | `GET /crystals/{name}` |
| `create_crystal(name)` | `POST /crystals` (seeds from current settings) |
| `apply_crystal(name, **fields)` | `POST /crystals/{name}/apply` (save explicit values + apply now) |
| `activate_crystal(name)` | `POST /crystals/{name}/activate` |
| `save_current_to_crystal(name)` | `POST /crystals/{name}/save_current` |
| `delete_crystal(name)` | `DELETE /crystals/{name}` |

### Frequency sweep (characterise resonances)
| method | endpoint |
|--------|----------|
| `start_sweep(oscillator_idx, start_freq, stop_freq, step_size, settle_time)` | `POST /sweep/start` |
| `sweep_mode(oscillator_idx, settle_time=0.05)` | 500 kHz window at 1 kHz around the crystal's lock frequency |
| `abort_sweep()` | `POST /sweep/abort` |
| `sweeps()` | `GET /sweeps` — sweep CSVs on the Pitaya, newest first |
| `download_sweep(name, dest_path)` / `download_latest_sweep(dest_path)` | `GET /sweeps/{name}/download` |
| `delete_sweep(name)` | `DELETE /sweeps/{name}` |

While sweeping, the server pushes `SweepPointEvent` (`frequency`, `amplitude`,
`phase`) then a final `SweepCompleteEvent` whose `name` is the CSV the sweep was
recorded to (`None` if recording failed).

Every sweep is written to `data/sweeps/` on the Pitaya as it is taken, so it
survives a dropped WebSocket. `sweep_mode()` is the scripted form of the
**SWEEP fM** / **SWEEP fT** buttons in the UI: it centres a 500 kHz window,
stepped at 1 kHz, on the active crystal's mass or temp lock frequency. That is
a coarse locator — wide enough to find a resonance that has moved well away
from the stored frequency, but too coarse to resolve the peak's shape. Narrow
the range and re-run once you know roughly where it sits.

```python
qcm.sweep_mode(1)                                  # sweep around fM
for _ in qcm.stream(types=("SweepCompleteEvent",), limit=1):
    pass                                           # wait for it to finish
qcm.download_latest_sweep("sweep.csv")
```

Plot the result with [`sweep_plotter.py`](sweep_plotter.py):

```bash
python sweep_plotter.py sweep.csv
```

It draws amplitude and phase against frequency and marks the two strongest
well-separated peaks — the same "peak / 2nd peak" the ANALYSIS tab reports. With
no argument it opens a file dialog or picks the newest `qcm_sweep_*.csv` from
the current directory, `./data/sweeps`, or your Downloads folder.

### Capacitor adjust (null the parasitic capacitance)
| method | endpoint |
|--------|----------|
| `start_cap_adjust()` | `POST /cap_adjust/start` |
| `stop_cap_adjust()` | `POST /cap_adjust/stop` |

Emits `CapAdjustEvent` (`amp_mass`, `amp_temp`) — minimise these.

### Server-side run logs
The worker writes a CSV for every measurement run on the Pitaya itself.
| method | notes |
|--------|-------|
| `runs()` | list saved runs, newest first |
| `download_run(name, dest_path)` | save a specific run locally |
| `download_latest_run(dest_path)` | save the most recent run locally |

### OPC-UA / PLC bridge
| method | endpoint |
|--------|----------|
| `opc_settings()` | `GET /opc/settings` |
| `opc_connect(url, user="", password="", base_node=None)` | `POST /opc/connect` |
| `opc_nodes()` | `GET /opc/nodes` (the node tree to mirror on the PLC) |

### System
| method | endpoint | notes |
|--------|----------|-------|
| `reboot(force=False)` | `POST /system/reboot` | reboots the Pitaya; 409 during a run unless `force=True` |

A reboot reloads the FPGA bitstream and brings the PLLs up unlocked, so it is
the way out of a wedged instrument — but it ends any run in progress, keeping
only what the CSV had recorded up to that moment.

---

## 7. Without the Python client

The API is plain HTTP, so you can drive it from anything. Parameters are
**query-string** args, not JSON bodies:

```bash
curl -X POST "http://192.168.1.50:8000/measurement/get_lock"
curl -X POST "http://192.168.1.50:8000/measurement/start?ambient_temp=23&z_ratio=0.381"
curl       "http://192.168.1.50:8000/state"
curl -X POST "http://192.168.1.50:8000/measurement/stop"
```

---

## 8. Troubleshooting

- **`ConnectionError` / timeout** — the server isn't running or the host/port
  is wrong. Check `curl http://<host>:8000/health` and `systemctl status qcm`
  on the Pitaya.
- **`get_lock()` returns but state stays `LOCKING`, or you get a
  `LockFailedEvent`** — the start frequencies are off. Run a `start_sweep` to
  find the resonances, set them via the crystal profile, and retry.
- **State never reaches `RUNNING`** — you must `get_lock()` *before*
  `start_measurement()`.
- **`stream()` raises "Live streaming needs websocket-client"** — run
  `pip install websocket-client`.
- **`opc_connect` returns 503** — the server was started with `--no-opcua`.
