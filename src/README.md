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
is locked, and a mode whose start frequency is 0 Hz stays parked. Once the mass
mode is locked the instrument measures straight away (see below). If the lock
fails, it waits in IDLE for a GET LOCK.

The start frequencies follow the lock (frequency tracking, and the update at
the end of a run) only once a lock attempt has aimed the loops. From power-up
until then, the NCOs run from 0 Hz and their phase can pass the lock test, and
a fresh start used to overwrite the start frequencies with 0 Hz that way. A
sweep or a capacitor adjustment also leaves the loops open, so tracking stops
after either one until the next lock.

## Measuring, logging and ZERO

The sensor measures all the time: from the moment the mass mode is locked, every
sample's thickness, rate and temperature are worked out and shown, whether or
not anything is being logged. **START LOG** and **STOP LOG** only open and close
the run log. Everything that used to run only during a run now runs whenever
the instrument measures:

- **The capture windows follow the locked frequencies.** They used to stay put
  outside a run, so a calibration walked the B-mode (about −106 Hz/°C, −221 at
  64 °C) out of its ±2 kHz window and lost the lock.
- **Automatic re-lock** when the mass mode is lost. A failed automatic attempt
  raises no LOCK FAILED dialog and is retried every 30 s.
- **The drive-gain servo and frequency tracking**, as before.

What is shown depends on the locks:

- mass mode not locked: no thickness, rate or temperature, on the page, to the
  PLC or in the log, until it is back;
- both locked: the compensated thickness;
- the temp mode lost after the reference was taken: the temperature is held as
  before (**Hold Temperature On Lock Loss**), so the thickness stays compensated;
- the reference taken without the temp mode (or uncompensated mode): the
  uncompensated thickness, with no temperature, until the next ZERO.

**ZERO** sets the thickness back to 0: the mass-mode frequency now becomes the
reference, T start is read from the T Start Source, and the film parameters are
those in Settings. It is compensated when the temp mode is locked at that
moment. Logging never moves the reference, so a log can start or stop mid-
deposition; a ZERO during a log is written to it as a `ZERO` row. Before the
first ZERO the reference is taken automatically after every successful lock
(and retaken, compensated, once the temp mode locks), so the readout starts
from 0 at every GET LOCK; after a ZERO, re-locks keep the deposition baseline
and only ZERO moves it. Every new reference is announced as a `ZeroEvent` on
`/ws`. The target flag clears at a ZERO.

The PLC's StartMeasurement is the start of a deposition: it zeroes and opens
the log; StopMeasurement closes it. For the PLC and `GET /state` the state
keeps its old meaning, RUNNING while a log is open and IDLE while none is;
`measuring` says whether the sensor is producing a thickness meanwhile. The
page shows MEASURING and LOGGING. Over REST, `POST /measurement/start` opens the
log (`zero=true` zeroes first), `POST /measurement/zero` zeroes, and
`POST /measurement/stop` closes the log. `tools/qcm_client.py`'s
`start_measurement()` still zeroes by default, as a start always did.

A sweep or a capacitor adjustment can be run while the sensor measures, as long
as nothing is being logged; both open the loops, so measuring resumes at the
next GET LOCK. A new sample rate waits for a log in progress to end.

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

The same thread reads the Zynq's own die temperature from its XADC
([`domain/zynq_temperature.py`](domain/zynq_temperature.py)), board or no
board. It stands in for the Pitaya's 125 MHz reference oscillator: a drift in
that clock shifts both modes by the same fraction, which the compensation
cannot tell from mass. It takes one conversion a second and reports the mean
of the last minute; a burst of conversions dips both mode frequencies for as
long as it lasts.

Every run log also carries the newest readings beside each sample, in
`pt100_c`, `thermocouple_c`, `cold_junction_c` and `zynq_die_c` (°C), with any
LTC2986 fault named in `temp_sensor_faults`. They are blank while there is no
current reading. `temperature_c` is something else: the crystal temperature the
compensation solves from the two modes.

The reference's start temperature (T start, the crystal temperature the
compensation starts from) can come from one of these sensors instead of the
typed-in ambient temperature: **Settings → Measurement → T Start Source**, or
`t_start_source` on `POST /measurement/zero` and `/settings/measurement_params`.
The QCM worker reads the sensor the moment the reference is taken (ZERO, the
PLC's start, or the automatic reference after a lock). Without a current,
fault-free reading it starts from the ambient temperature rather than not at
all. Either way the run log records the value and its origin, in the SETTINGS
row and as `T_source=` in `RUN_START` (and in `ZERO` rows).

T start can also come from the crystal itself (`crystal`). The calibration fit
gives both modes' whole curves, constants `fM_0`/`fT_0` included, so the two
frequencies say the temperature without a thermometer and without its lag.
`absolute_temperature` in `processing/TempCompAlgorithm.py` solves it from
fT − ρ·fM with ρ = fT_0/fM_0: deposited film and reference-clock errors
cancel in that combination. A shift of one mode alone does not (1 Hz on fM is
about 0.01 °C). The worker takes it only with both modes locked and on
resonance in the newest sample, and falls back like a thermometer otherwise.
It needs a profile whose constants come from a calibration of this crystal;
TRANSFER TO CRYSTAL in the CALIBRATE tab copies them along with the slopes.
Every telemetry and measurement message on `/ws` carries the current reading as
`crystal_temp` (null unless both modes are locked), and the Settings row shows
it next to the PT100.

## Deposition rate

The MAIN tab shows the deposition rate of both thicknesses, in nm/s, beside the
thicknesses themselves. A difference between successive samples is mostly
noise, so the worker fits a straight line through the last **Rate Window**
seconds of each thickness and takes its slope
([`processing/deposition_rate.py`](processing/deposition_rate.py)). The window
is set under **Settings → Measurement → Rate Window** (default 1 s, or
`POST /settings/rate_window`). A longer window gives a quieter rate that lags a
change by about half the window. The rate stays blank until half a window has
been taken since the last reference (ZERO), and again for half the new window when the window is
lengthened mid-run.

Every run log carries it in `rate_comp_nm_s` and `rate_uncomp_nm_s`, beside the
thickness columns, and records the window in its SETTINGS row.
`tools/csv_plotter.py` works the rate out from the thickness for a log written
before these columns existed, by the same rule.

## Sampling

The FPGA updates its frequency registers far faster than the software reads
them, so how often the instrument samples is a software setting:
**Settings → Measurement → Sample Rate**, 1–100 Hz (default 100). Every sample
goes into the deposition rate, the target check and the lock automation. Two
slower streams take a subset of them, each on its own schedule
([`domain/acquisition.py`](domain/acquisition.py)):

- **Display / PLC Rate** (1–25 Hz, default 10): the samples sent to the
  browsers over `/ws` and written to the PLC. Each is the newest sample, its
  rate fitted to every sample before it.
- **Log Rate** (0–100 Hz, default 10; 0 = every sample): the samples written
  to the run log, each with the rate fitted to every sample. A row is about 300
  bytes: about 250 MB a day at 10 Hz, 2.6 GB if every sample at 100 Hz is
  logged.

The same is `POST /settings/acquisition`. A new sample rate waits for a run in
progress to end, so a run log has one rate throughout; the other two take
effect at once, and a change during a run is written to the log as an
`ACQUISITION` row. Every run's SETTINGS row records all three.

What a higher rate buys is a short Rate Window that still holds enough samples:
a 0.2 s window needs about 25 Hz. It does little for the noise at a given
window. A 30 kHz capture of the frequency registers (rp-f0ea58, idle and
locked, 10 Nov 2026) shows the noise is mostly slow wander below 5 Hz, and the
rate noise for gold came out at 1.6e-4, 8e-5, 4.3e-5 and 7e-6 nm/s for 0.2, 0.5,
1 and 5 s windows, the same at 25, 50 and 100 Hz.

On the Pitaya's own CPU (10 Nov 2026, against a fake FPGA, logging every sample,
one browser at 10 Hz) the service held 10, 50 and 100 Hz exactly, using 18 %,
34 % and 52 % of one core; at 100 Hz the samples came 10.0 ms apart, 12.7 ms at
the 99th percentile.

That extra load warms the board, and the reference clock with it: the minute of
that test warmed the Zynq die by 0.5 degC and moved both modes of a live run up
by ~35 ppb, a ~2 pm dip in its compensated thickness. A steady rate is a steady
load and does no harm, but change the sample rate a few minutes before a run
rather than just before it.

Lock detection and the drive-gain servo keep their timing at any rate: the lock
window spans 1.2 s and a loss is called after 0.2 s of out-of-limit samples
(`LOCK_WINDOW_S`, `LOCK_LOSS_S` in `domain/qcm_interface.py`), and the AGC steps
10 times a second. The footer of the page shows the rate actually achieved. A
run that falls more than 10 % behind the set rate says so in its log
(`SAMPLE_RATE_LOW`, then `SAMPLE_RATE_OK`).

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

## TEC temperature control

The calibration Peltier can be driven by the Pitaya itself, through an H-bridge
(an X-NUCLEO-IHM04A1) on E1 pins that the FPGA switches with a PWM. A PID loop
on the Pitaya ([`workers/tec_worker.py`](workers/tec_worker.py), logic in
[`domain/tec_controller.py`](domain/tec_controller.py)) holds a setpoint on a
feedback temperature, by default the PT100. The setpoint and an ON box are on
the **CALIBRATE** tab; everything else is under **Settings → TEC Control**.

The software sets four registers:

| register | meaning |
|---|---|
| `PWM_en` | 1 = drive the bridge, 0 = everything off |
| `PWM_freq` | 27 bits: step of a 27-bit phase accumulator at 125 MHz, so f = `PWM_freq` · 125 MHz / 2^27, 0.93 Hz per unit (1 kHz = 1074) |
| `PWM_thresh` | 27 bits: duty × 2^27; the output is high while the accumulator is below it |
| `PWM_dir` | which way the current runs; **Heating Direction** says which value heats |

`pwm_words()` in `domain/tec_controller.py` is the one place that turns a
frequency and a duty cycle into those words. Full drive is written as
2^27 − 1: the register is 27 bits wide, so 2^27 would wrap to 0 and full drive
would come out as none. A bitstream without the registers shows the TEC as
unavailable and refuses to switch it on.

**Safety.** The loop is off after every start of the service, whatever it was
doing before. It switches itself off, and shows FAULT with the reason, when the
feedback temperature leaves the safety limits (default 5–75 °C), when the PT100
does with another source, after a minute without a usable reading, or on an
error in the loop; ON clears it. Without a reading for 5 s the drive goes to
zero until one comes back. Stopping the service switches the bridge off.
**The bitstream has no watchdog**: if the process dies without switching off,
the FPGA keeps the last drive, and the bimetal thermostat on the holder is then
what stops a runaway. A watchdog in the FPGA that drops `PWM_en` when the
registers have not been written for a few seconds would close that gap.

**Feedback sources:** an LTC2986 sensor (PT100 by default, read once a second),
or the crystal's own absolute temperature from its two locked modes (T start
from the crystal, above), which follows the crystal without the PT100's lag but
needs a calibrated profile and both modes locked.

**PID.** Gains are in % of drive: Kp per °C, Ki per °C·s, Kd per °C/s. The
derivative acts on the temperature rather than the error (a new setpoint does
not kick the drive) and is smoothed over 5 s; the integral stops growing while
the drive is at its cap.

**Autotune** (relay method, Åström–Hägglund). The drive switches between
bias ± the relay amplitude each time the temperature crosses the setpoint by
more than the hysteresis, so the loop oscillates at its natural period Tu; the
swing gives the ultimate gain Ku, and the chosen rule (Tyreus–Luyben by
default) turns those into gains, which replace the old ones. Started away from
the setpoint it first gets there on the PID and takes the PID's integral as the
bias; the bias is also nudged when one side of the relay cannot reach the
setpoint, and evened out after every cycle so heating and cooling take equally
long. It needs four or more agreeing cycles, so 20–60 min on a real holder, and
gives up after 12 cycles, 3 h, or an hour without the relay switching. Run it
at a setpoint mid-range.

The settings are kept in `data/tec.json`, apart from DEFAULT_SETTINGS: the
gains belong to this rig, so RESTORE DEFAULTS leaves them alone. The same is
available at `GET /tec`, `POST /tec/settings`, `POST /tec/enabled`,
`POST /tec/autotune/start` and `/abort`; every step of the loop is pushed to
the browsers as a `TecStatusEvent`.

## Calibration sequence

With the TEC under its control, the Pitaya can take a whole calibration by
itself ([`domain/calibration_sequence.py`](domain/calibration_sequence.py)).
The steps are set up under **Settings → Calibration Sequence** and saved with
SAVE SEQUENCE (not APPLY), in `data/calibration/sequence.json`; START and STOP
are on the **CALIBRATE** tab's SEQUENCE row, which shows the step in progress.

Each step goes to a setpoint and is one of:

- **point**: wait for the settle detector to take a point there, as AUTO
  would (same WINDOW, drift limits and HOLD), then go on;
- **soak**: hold the setpoint for its minutes and, with a drift limit, until
  the mass mode drifts less than that (Hz/min over the settle window). No
  point: a break-in, or an anneal.

The generator fills the list from a range, a step and a pattern:
**interleaved** (every other temperature going up, the ones in between coming
down, so neighbours are approached from opposite sides and a drift shows up
instead of tilting the curve), **both ways**, or **up only**. Before the
points it can put one hot soak that is both break-in and anneal: held a little
above the highest point for a minimum time and until the mass mode drifts less
than a limit. Heat speeds up a fresh crystal's break-in, and an irreversible
shift then happens before the points rather than among them. Reference points
can follow it and close the run. The defaults (10–50 °C in 5 °C steps, the
soak at 52 °C for at least 60 min and ≤ 0.02 Hz/min, reference 25 °C) give 12
steps with 11 points. The list can be edited by hand afterwards.

While a soak runs, and in a point step until the TEMP sensor is within the
point tolerance of the setpoint, the settle detector's automatic points are
held back, so it takes nothing during a break-in or on the way to a setpoint.
Each point step re-arms the detector, so a temperature can be taken again
after a soak with nothing in between. A step that times out is skipped, taken
as it stands (marked not settled), or ends the sequence. At the end the TEC
switches off, goes to the reference temperature, or holds the last setpoint.

START needs a sensor as the TEMP source, switches AUTO on (and off again when
the sequence ends, if it was off before) and the TEC on, and refuses setpoints
outside the TEC's safety limits. The TEC switching off, by
hand or on a fault, ends the sequence, and so does a restart of the service
(the TEC comes up off); the points taken so far stay. The same is available at
`GET`/`POST /calibration/sequence` and `POST /calibration/sequence/start`,
`/stop` and `/skip`; progress goes out once a second as `status.sequence` in
the `CalibrationStatusEvent`.

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
