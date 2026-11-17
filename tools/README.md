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
.venv/bin/python src/main.py          # OPC-UA is off until enabled in Settings
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
`websocket-client`. Both are pinned in
[requirements-client.txt](../requirements-client.txt):

```bash
pip install -r requirements-client.txt
```

**With no internet**, install the same set from the wheels committed to the
repo — no download needed at the bench:

```bash
python -m venv .venv
.venv/Scripts/pip install --no-index --find-links vendor/wheels -r requirements-client.txt
```

On Linux/macOS use `.venv/bin/pip`. **Always pass `--no-index`**: without it
pip tries PyPI first and sits timing out on every package before falling back
to the local files. `python -m venv` itself works offline, but do not pass it
`--upgrade-deps`, which does need the network.

The plotters ([`csv_plotter.py`](csv_plotter.py), and the sweep and admittance
plotters further down this page) need numpy, pandas and matplotlib, and the
admittance plotter scipy as well. All four are pinned in
[requirements-plot.txt](../requirements-plot.txt) and vendored the same way,
but unlike the client set those wheels are compiled, and the committed ones fit
**Windows x64, Python 3.13** only. Anywhere else pip reports
`No matching distribution found`; see
[vendor/wheels/README.md](../vendor/wheels/README.md) to add another target.

The plotters offer a file dialog through tkinter, which comes with the Python
installation and cannot be pip-installed. Without it they fall back to the
newest matching CSV, or take the path outright:

```bash
python csv_plotter.py path/to/qcm_run_2026-09-03_141530.csv
```

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
qcm.activate_crystal("SC_cut_6MHz")

# 2. Lock the two PLLs onto the crystal's resonances. The service already
#    locks once on its own when it starts; this re-locks on the crystal
#    activated above.
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
| `uncompensated_rate`, `calculated_rate` | deposition rate of each thickness (nm/s) over the rate window; `None` until it fills |
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
| `start_measurement(ambient_temp, mat_dens=19320.0, z_ratio=1.0, zero=True)` | `POST /measurement/start` | opens the run log; `zero` zeroes the thickness first, as a start always did. The instrument measures whether or not a log is open |
| `zero(ambient_temp=None, mat_dens=None, z_ratio=None)` | `POST /measurement/zero` | thickness back to 0 without touching the log; parameters left out keep the stored ones |
| `stop_measurement()` | `POST /measurement/stop` | closes the run log; the measurement carries on |
| `state()` | `GET /state` | returns the state string |
| `set_target_thickness(value)` | `POST /settings/target_thickness` | nm; 0 = no target |
| `target()` | `GET /measurement/target` | current target + whether it has been reached |
| `set_rate_window(seconds)` | `POST /settings/rate_window` | s of thickness the rate is fitted over, 0.1–300; default 1 |
| `set_acquisition(sample_rate, publish_rate=10, log_rate=10)` | `POST /settings/acquisition` | Hz: how often the instrument samples (1-100), how often the GUI, the stream and the PLC get a sample (1-25), how often the run log takes one (0 = every sample); defaults 100, 10, 10 |

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

### Surviving a lost lock
Two defences run during a measurement, both on by default and both in the
settings panel under **LOCK RECOVERY**.

`set_temp_hold()` covers the temp mode. An unlocked temp PLL reports an fT that
is not a resonance, and solving the compensation from it throws the calculated
temperature — and with it the compensated thickness the deposition is
controlled on — even though the mass mode is still being measured perfectly
well. With the hold on, the last solved temperature is carried forward, so only
the thermal *correction* pauses. **The measurement does not:** the mass mode is
still read every cycle and the compensated thickness keeps coming out, following
the film sample for sample. While the temperature is frozen the correction is a
constant, so the compensated reading moves by exactly the same amount as the
uncompensated one; what it loses is only the *tracking* of a temperature that is
still drifting, which over seconds is far smaller than the error a garbage fT
injects.

A temp-only loss therefore does **not** trigger the automatic re-lock. There is
one global reset register on this FPGA, so re-acquiring means re-parking both
NCOs at pre-lock gain and blocking for up to ten attempts — seconds with no
output at all, and the mass loop dragged through a re-acquisition it did not
need. Instead the run carries straight on: the temp loop keeps running with its
capture window held where the lock was last good, which is the best place for it
to re-acquire from, and in practice it usually does so on its own with the run
never interrupted. A lost *mass* lock still re-locks immediately — the thickness
is being measured wrong there and nothing in software can retrieve it — and so
does a temp-only loss with the hold switched off, since the compensation is then
wrong either way.

Held samples carry `temp_held: true` on the WebSocket, show as **HELD** on the
Monitor tab, and are the rows with `lock_temp = 0` in the run CSV; the run log
also marks the edges with `TEMP_HOLD` / `TEMP_HOLD_END` events. Nothing gives up
on its own, so if the temp mode stays unlocked the run keeps going on a
progressively staler correction — watch for **HELD** and press GET LOCK when it
suits the deposition.

`set_freq_tracking()` improves the odds of getting the lock back. Film loading
pulls the mass mode down by kHz over a deposition and temperature moves both
modes, so by the middle of a long run the crystal profile's stored start
frequencies can sit outside the PLL capture range of where the resonance now
is — and a re-lock aimed there fails however healthy the crystal is. While both
modes are locked the start frequencies are re-pointed every `interval` seconds
at the frequencies the PLLs are actually holding (rounded to 100 Hz), so a
re-lock starts from the last place the lock was known to be good. Tracking
only starts once a lock attempt (`get_lock()`, the lock the service does when
it starts, or a re-lock) has aimed the loops, and a sweep or a capacitor
adjustment stops it until the next one. This is in-memory only: `GET /settings/lock_frequencies`, `get_lock()` and the
OPC-triggered lock all pick it up, but the crystal profile on disk is untouched
until you save it yourself.

```python
qcm.set_temp_hold(True)
qcm.set_freq_tracking(True, interval=10.0)
```

### Oscillator / lock settings
| method | endpoint |
|--------|----------|
| `set_frequency(oscillator_idx, frequency)` | `POST /settings/frequency` |
| `set_integrator_gain(oscillator_idx, gain)` | `POST /settings/integrator_gain` |
| `set_lpf_freq(oscillator_idx, freq)` | `POST /settings/lpf_freq` (demod low-pass cutoff) |
| `set_inverted(oscillator_idx, inverted)` | `POST /settings/inverted` |
| `set_agc(oscillator_idx, enabled, manual_gain=0.25, target_amp=1024.0)` | `POST /settings/agc` (drive gain: servo the amplitude while locked, or a fixed manual gain — default off at gain 0.25) |
| `set_output_mode(mode)` | `POST /settings/output_mode` (DAC debug tap, int 0–11) |
| `set_lock_detect(phase_tolerance, phase_std)` | `POST /settings/lock_detect` (both rad: max mean phase error, max phase-error σ) |
| `set_auto_relock(enabled)` | `POST /settings/auto_relock` (auto re-acquire on lost lock, default on) |
| `set_temp_hold(enabled)` | `POST /settings/temp_hold` (hold the last temperature while temp lock is lost, default on) |
| `set_freq_tracking(enabled, interval=10.0)` | `POST /settings/freq_tracking` (follow the locked frequencies, default on every 10 s) |
| `set_lock_frequencies(mass, temp)` | `POST /settings/lock_frequencies` (transient, feeds `get_lock`) |
| `get_lock_frequencies()` | `GET /settings/lock_frequencies` |
| `set_coefficients(fM_0..fM_3, fT_0..fT_3)` | `POST /settings/coefficients` (compensation polynomial) |
| `settings()` | `GET /settings` (everything at once) |
| `get_coefficients()` | `GET /settings/coefficients` (from the active crystal) |
| `set_measurement_params(ambient_temp, mat_dens, z_ratio, target_thickness=None)` | `POST /settings/measurement_params` (persist run parameters without starting a run) |
| `set_measurement_mode(mode)` | `POST /settings/measurement_mode` — `"compensated"` (default, both modes) or `"uncompensated"` (mass mode only, temp oscillator parked, no temperature) |

### Drive gain and AGC
Each oscillator's excitation sinewave is scaled in the FPGA by `output_gain_1` /
`output_gain_2` (Fixed 32.32 — all fraction, capped at half scale, so the gain
runs from 0 to **0.5**: half the 1.0 the hardware put out before the register
existed). Automatic gain control servos that scale so
the demodulated amplitude coming back off the
crystal holds at a target: the loop's signal-to-noise stays put — and with it
the phase noise the lock detection judges on — while the crystal's own response
falls off with temperature and film loading.

It ships **off**, at a manual gain of **0.25** and a target amplitude of
**1024**. An instrument coming from a build without the register was driven at
1.0, so it ran four times as hard as this default — twice as hard as the register
can now be asked for at all — and the amplitudes it reported fall with it.
Anything chosen against the old drive, the target amplitude above all, wants
re-reading at the new one. 1024 is a typical demodulated amplitude at a healthy
lock, there to put the servo in the right decade rather than to be used as it
stands.

The default is also halfway up the 0–0.5 range, which matters once the AGC is
on: it starts an unlocked channel at the manual gain and can only double the
drive from there before railing. That covers the ~2x the b-mode amplitude loses
over 20 K and not much more, so if a run also needs the drive raised for film
loading, start from a lower manual gain.

The gain is only ever adjusted **while that channel is locked**. Off resonance
the amplitude coming back says nothing about the drive a resonance would need,
so an unlocked channel — and a sweep, an acquisition attempt or a capacitor
adjustment — runs at the manual gain instead, which is also the fixed, known
drive those procedures want.

Picking a target is a measurement, not a guess. Lock the crystal, read
`amp_mass` / `amp_temp` off the telemetry (or the Monitor tab) at a good lock,
and ask for the level you want held:

```python
qcm.set_agc(1, enabled=True, manual_gain=0.25, target_amp=1024)
```

A target this crystal cannot return just parks the gain at its limit; that is
reported once as an `AGC_RAIL` event in the run log (and `AGC_RAIL_END` when it
comes back into range). The servo is deliberately slow — a few seconds to cover
a factor of ten — because drive level pulls the quartz resonance slightly, and a
gain chasing every amplitude wobble would write that wobble into the frequency
being measured. It does correct on every cycle, with no deadband, so the
amplitude settles *on* the target rather than anywhere near it; the cost is that
the drive follows about a third of the channel's amplitude noise, which is what
the slow loop gain is there to keep small.

Bitstreams older than the gain registers are detected at startup: the drive stays
whatever the FPGA hard-codes and every gain write becomes a no-op. That is the
undivided drive, so such a box runs harder than the default asks for and its
amplitudes are not comparable with one that has the registers.

### Defaults
The standard settings ship with the software, as `DEFAULT_SETTINGS` in
[`src/api/server.py`](../src/api/server.py). They are the same on every
instrument and can only be changed by a commit, so a restore always lands
somewhere known and reviewable.

| method | endpoint |
|--------|----------|
| `defaults()` | `GET /settings/defaults` → `{"settings": {...}}` |
| `restore_defaults()` | `POST /settings/restore_defaults` (put them back into force now) |

A restore covers the oscillator and lock settings, the output mode, the film
parameters and the target thickness, and takes effect immediately. The active
crystal and the OPC connection describe one particular box rather than the
software, so they are left exactly as they are — the fitted crystal keeps its
calibration.

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
| `upload_crystal(path)` | `POST /crystals/upload` (filename becomes the name; does not activate) |
| `download_crystal(name, dest_path)` | `GET /crystals/{name}/download` |

### Frequency sweep (characterise resonances)
| method | endpoint |
|--------|----------|
| `start_sweep(oscillator_idx, start_freq, stop_freq, step_size, settle_time)` | `POST /sweep/start` |
| `start_sweep_centered(oscillator_idx, center_freq, span, step_size, settle_time)` | `POST /sweep/start` with a centre and span, as the web UI sends it (points symmetric about the centre) |
| `sweep_mode(oscillator_idx, settle_time=0.05)` | 50 kHz window at 100 Hz around the crystal's lock frequency |
| `abort_sweep()` | `POST /sweep/abort` |
| `sweeps()` | `GET /sweeps` — sweep CSVs on the Pitaya, newest first |
| `download_sweep(name, dest_path)` / `download_latest_sweep(dest_path)` | `GET /sweeps/{name}/download` |
| `delete_sweep(name)` | `DELETE /sweeps/{name}` |

While sweeping, the server pushes `SweepPointEvent` (`frequency`, `amplitude`,
`phase`) then a final `SweepCompleteEvent` whose `name` is the CSV the sweep was
recorded to (`None` if recording failed).

Every sweep is written to `data/sweeps/` on the Pitaya as it is taken, so it
survives a dropped WebSocket. `sweep_mode()` is the scripted form of the
**SWEEP fM** / **SWEEP fT** buttons in the UI: it centres a 50 kHz window,
stepped at 100 Hz, on the active crystal's mass or temp lock frequency — 501
points, the same as the buttons. That is fine enough to resolve the shape of a
peak that has drifted a few kHz under film loading. If a resonance has moved
further than 25 kHz from the stored frequency it falls outside the window
entirely: use `start_sweep()` with a wider span and a coarser step to find it
first, then come back to `sweep_mode()`.

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
well-separated peaks — the same "peak / 2nd peak" the ANALYSIS tab reports. The
phase is unwrapped by default, like the ANALYSIS tab's; untick "Unwrap phase"
for the phase as measured. With
no argument it opens a file dialog or picks the newest `qcm_sweep_*.csv` from
the current directory, `./data/sweeps`, or your Downloads folder.

#### Admittance circles

The same CSV holds more than a peak position. The front end is a
capacitance-compensated bridge, so amplitude · e^(j·phase) is the crystal's
admittance times an unknown, slowly varying complex gain.
[`admittance_plotter.py`](admittance_plotter.py) fits each resonance with a
Lorentzian on a linear background (plus a term for any spur in the window). It
then rotates data and fit by the fitted phase and subtracts nothing, so the plot
shows what the background does. The result is the admittance circle in the G–B
plane, in arbitrary units, starting from the background rather than the origin.
A spur shows as its own smaller circle, nested inside the mode's:

```bash
python admittance_plotter.py sweep.csv                        # strongest resonance
python admittance_plotter.py sweep.csv --peaks 2              # the two strongest
python admittance_plotter.py sweep.csv --mode-freq 5984500    # the one near this frequency
python admittance_plotter.py sweep.csv --save circle.png      # also write a PNG
```

For each mode it prints fs, Q, FWHM, 1/R1 (arb., the circle's diameter), the
instrument phase (the rotation) and the background at fs. Every mode should
need the same instrument phase, about −1.36 rad on this front end, and with more
than one mode the tool prints the spread. A mode that disagrees has been badly
fitted. It also lists every resonance it found, so in a wide sweep you can pick
one by `--mode-freq`.

The window has three panels per mode:

- **G–B plane:** the measured points and the full fit (background plus every
  resonance). fs and fs ± FWHM/2 are marked on the fit, each spur is labelled
  with its fs, and the fitted background at fs is a black diamond.
- **Zoom to ±120 counts around the origin:** the arrow from the origin to the
  diamond is the background. The same fit without it is drawn dashed for
  comparison.
- **Raw |signal| against detuning, log scale:** the full fit, the background
  level (dotted) and the fit without the background (dashed).

The modes are only 40–55 Hz wide. `sweep_mode()`'s 100 Hz steps give about
half a point per FWHM, and a wide 500 Hz sweep a tenth. With that few points the
circle is drawn from the fit, not measured, and Q tends to come out low (by up
to ~12 % at 500 Hz steps). The tool warns whenever the step is more
than about FWHM/3, and prints the sweep to take instead: ±10 kHz at 10 Hz steps,
e.g. `qcm.start_sweep(2, 6561200, 6581200, 10, 0.05)`.

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
| `runs_info()` | the full payload: runs, the one recording now, free/total bytes |
| `download_run(name, dest_path)` | save a specific run locally |
| `download_latest_run(dest_path)` | save the most recent run locally |
| `delete_run(name)` | delete a run and every file it consists of — the only copy; 409 for the run recording *now* |
| `retry_run_log()` | re-open the log for a run going unrecorded, after freeing space |
| `set_storage_reclaim(prune_old_runs, trim_current_run)` | `POST /settings/storage_reclaim` — what may be deleted when the disk fills (both on by default) |

#### When the disk fills up
The SD card is small and a run writes about 100 MB a day, so a long run will
eventually run into the end of it. Rather than stopping the recording, the logger
deletes CSVs to keep going, and says so every time — a toast, a line in the LOGS
tab, a desktop notification, and a `STORAGE_RECLAIM` event row in the run log
itself. **Deletions are permanent: a run CSV is the only copy of that run.** Both
halves are switchable in the settings panel under **STORAGE**, and with both off
the old behaviour returns — a full disk stops the recording and raises the
"RUN NOT BEING RECORDED" alarm.

It gives things up in this order, and stops as soon as there is room:

1. **Finished runs, oldest first** (`prune_old_runs`).
2. **The oldest chunks of the run in progress** (`trim_current_run`) — for the
   case where the run is by itself bigger than the disk. A run is one CSV until
   it passes 64 MB (~18 h), after which it continues in a numbered part
   (`….p02.csv`); it is those earlier parts that get dropped. The part being
   written is never deleted, so the most recent hours always survive. This is
   why a run rolls over at all: deleting a whole part is instant, where cutting
   rows off the front of one huge file would mean rewriting everything after
   them — tens of seconds of SD I/O on the worker thread, mid-deposition.

Parts are invisible from here. `runs()` reports a run as one entry (`size` totals
its parts, `parts` counts them), `download_run()` serves them back as a single
CSV with the header once, and `delete_run()` removes all of them. Every part
repeats the run's `SETTINGS` row, so a part that outlives the first one can
still be interpreted, and `PART_END` / `PART_START` rows name the file on the
other side of each seam.

### OPC-UA / PLC bridge
| method | endpoint |
|--------|----------|
| `opc_settings()` | `GET /opc/settings` |
| `opc_connect(url, user="", password=None, base_node=None)` | `POST /opc/connect` — `password=None` keeps the stored one |
| `set_opc_enabled(enabled)` | `POST /opc/enabled` — switch the bridge on or off (off by default; kept across restarts) |
| `opc_nodes()` | `GET /opc/nodes` (the node tree to mirror on the PLC) |

### System
| method | endpoint | notes |
|--------|----------|-------|
| `reboot(force=False)` | `POST /system/reboot` | reboots the Pitaya; 409 during a run unless `force=True` |
| `health()` | `GET /health` | liveness check |
| `version()` | `GET /version` | the software and bitstream the box is running |

A reboot reloads the FPGA bitstream and brings the PLLs up unlocked, so it is
the way out of a wedged instrument — but it ends any run in progress, keeping
only what the CSV had recorded up to that moment.

`version()` returns the software version as `git describe` names the commit the
server started from (`v1.2.0`, `v1.2.0-3-g1a2b3c4`, `-dirty` for uncommitted
changes), whether the checkout has moved on since (`restart_needed`), and the
bitstream it loaded:

```python
>>> qcm.version()
{'software': {'version': 'v1.0.0-1-g1a2b3c4', 'commit': '1a2b3c4…', 'commit_date': '2026-10-20T14:03:11+02:00',
              'dirty': False, 'restart_needed': False},
 'fpga': {'fpg': 'qcm_rp_2026-09-04_1026.fpg', 'build_date': '2026-09-04T10:19:47',
          'bitstream_sha256': '…', 'fabric_build_time': None}}
```

Every run records the same thing in its `SETTINGS` row. How the versions are
numbered is described in [src/README.md](../src/README.md#versions).

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
- **`opc_connect` / `set_opc_enabled` return 503** — the server was started
  with `--no-opcua`.
- **The PLC gets nothing** — the OPC-UA bridge is off by default: switch it
  on under Settings → OPC-UA, or `qcm.set_opc_enabled(True)`.

---

## 9. Simulating the TEC

[`tec_simulator.py`](tec_simulator.py) simulates the calibration holder on its
Peltier, driven by the real TEC controller code (`src/workers/tec_worker.py`:
the PID, the derivative on the crystal with its checks, the limits) on a
virtual clock. Use it to try gains or a calibration sequence before running
them on the holder. Four hours of holder time take a few seconds.

```bash
python tools/tec_simulator.py                                   # the standard 15-65 °C sequence, plotted
python tools/tec_simulator.py --setpoints 25,45,25 --dwell 30
python tools/tec_simulator.py --source pt100 --gains 39.9,0.196,585
python tools/tec_simulator.py --gains 40,0.2,2500 --crystal-scale 1.8 --no-plot
python tools/tec_simulator.py --tf 25,40,60                     # small-signal transfer functions
```

By default it runs "PT100 + crystal for D" with Kp 39.9, Ki 0.196 and Kd 2000.
`--crystal-scale` makes the crystal read its changes that many times too large,
like a profile from another crystal; with D on the crystal that multiplies Kd.

From Python, `tec_simulator.run(schedule, t_end, settings, params, ...)` returns
the crystal, PT100 and drive once a second. `crystal_error=` adds a calibration
error to the crystal temperature the loop is fed (only its slope matters), and
`room=` makes the room temperature a function of time. `Holder` is the thermal model on its own:
`step(u, dt)` advances it, and `linearise(T)` gives the transfer functions.

### Transfer functions

Small signal, TEC drive (%) to temperature (°C). They were measured on the
rp-f0ea58 holder on 14 Nov 2026 with a PRBS on the drive (±15 % around 40 °C,
±12 % around 60 °C), the drive logged throughout:

| Operating point | Drive → crystal | Crystal → PT100 |
|---|---|---|
| 40 °C (37 % drive) | 0.47 · e^(−2.3 s) / ((320 s + 1)(12.2 s + 1)) | (64.5 s + 1) / ((80.1 s + 1)(6.7 s + 1)) |
| 60 °C (82 % drive) | 0.40–0.43 · e^(−2.3 s) / ((330 s + 1)(12.2 s + 1)) | the same |

- **Crystal fit:** at 40 °C it is within 3 % of the measured response from
  0.5 to 33 mHz.
- **Gain vs operating point:** the gain falls with the drive, so it is lower
  at 60 °C.
- **Time constants:** these barely change between the two operating points.
- **TEC off:** with the bridge switched off the holder relaxes with τ ≈ 410 s.
- **Check:** with this model the controller's own relay autotune reproduces
  both autotunes run on the real holder on 13 Nov:
  - PT100 loop: Ku 84–87 %/°C, Tu 90–91 s (real: 87.8, 92 s);
  - the PT100 + crystal (fused) loop of the time: Ku 132–136, Tu 57–58 s
    (real: 131, 57.5 s).

### The model (large signal)

A static map from drive to the rise it holds, feeding the linear part above (a
Hammerstein model):

```
w = F(u)·(1 − kap·ΔT)     F(u) = 0.495·u − 0.00067·u²   heating   (F(100 %) = 42.8 °C)
                          F(u) = 0.261·u + 0.00044·u²   cooling   (F(−100 %) = −21.7 °C)
crystal − Ta = e^(−2.3 s) / ((312 s + 1)(12.2 s + 1)) · w      kap = −0.0014 /°C, Ta ≈ 22 °C
```

- **The map F(u) is the holding curve:** 0 % at room temperature, 37 % at
  40 °C, 82 % at 60 °C. Full heating tops out near 67 °C.
- **Cooling is weaker:** at room temperature full cooling does half of what
  full heating does. The Peltier's Joule heat helps heating and fights
  cooling.
- **How it was fitted:**
  - fast part (delay, crystal lag, PT100 path): the PRBS frequency response;
  - map and slow poles: the time series of the test;
  - cooling side: the full-cooling ramps of the 13-14 Nov runs.
- **Room temperature:** the room enters through the slow pole (t1, or t1_off
  with the bridge off), so a change in room temperature reaches the holder
  over minutes rather than at once.

### Simulink

[`simulink/tec_holder_lib.slx`](simulink/tec_holder_lib.slx) is a library with
the same model as one masked block, **TEC holder**:

| | Port | Unit |
|---|---|---|
| Input 1 | drive | %, +100 = full heating; limited to ±100 |
| Input 2 | enabled | 1 = bridge on, 0 = off (the drive is then ignored) |
| Input 3 | room | °C |
| Output 1 | crystal | °C |
| Output 2 | PT100 | °C |

- **Mask parameters:** the mask holds the parameters above, defaulting to the
  fitted values. It also sets the starting point: the crystal at T0 in a room
  at Ta0. The block starts in equilibrium there, with the delay line holding
  the drive that keeps it there.
- **Demo:** [`simulink/tec_holder_demo.slx`](simulink/tec_holder_demo.slx)
  puts the block in a PID loop on the PT100. That is the TEC worker on its
  PT100 source:
  - the PT100 gains;
  - the derivative on the measurement through a 5 s filter;
  - clamping anti-windup and a ±100 % output limit;
  - a step every 0.5 s on a PT100 read once a second.

  It changes setpoint every 30 min. To use other setpoints, define
  `tec_setpoints = [t_s, degC; ...]` in the base workspace before running it.
- **What the demo leaves out:** the worker's integral band (`i_band`) and the
  derivative on the crystal. The TEC controller block below has both.
- **Rebuilding:** [`simulink/make_tec_holder.m`](simulink/make_tec_holder.m)
  builds both files. Run it again to rebuild them, for example after a new
  fit or in an older MATLAB: the files are saved by R2025b, and the script
  uses only basic blocks.
- **Checks:**
  - The block agrees with `tec_simulator.Holder` to within 1 mK on the logged
    drive of the 14 Nov test.
  - Its linearisation at 40 °C gives the transfer function in the table above.
    `linmod` leaves out the 2.3 s transport delay, which is its default.

**The controller.** [`simulink/tec_controller_lib.slx`](simulink/tec_controller_lib.slx)
holds a second block, **TEC controller**: the Pitaya's TEC loop, ported tick
by tick from `src/domain/tec_controller.py` and `src/workers/tec_worker.py`.

| | Port | Unit |
|---|---|---|
| Input 1 | setpoint | °C |
| Input 2 | PT100 | °C; read inside once a second, like the LTC2986 |
| Input 3 | crystal reading | °C; NaN while unlocked |
| Output 1 | drive | %, to the holder's input 1 |
| Output 2 | enabled | 0 after a limit trip, to the holder's input 2 |
| Output 3 | status | [derivative on the crystal; crystal/PT100 move ratio; PT100 °C] |

- **Why a block of its own:** a stock PID Controller block is not the
  worker's PID.
  - It has no integral band. With a strong derivative the output stays below
    its limit during the approach, clamping never acts, and the integral
    collects the whole approach: 2 °C of overshoot on a 10 °C step.
  - Two PID blocks, each limited to ±100 % and then added, are not one PID
    limited once. The first block's limit cuts the P term before the second
    block's D is taken off, so the heater gets about 30 % during a step.
- **What it runs:** a MATLAB Function block steps every 0.1 s, like the
  worker's tick, with the script
  [`simulink/tec_controller_block.m`](simulink/tec_controller_block.m). It
  contains:
  - the PID: P and I on the PT100, the derivative on the measurement through
    a 5 s filter, conditional integration, the integral band, a step on each
    fresh reading at least 0.5 s apart;
  - the derivative on the crystal with its checks (a jump; a crystal moving
    backwards or 0.25–2× off; a first move to measure it), the rejoin wait,
    a quarter of Kd on the PT100 meanwhile, and the derivative restarting when
    the crystal joins or leaves;
  - the trips on t_min/t_max.

  It leaves out the relay autotune and the handling of stale readings.
- **Sources,** as on the Pitaya: PT100, and PT100 + crystal for D. One set
  of gains, `kp`, `ki`, `kd`; Kd about 600 on the PT100, 2000 on the
  crystal.
- **Demo:** [`simulink/tec_controller_demo.slx`](simulink/tec_controller_demo.slx)
  wires it to the TEC holder block.
  - It runs the standard 15–65 °C sequence by default, with 13 mK of PT100
    noise.
  - Set these in the base workspace (see its InitFcn): `tec_setpoints`,
    `pt100_noise`, and `crystal_scale` and `crystal_offset`. The last two make
    the crystal reading `crystal_scale·(T − 25) + 25 + crystal_offset`, a
    crystal on another crystal's profile. The scale multiplies Kd; the offset
    does nothing.
  - The integral starts at the drive that holds the first setpoint.
- **Rebuilding:** [`simulink/make_tec_controller.m`](simulink/make_tec_controller.m)
  builds the library and the demo. It leaves the holder's files alone.
- **Check against `tec_simulator.py`** (which runs the Python code itself),
  noise off, setpoints 25 → 35 → 45 → 35 °C:
  - PT100 source, Kd 585: 1.0 mK rms;
  - D on the crystal, Kd 2000, crystal_scale 1.7 and a +7 °C offset: 4.9 mK
    rms, all of it a 56 mK blip for some 20 s where the crystal first joins,
    halfway up the first ramp (629 s in Simulink, 630 s in Python);
  - a backwards crystal (crystal_scale −1): 0.1 mK rms; it never joins in
    either;
  - settling times equal to 0.1 min.

  That is after taking out a step-timing offset of 1 s (0.5 s on the crystal).
  The offset is only the harnesses' setpoint timing: Simulink changes the
  setpoint on a reading's tick, while the Python harness changes it between
  readings. The same second, landing on the join mid-ramp, makes the blip.

### Accuracy and limits

- **On the identification test:** the model is within 0.13 °C rms of the
  crystal and 0.14 °C of the PT100 over 2¾ hours. That stretch runs from the
  settled 40 °C hold, through a 20 °C step and both PRBS runs, to a TEC-off
  relaxation.
- **On the logged runs:** the 13 and 13-14 Nov calibration sequences, replayed
  with the controller settings of the time, agree to 0.16-0.19 °C rms.
  - The plateaus agree to about 0.1 °C.
  - During full-power ramps the error reaches 0.5-0.75 °C for a minute or two.
- **Below 35 °C:** heating there is slower than the model, by up to about
  15 %. The first ramp up from room temperature on 14 Nov was 15 % slower than
  modelled too. Neither is understood yet.
- **Not modelled:**
  - **Room temperature:** the model holds it constant.
  - **Box warming:** the box warms by a few tenths of a degree once the TEC
    runs.
  - **Crystal thermometer in fast ramps:** it is off by up to ~0.2 °C (thermal
    transients in the B-mode).
- **The 0.3-0.5 °C creeps after each step on 13-14 Nov** came from the crystal
  calibration, not from the holder. The loop then ran PT100 + crystal fused
  (P and I on the crystal plus an averaged offset; replaced on 17 Nov by D on
  the crystal, which cannot creep). The offset learned at one plateau was
  wrong at the next by the change in the crystal's calibration error: most
  likely half_cal's, −0.4 °C to +1.1 °C against the PT100 across 15-65 °C. The
  replays of those runs used that controller.
