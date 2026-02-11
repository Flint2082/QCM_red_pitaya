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

Use git to clone this repository on the Red Pitaya.

```bash
git clone https://github.com/Flint2082/QCM_red_pitaya.git
```

---
 
numpy does not provide a wheel for this platform. To avoid building it from source, we use the version provided by the system package manager.

```bash
python3 -m venv --system-site-packages .venv-rp 
```

---