# Red Pitaya QCM Control Software

## Setup

Connect to the Red Pitaya over SSH. The default username and password are both
`root`.

```bash
ssh root@<red_pitaya_ip_address>
```

Make sure the Red Pitaya is connected to the internet (see the
[Red Pitaya networking docs][RP_internet]). The board needs internet for this
install only, to fetch its Python dependencies. After that, neither the board
nor the PCs controlling it need internet again; see
[Without internet](#without-internet).

[RP_internet]: https://redpitaya.readthedocs.io/en/latest/networking/ethernet.html

Clone this repository on the Red Pitaya:

```bash
git clone https://github.com/Flint2082/QCM_red_pitaya.git
```

Create a venv and install the dependencies. numpy has no wheel for this
platform, so rather than building it from source the venv uses the one from the
system package manager:

```bash
cd QCM_red_pitaya
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

## Run on boot (systemd)

To have the control software start automatically on every boot, install the
provided systemd unit ([`deploy/qcm.service`](../deploy/qcm.service)):

```bash
cp /root/QCM_red_pitaya/deploy/qcm.service /etc/systemd/system/qcm.service
systemctl daemon-reload
systemctl enable --now qcm.service     # start now AND on every boot
```

Useful commands:

```bash
systemctl status qcm        # is it running?
journalctl -u qcm -f        # live logs
systemctl restart qcm       # restart after a code update
systemctl stop qcm          # stop (e.g. to run manually / in --dev)
systemctl disable qcm       # stop launching on boot
```

The unit assumes the repo is at `/root/QCM_red_pitaya` with the venv at
`.venv`. If your paths differ, edit `WorkingDirectory` and `ExecStart` in the
unit accordingly. It runs as `root` (required for `/dev/mem` and the FPGA
bitstream load), waits for the network, and restarts automatically if the
process exits.

### What a start does

On every start the service writes the saved settings to the FPGA, applies the
active crystal profile, and then locks the PLLs on that profile's start
frequencies, exactly as GET LOCK does. In uncompensated mode only the mass mode
is locked, and a mode whose start frequency is 0 Hz stays parked. If the lock
fails, the instrument waits in IDLE for a GET LOCK, as it did before this
start-up lock existed.

The start frequencies follow the lock (frequency tracking, and the update at
the end of a run) only once a lock attempt has aimed the loops. From power-up
until then, the NCOs run from 0 Hz and their phase can pass the lock test, and
a fresh start used to overwrite the start frequencies with 0 Hz that way. A
sweep or a capacitor adjustment also leaves the loops open, so tracking stops
after either one until the next lock.

## Front-panel LEDs

The bitstream wires the 4-bit `led_control` register straight to the LEDs, and
the control software drives it ([`domain/status_leds.py`](domain/status_leds.py)):

| LED | meaning |
|---|---|
| 1 | on while the software runs on a loaded bitstream; **flashes** while a run has a problem: a lost lock (the temp mode only counts in a compensated run), a re-lock in progress, recording failed, or no thickness coming out |
| 2 | mass-mode PLL locked |
| 3 | temp-mode PLL locked (dark while parked) |
| 4 | yellow, not assigned yet |

The lock LEDs stay dark through a sweep and a capacitor adjustment, which open
the loops. All four go dark when the service stops.

## Temperature sensors (LTC2986)

The V2 board carries an LTC2986 on the SPI pins of the E2 connector
(`/dev/spidev2.0`), wired for a 4-wire PT100 and a thermocouple whose cold
junction is a TSic 501F on the board. A background thread
([`workers/temperature_worker.py`](workers/temperature_worker.py)) reads all
three once a second; the readings, with any fault the chip flags, show in the
**TEMP SENSORS** tab and at `GET /temperatures`. A box without the board just
shows them as unavailable, with the reason.

The channel map, and the values the schematic does not give (the sense resistor
R22, the thermocouple type), are at the top of
[`domain/temperature_sensors.py`](domain/temperature_sensors.py). The PT100
reads in proportion to the sense resistor, so set it to the fitted value.

Every run log also carries the newest readings beside each sample, in
`pt100_c`, `thermocouple_c` and `cold_junction_c` (°C), with any fault named in
`temp_sensor_faults`. They are blank while there is no current reading.
`temperature_c` is something else: the crystal temperature the compensation
solves from the two modes.

## Calibration points

The **CALIBRATE** tab's points are taken and kept on the Pitaya
([`domain/calibration_recorder.py`](domain/calibration_recorder.py)), in
`data/calibration/points.json`, so they survive a page reload and a restart.
A settle detector watches the readings and shows on the tab's SETTLE row whether
the crystal is ready for a point. Over the last WINDOW, both modes must stay
locked, fT must drift less than the limit in Hz/min (over the whole window and
over its last third), and, with a sensor as the
TEMP source, that sensor must report no fault and drift less than its limit in
°C/min. All of that has to hold for HOLD.

Each point is the average of fM, fT and the temperature over the window. With a
sensor as the source and AUTO on, the point is taken by itself once the crystal
has settled, and the page signals that it is time to set the next temperature.
The next one waits until fT has left that temperature (100 Hz, about 0.7 °C,
for 10 s in a row) and then for a whole window recorded after that, so it is
never averaged partly on the old plateau. With the
Manual source, the page signals when the crystal has settled, and you enter the
temperature and press ADD POINT. ADD POINT before the crystal has settled asks
first. The same is available at `GET /calibration`,
`POST /calibration/settings`, `POST /calibration/points` and
`DELETE /calibration/points[/{id}]`.

## OPC-UA

The bridge to the WAGO PLC is **off by default**: switch it on under Settings →
OPC-UA (or `POST /opc/enabled?enabled=true`). The setting is remembered across
restarts, and RESTORE DEFAULTS leaves it alone. `--no-opcua` on the command line
leaves the bridge out entirely.

## Versions

The web UI shows what the box is running in the header badge, with details
under Settings → System → VERSION. The same is served by `GET /version`
(`qcm.version()` from Python) and recorded in the `SETTINGS` row of every run,
so a run says which software and bitstream took it.

### Software

The software version is the git commit the server started from, as
`git describe` names it:

| the Pitaya is running | shown as |
|---|---|
| a commit, before any release is tagged | `c29cb07` |
| a tagged release | `v1.0.0` |
| 3 commits past that tag | `v1.0.0-3-g1a2b3c4` |
| any of these with uncommitted changes | `…-dirty` |

Day to day nothing changes: commit on the PC, push, and pull on the Pitaya. The
UI then shows the same hash `git log` shows on the PC. When a commit has proved
itself on the Pitaya, tag it as a release on the PC and push the tag:

```bash
git tag -a v1.1.0 -m "short description of the release"
git push --follow-tags
```

The next `git pull` on the Pitaya brings the tag along, and the running commit
is named by it straight away, without a restart. The numbers follow the commit
messages: a `fix:` bumps the last digit, a `feat:` the middle one, and the first
goes up for a change that breaks existing scripts or files (the REST API the
Python client uses, the run CSV columns, the crystal profile format).

`-dirty` only looks at the files that make up the firmware (`src/`,
`bitstream/`, the `.fpg` and `deploy/`). The server writes into `data/` during
normal operation, and two of the crystal profiles there are tracked, so a check
over the whole tree would call every box dirty after its first run.

### Updating the Pitaya

The code and the bitstream are both loaded when the service starts, so a pull
changes nothing until it restarts:

```bash
cd /root/QCM_red_pitaya && git pull && systemctl restart qcm
```

The UI shows **RESTART NEEDED** when the checkout on disk has moved on from what
is running.

### Bitstream

The bitstream is identified by the `.fpg` the register map was read from (its
name and build date) and a SHA-256 of `bitstream/top.bit.bin`, both taken when
the service loads them. It has no number of its own: it is committed in the same
repository, so a software tag pins a bitstream too.

A bitstream can also report its build time from the FPGA itself, which is the
one source that cannot disagree with what is actually loaded. Add a software
register named `build_time` (To Processor, 32 bits unsigned) to the Simulink
model, fed by a constant holding the build time in Unix seconds (UTC); setting
that constant in the model's InitFcn, e.g.
`qcm_build_time = floor(posixtime(datetime('now','TimeZone','UTC')));`, stamps
every compile. The server reads the register when the bitstream has one; older
bitstreams show "not in this bitstream".

## Without internet

Once installed, the board runs on an isolated network with nothing further to
do, apart from two things below.

### Reinstalling without internet

Setting up a new or wiped board offline is the one job this repo cannot make
painless. The board is `linux_armv7l`, and several of its dependencies have no
wheel for that platform on PyPI, so `pip download` on a PC cannot produce them,
whatever flags it is given. Do one of these:

- **Best:** while the board still has a route to the internet, build its own
  wheelhouse on the board and keep it: run
  `python3 tools/fetch_wheels.py --set server` on the Pitaya. That directory
  then reinstalls the board offline from then on.
- **Or:** clone the SD card of a working box.
- **Or:** give the board internet for the length of the install, once.

Copying the repo itself needs no internet: `scp -r` it from the PC, or carry it
on a USB stick.

### Set the clock

Without a network time source the Pitaya's clock is wrong, and **the run CSV,
which is the only copy of a run, is timestamped from that clock.** Check it
before a session that matters:

```bash
ssh root@<pitaya-ip> date
```

If it is wrong, set it (the service runs as root, so no sudo):

```bash
ssh root@<pitaya-ip> "date -s '2026-09-03 14:15:00'"
```

A wrong clock damages the dating, not the data. The plotters work in time
relative to the first sample of a run, and the sample interval comes from the
same clock throughout, so the run still plots and analyses correctly. What goes
wrong is the filename, the `timestamp_iso` column, and telling two runs apart a
month later. Two smaller consequences:

- Runs are named from the clock (`qcm_run_<date>_<time>.csv`). A clock stuck at
  the same value across reboots would name two runs identically; the logger
  notices and appends `-2`, so nothing is overwritten, but the names stop being
  informative.
- When the SD card fills, the logger deletes the **oldest** runs first, judged
  by file modification time. If the clock jumps backwards, a newer run can look
  older and be deleted first. If the card is near full and the clock is
  unreliable, turn *Delete Old Runs When Full* off in Settings → System →
  STORAGE and manage the space by hand.
