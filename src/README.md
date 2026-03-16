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
cd QCM_red_pitaya
git submodule update --init src/casperfpga
```

---
 
numpy does not provide a wheel for this platform. To avoid building it from source, we use the version provided by the system package manager.

```bash
cd QCM_red_pitaya
python3 -m venv --system-site-packages .venv-rp 
```

---

Install casperfpga using the folling guidelines from the [CASPER documentation](https://casper-toolflow.readthedocs.io/en/latest/src/How-to-install-casperfpga.html).

The "tornado" and "circus" packages may create conflicts. It is recommended to use v4.5.3 for tornado and v0.16.0 for circus.

```bash
cd src/casperfpga
git checkout py38
sudo pip install -r requirements.txt
pip install 'tornado==4.5.3' --force-reinstall
pip install 'circus==0.16.0' --force-reinstall
sudo python setup.py install
```
To check if casperfpga is correctly installed start a python environment and ask for the casperfpga version number:
```bash
cd ..
ipython
```

```python
import casperfpga
casperfpga.__version__
```

---

Configure startup behavior by creating a systemd service file:

```bash
sudo nano /etc/systemd/system/qcm_client.service
```

Add the following content to the file:

```ini
[Unit]
Description=QCM client
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/QCM_red_pitaya
ExecStart=/root/QCM_red_pitaya/.venv-rp/bin/python src_rp/src.py
Restart=always

[Install]
WantedBy=multi-user.target
```

---
