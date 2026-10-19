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
