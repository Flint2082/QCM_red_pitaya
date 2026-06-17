# Red Pitaya QCM Control Software

## Setup notes
Use ssh to connect to the Red Pitaya. The default username is `root` and the default password is `root`.

```bash
ssh root@<red_pitaya_ip_address>
```


--- 

Make sure the Red Pitaya is connected to the internet. See [this][RP_internet] link for instructions on how to set up the network connection.

[RP_internet]: https://redpitaya.readthedocs.io/en/latest/networking/ethernet.html

---

Use git to clone this repository and the submodule on the Red Pitaya.

```bash
git clone https://github.com/Flint2082/QCM_red_pitaya.git
```

---
 
numpy does not provide a wheel for this platform. To avoid building it from source, we use the version provided by the system package manager.

```bash
cd QCM_red_pitaya
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

---

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
systemctl restart qcm  # restart after a code update
systemctl stop qcm     # stop (e.g. to run manually / in --dev)
systemctl disable qcm  # stop launching on boot
```

The unit assumes the repo is at `/root/QCM_red_pitaya` with the venv at
`.venv`. If your paths differ, edit `WorkingDirectory` and `ExecStart` in the
unit accordingly. It runs as `root` (required for `/dev/mem` and the FPGA
bitstream load), waits for the network, and restarts automatically if the
process exits.

---
