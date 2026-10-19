# QCM_red_pitaya

A quartz crystal microbalance (QCM) built on a Red Pitaya. The FPGA drives the
crystal and locks a PLL onto each of two resonances, one mass-sensitive and one
temperature-sensitive. The control software on the board solves those two
frequencies for temperature and mass, so the film thickness it reports is
compensated for the crystal's temperature.

The FPGA design is made with the CASPER toolflow. The theory is described in
detail in the accompanying thesis (in Dutch):
[Scriptie_V1.0_Flint_Wardenaar.pdf](docs/Scriptie_V1.0_Flint_Wardenaar.pdf).

## Repository layout

| path | contents |
|---|---|
| [`model_composer/`](model_composer) | FPGA design (Simulink, CASPER toolflow) |
| [`bitstream/`](bitstream) | compiled bitstream, loaded by the control software at startup |
| [`src/`](src) | control software that runs on the Red Pitaya: measurement loop, HTTP + WebSocket API, web UI, OPC-UA bridge to the PLC |
| [`deploy/`](deploy) | systemd unit that starts the control software on boot |
| [`tools/`](tools) | PC side: Python client, demo script, plotters for run and sweep CSVs |
| [`vendor/wheels/`](vendor/wheels) | Python packages for installing on a PC with no internet |
| [`Hardware/`](Hardware) | PCB designs (Altium) |
| [`data/`](data) | calibration data and example crystal profiles |
| [`docs/`](docs) | thesis |

## Getting started

1. **Set up the Red Pitaya:** install the control software and its systemd
   service, as described in [src/README.md](src/README.md).
2. **Open the web UI** at `http://<pitaya-ip>:8000`.
3. **Script it (optional):** the HTTP + WebSocket API behind the web UI is
   wrapped by a Python client. See [tools/README.md](tools/README.md).

### Reaching the instrument

- **An IP address always works** (`http://192.168.1.50:8000`). Use it if
  anything else is uncertain.
- `.local` names (`rp-f0f587.local`) need mDNS, which Windows 10/11 and macOS
  have out of the box. If the PC cannot reach the box by name, try the IP
  before suspecting anything else.
- Plain hostnames without `.local` need a DNS server, which an isolated bench
  network does not have.
- **Open the UI from the Pitaya, not from a local copy of `index.html`.** The
  page derives the backend address from the address it was served from. Served
  from `localhost` or a `file://` copy, it falls back to a hostname hard-coded
  in [`src/static/index.html`](src/static/index.html) (`DEV_HOSTS` /
  `backendHost`), which is a development convenience and probably not your box.

## Running without internet

The instrument and the control PC have to share a network (a cable between
them, or an isolated switch, is enough), but neither needs the internet once
the board is installed:

- **Web UI:** nothing to do. The page, its chart libraries and its fonts are
  all served by the Pitaya from [`src/static/vendor/`](src/static/vendor).
- **Python on a PC:** the packages are committed to
  [`vendor/wheels/`](vendor/wheels). Install them as described in
  [tools/README.md](tools/README.md#2-install-the-client-dependencies-on-your-pc).
- **The Pitaya:** its first install needs internet. See
  [src/README.md](src/README.md#without-internet) for reinstalling it offline,
  and for setting its clock, which nothing sets for you on an isolated network.

## Block diagram

<img width="940" height="690" alt="Block diagram of the QCM system" src="https://github.com/user-attachments/assets/5986d6df-943e-458a-be10-527f9ac0927c" />

## Algorithm

The system measures the frequency shifts of two modes of the QCM sensor: a
temperature-sensitive mode and a mass-sensitive mode. Both shifts are modelled
as a cubic in the temperature change plus a linear term in the mass change:

```math
    \begin{aligned}
        \Delta f_T &= \lambda_{T_3} \cdot \Delta T^3 + \lambda_{T_2} \cdot \Delta T^2 + \lambda_{T_1} \cdot \Delta T + \lambda_{T_0} \cdot \Delta m \\
        \Delta f_M &= \lambda_{M_3} \cdot \Delta T^3 + \lambda_{M_2} \cdot \Delta T^2 + \lambda_{M_1} \cdot \Delta T + \lambda_{M_0} \cdot \Delta m
    \end{aligned}
```

Where:
- $` \Delta f_T `$, $` \Delta f_M `$: frequency shift of the temperature-sensitive and mass-sensitive mode
- $` \Delta T `$: temperature change
- $` \Delta m `$: mass change
- $` \lambda_{T_1} \ldots \lambda_{T_3} `$, $` \lambda_{M_1} \ldots \lambda_{M_3} `$: temperature coefficients of each mode
- $` \lambda_{T_0} `$, $` \lambda_{M_0} `$: mass sensitivity of each mode

These are the `fT_0`…`fT_3` and `fM_0`…`fM_3` coefficients stored in a crystal
profile.

To isolate the temperature change $\Delta T$, eliminate the mass change
$\Delta m$ from the equations:

```math
    (\lambda_{M_3} \cdot \lambda_{T_0} - \lambda_{T_3} \cdot \lambda_{M_0}) \cdot \Delta T^3 +
    (\lambda_{M_2} \cdot \lambda_{T_0} - \lambda_{T_2} \cdot \lambda_{M_0}) \cdot \Delta T^2 + \\
    (\lambda_{M_1} \cdot \lambda_{T_0} - \lambda_{T_1} \cdot \lambda_{M_0}) \cdot \Delta T +
    \lambda_{M_0} \cdot \Delta f_T - \lambda_{T_0} \cdot \Delta f_M = 0
```

Once $\Delta T$ is known, substitute it back into the mass-mode equation to
find the mass change $\Delta m$:

```math
    \Delta m = \frac{\Delta f_M - (\lambda_{M_3} \cdot \Delta T^3 + \lambda_{M_2} \cdot \Delta T^2 + \lambda_{M_1} \cdot \Delta T)}{\lambda_{M_0}}
```
