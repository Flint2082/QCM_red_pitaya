# Running the QCM with no internet

Everything needed to control the instrument from an air-gapped PC is committed
to this repo. Nothing on a control PC reaches the internet, and nothing needs
to be downloaded at the bench.

The instrument and the PC still have to be on the **same network** — an
ethernet cable between them, or both on an isolated switch, is enough. "No
internet" is fine; "no network" is not.

---

## The web UI: nothing to do

Open `http://<pitaya-ip>:8000` in a browser and everything works. The page, the
charts and the fonts are all served by the Pitaya out of
[`src/static/vendor/`](src/static/vendor) (580 kB).

This was the one thing that broke badly offline, and it broke in a way that
looked like a dead instrument rather than a missing font: Chart.js, Hammer and
chartjs-plugin-zoom used to load from CDNs, and when the first `new Chart()`
threw, the script stopped there — before it ever opened the WebSocket. No
charts, no buttons, no lock, no readouts. The libraries are now files in the
repo at the same versions.

To refresh them (on a connected machine), re-download the three scripts named
in the comment at the top of [`src/static/index.html`](src/static/index.html)
into `src/static/vendor/`, keeping the filenames. The fonts work the same way:
`vendor/fonts.css` is the Google Fonts stylesheet with every `url()` rewritten
to point at `vendor/fonts/`.

---

## Python on a control PC

Two separate sets, so an offline PC installs only what it needs:

| you want to | install | size |
|---|---|---|
| drive the instrument from a script | [`requirements-client.txt`](requirements-client.txt) | 768 kB |
| plot run and sweep CSVs | [`requirements-plot.txt`](requirements-plot.txt) | 75 MB |

```bash
python -m venv .venv
.venv/Scripts/pip install --no-index --find-links vendor/wheels -r requirements-client.txt
```

On Linux/macOS use `.venv/bin/pip`. **Always pass `--no-index`** — without it
pip tries PyPI first and sits timing out on every single package before falling
back to the local files.

`python -m venv` itself works offline (pip comes from the bundled `ensurepip`).
Do not pass `--upgrade-deps`; that one does need the network.

### The control set installs anywhere; the plotting set does not

The control wheels are pure Python, so they fit any OS and any Python 3. The
plotting wheels (numpy, pandas, matplotlib, scipy) are compiled, and the
committed ones are **Windows x64, Python 3.13**. On any other OS or Python minor
version pip reports `No matching distribution found` even though the file is
right there — that is the wheel not matching the interpreter, not a missing
download.

To add a target, on a machine with internet:

```bash
python tools/fetch_wheels.py --platform manylinux2014_x86_64 --python-version 311
```

then commit the new wheels. Targets coexist; pip picks per machine.
`python tools/fetch_wheels.py --verify` checks a wheelhouse without needing the
offline PC. More detail in [`vendor/wheels/README.md`](vendor/wheels/README.md).

### If the plotters cannot open a file dialog

`csv_plotter.py`, `sweep_plotter.py` and `admittance_plotter.py` offer a file
dialog through tkinter, which is part of the Python installation and cannot be
pip-installed. If it is missing they fall back to searching for the newest
`qcm_*.csv`; you can always pass the path outright:

```bash
python tools/csv_plotter.py path/to/qcm_run_2026-09-03_141530.csv
```

---

## Installing on the Pitaya itself

Setting up a **new board** offline is the one job this repo cannot make
painless, because the board is `linux_armv7l` and several of its dependencies
have no wheel for that platform on PyPI — `pip download` on your laptop cannot
produce them, whatever flags it is given. Do one of these:

- **Best:** while the board still has a route to the internet, build its own
  wheelhouse on the board and keep it — `python tools/fetch_wheels.py --set
  server` on the Pitaya. That directory then reinstalls offline for ever after.
- **Or:** clone the SD card of a working box.
- **Or:** give the board internet for the length of the install, once.

Copying the repo itself needs no internet: `scp -r` it from the PC, or carry it
on a USB stick. `git clone` from GitHub is only one way to get the files there.

The service's dependency list ([`requirements.txt`](requirements.txt)) is
deliberately as short as it can be:

- `numpy` comes from the system packages (`python3 -m venv
  --system-site-packages`), as [`src/README.md`](src/README.md) already
  describes — there is no armv7 wheel for it.
- **matplotlib is no longer needed on the board at all.** It used to be
  imported at the top of `src/processing/TempCompAlgorithm.py`, which made it a
  hard dependency of the measurement service; it is only used by
  `calculateCoeffecients()`, an interactive bench helper that prompts on stdin
  and calls `plt.show()`, and which the running instrument never invokes. The
  import moved inside that function. That takes matplotlib, pillow, fonttools,
  contourpy, kiwisolver, cycler and pyparsing off the board's install — none of
  which have armv7 wheels either.
- `pandas` was never a board dependency; it belongs to the plotters and is now
  declared in `requirements-plot.txt`, where it always belonged.
- `utils` was in the list and is imported by nothing. Removed.
- `ipython` is commented out. `main.py --shell` is the only thing that wants it
  and it already handles the ImportError with a message.

---

## The clock — the one that quietly corrupts data

Without a network time source the Pitaya's clock is wrong, and **the run CSV is
the only copy of a run**, timestamped from that clock. Check it before a
session that matters:

```bash
ssh root@<pitaya-ip> date
```

If it is wrong, set it (the service runs as root, so no sudo):

```bash
ssh root@<pitaya-ip> "date -s '2026-09-03 14:15:00'"
```

Worth knowing about the damage: it is **dating that suffers, not the data**. The
plotters work in time *relative* to the first sample of a run, and the sample
interval comes from the same clock throughout, so a run recorded under a wrong
clock still plots and analyses correctly. What goes wrong is the filename, the
`timestamp_iso` column, and telling two runs apart a month later.

Two smaller consequences on an offline bench:

- Runs are named from the clock (`qcm_run_<date>_<time>.csv`). A clock stuck at
  the same value across reboots would name two runs identically; the logger
  notices and appends `-2`, so nothing is overwritten, but the names stop being
  informative.
- The automatic space reclaim deletes the **oldest** runs first, judged by file
  modification time. If the clock jumps backwards, a newer run can look older
  and be given up first. If the card is near full and the clock is unreliable,
  turn *Delete Old Runs When Full* off in Settings → System → STORAGE and
  manage the space by hand.

---

## Finding the instrument on the network

- **An IP address always works:** `http://192.168.1.50:8000`. Use it if
  anything else is uncertain.
- `.local` names (`rp-f0f587.local`) need mDNS, which Windows 10/11 and macOS
  do out of the box. It is unrelated to internet access, but it is the first
  thing to stop trusting when the PC cannot reach the box — try the IP.
- Plain hostnames without `.local` need a DNS server, which an isolated bench
  network does not have.
- **Open the UI from the Pitaya, not from a local copy of `index.html`.** The
  page derives the backend address from the address it was served from; served
  from `localhost` or a `file://` copy it falls back to a hostname hard-coded in
  [`src/static/index.html`](src/static/index.html) (`DEV_HOSTS` /
  `backendHost`), which is a development convenience and probably not your box.

---

## Quick check that a PC is ready

```bash
python -m venv .venv
.venv/Scripts/pip install --no-index --find-links vendor/wheels -r requirements-client.txt
.venv/Scripts/python -c "import requests, websocket; print('control client ready')"
```

Then, with the instrument on the network:

```bash
.venv/Scripts/python -c "import sys; sys.path.insert(0,'tools'); from qcm_client import QCMClient; print(QCMClient('192.168.1.50').health())"
```

`{'status': 'ok'}` means the PC can drive the instrument with no internet in
sight. (`QCMClient` takes the host, not a URL — the port defaults to 8000.)
