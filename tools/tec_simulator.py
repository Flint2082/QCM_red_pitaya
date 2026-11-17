"""Simulate the calibration TEC: a thermal model of the holder on its Peltier, driven by
the real TEC controller (src/workers/tec_worker.py) on a virtual clock.

The model
---------
A static map from drive to heat, feeding fixed linear dynamics (a Hammerstein model):

  w = F(u)·(1 - kap·dT)        F(u) = a·u - b·u²     heating (u >= 0)
                               F(u) = c·u + d·u²     cooling (u <  0)

  crystal - Ta = exp(-theta·s) / ((t1·s + 1)(t2·s + 1)) · w
  PT100   - Ta = (z·s + 1) / ((ta·s + 1)(tp·s + 1)) · (crystal - Ta)

u is the TEC drive in % (+ heats), dT the crystal's rise above the room temperature
Ta, and w (degC) the rise the drive would hold once everything has settled: F(u) is
the holding curve. With the bridge switched off the drive does nothing and the holder
relaxes with t1_off instead of t1. Cooling is weaker than heating (c < a) because the
Peltier's Joule heat helps one and fights the other.

The small-signal transfer function at an operating point is F'(u0)·exp(-theta·s) /
((t1·s + 1)(t2·s + 1)) for the crystal (Holder.linearise works it out, kap included).

PARAMS were fitted on 14 Nov 2026 to an open-loop identification test on the
rp-f0ea58 holder (PRBS on the drive around 40 and 60 degC, closed-loop steps, a
TEC-off relaxation; drive logged throughout), the cooling map to the full-cooling
ramps of the 13-14 Nov calibration runs. Accuracy and limits: see the README section
"Simulating the TEC". The same model as a Simulink block: tools/simulink/
(make_tec_holder.m builds it).

Usage
-----
  python tools/tec_simulator.py                         # the standard 15-65 degC sequence, plotted
  python tools/tec_simulator.py --setpoints 25,45,25 --dwell 30
  python tools/tec_simulator.py --source pt100 --gains 39.9,0.196,585
  python tools/tec_simulator.py --gains 40,0.2,2500 --crystal-scale 1.8
  python tools/tec_simulator.py --tf 25,40,60           # the small-signal transfer functions

Needs numpy (and matplotlib for the plot); run from anywhere: it finds src/ next to
this file.
"""

import argparse
import os
import random
import sys
import tempfile
import types

import numpy as np

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")

# Fitted parameters (see the module docstring): a, c in degC per %, b, d in degC per %²,
# kap per degC, times in s, temperatures in degC.
PARAMS = {
    "a": 0.495, "b": 0.00067,          # heating map: F(100 %) = 42.8 degC
    "c": 0.261, "d": 0.00044,          # cooling map: F(-100 %) = -21.7 degC
    "kap": -0.0014,                    # fitted slightly negative: ~5 % more heat per % at 60 degC than at room
    "t1": 312.0,                       # slow pole with the bridge enabled
    "t1_off": 411.0,                   # ... and with it switched off
    "t2": 12.2, "theta": 2.3,          # the crystal's lag and the delay (from the PRBS)
    "z": 64.5, "ta": 80.1, "tp": 6.7,  # PT100 behind the crystal (from the PRBS)
    "Ta": 22.2,
}

# The TEC settings simulated by default: Kp and Ki from the 13 Nov autotune on the
# PT100 (data/tec.json on rp-f0ea58), D on the crystal at 2000.
SETTINGS = dict(pwm_freq=100.0, max_output=100.0, i_band=1.0, t_min=5.0, t_max=75.0, heat_dir=0,
                source="pt100_crystal_d", kp=39.9, ki=0.196, kd=2000.0)

SEQUENCE = [25, 15, 25, 35, 45, 55, 65, 60, 50, 40, 30, 20, 25]


class Holder:
    """The thermal model. step(u, dt, enabled) advances it (u in %, + heats); crystal
    and pt100 are the readings in degC. The room temperature .Ta may change any time."""

    def __init__(self, params=None, T0=None):
        self.q = dict(PARAMS)
        self.q.update(params or {})
        missing = [k for k, v in self.q.items() if v is None]
        if missing:
            raise ValueError(f"parameters not set: {', '.join(missing)}")
        self.Ta = self.q["Ta"]
        T0 = self.Ta if T0 is None else T0
        self.u_hold = self.equilibrium(T0)
        # degC: slow node, crystal, PT100 lead-lag, PT100. Absolute, so that a change
        # in .Ta reaches the holder through t1, as in tools/simulink/make_tec_holder.m.
        self.x = [T0, T0, T0, T0]
        self._queue = []                    # (time due, drive): the delay line
        self._t = 0.0
        self._u = self.u_hold

    def F(self, u):
        q = self.q
        return u * (q["a"] - q["b"] * u) if u >= 0 else u * (q["c"] + q["d"] * u)

    def dF(self, u):
        q = self.q
        return (q["a"] - 2 * q["b"] * u) if u >= 0 else (q["c"] + 2 * q["d"] * u)

    def equilibrium(self, Tc):
        """The drive (%) that holds the crystal at Tc (bridge enabled)."""
        q = self.q
        dT = Tc - self.Ta
        w = dT / (1 - q["kap"] * dT)
        if w >= 0:
            A, B = q["a"], q["b"]
            return w / A if B == 0 else (A - np.sqrt(max(A * A - 4 * B * w, 0.0))) / (2 * B)
        C, D = q["c"], q["d"]
        return w / C if D == 0 else (-C + np.sqrt(max(C * C + 4 * D * w, 0.0))) / (2 * D)

    def _deriv(self, x, u, enabled):
        q = self.q
        x1, x2, w, yp = x
        Ta = self.Ta
        if enabled:
            d1 = (Ta + self.F(u) * (1 - q["kap"] * (x2 - Ta)) - x1) / q["t1"]
        else:
            d1 = (Ta - x1) / q["t1_off"]
        kz = q["z"] / q["ta"]
        return (d1, (x1 - x2) / q["t2"], (x2 - w) / q["ta"], (kz * x2 + (1 - kz) * w - yp) / q["tp"])

    def step(self, u, dt, enabled=True):
        """Advance by dt seconds (keep it <= 0.5 s) with drive u (%)."""
        self._queue.append((self._t + self.q["theta"], u))
        while self._queue and self._queue[0][0] <= self._t + 1e-9:
            self._u = self._queue.pop(0)[1]
        x = [v for v in self.x]
        k1 = self._deriv(x, self._u, enabled)
        k2 = self._deriv([v + dt * k for v, k in zip(x, k1)], self._u, enabled)
        self.x = [v + 0.5 * dt * (a + b) for v, a, b in zip(x, k1, k2)]
        self._t += dt

    @property
    def crystal(self):
        return self.x[1]

    @property
    def pt100(self):
        return self.x[3]

    def linearise(self, Tc):
        """Small-signal transfer functions at the hold of Tc: drive (%) -> crystal and
        drive (%) -> PT100, each (num, den) in descending powers of s, both times
        exp(-theta·s); and the holding drive (%)."""
        q = self.q
        u0 = self.equilibrium(Tc)
        dT = Tc - self.Ta
        g = self.dF(u0) * (1 - q["kap"] * dT)
        fb = -q["kap"] * self.F(u0)            # d(heat)/d(dT)
        den_c = np.polyadd(np.polymul([q["t1"], 1.0], [q["t2"], 1.0]), [-fb])
        num_c = np.array([g])
        num_p = np.polymul(num_c, [q["z"], 1.0])
        den_p = np.polymul(np.polymul(den_c, [q["ta"], 1.0]), [q["tp"], 1.0])
        return (num_c, den_c), (num_p, den_p), u0


# -- closed loop with the real controller ------------------------------------------------

def _controller(settings, setpoint, path):
    sys.path.insert(0, SRC)
    import workers.tec_worker as tw

    class Clock:
        t = 1.8e9

    tw.time = types.SimpleNamespace(time=lambda: Clock.t)

    class FPGA:
        sample_rate = 125e6

        def __init__(self):
            self.regs = {}

        def has_register(self, r):
            return True

        def write_register(self, r, v):
            self.regs[r] = int(v)

        def drive(self):
            """The drive the registers set, in % (+ heats)."""
            r = self.regs
            if not r.get("PWM_en"):
                return 0.0
            sign = 1 if r.get("PWM_dir", 0) == settings.get("heat_dir", 0) else -1
            return sign * 100 * min(r.get("PWM_thresh", 0) / 2 ** 27, 1.0)

    class Queue:
        def put(self, x):
            pass

    latest = [None]
    fpga = FPGA()
    w = tw.TECWorker(fpga=fpga, event_queue=Queue(), temperatures=lambda: latest[0], settings_path=path)
    w.update_settings(**settings, setpoint=setpoint)
    return w, fpga, Clock, latest


class _Reading:
    def __init__(self, ts, p):
        self.available, self.timestamp = True, ts
        self.sensors = [{"key": "pt100", "temperature": p, "faults": []}]


def run(schedule, t_end, settings=None, params=None, T0=None, pt_noise=0.013, dt=0.1, seed=1, room=None,
        crystal_error=None):
    """Closed-loop simulation. schedule: [(t_s, setpoint, or None for TEC off), ...]
    starting at t = 0; the holder starts in equilibrium at T0 (default the first
    setpoint) with the controller's integral at the holding drive. room: optional
    function t -> room temperature. crystal_error: optional function T -> error (degC)
    of the crystal thermometer the loop is fed (its calibration against the PT100);
    only its slope matters, as a factor on Kd with D on the crystal. Returns arrays
    sampled once a second: t, crystal (true), pt100 (true and as read, with the
    LTC2986's ~13 mK noise) and drive (%)."""
    settings = dict(SETTINGS, **(settings or {}))
    T0 = schedule[0][1] if T0 is None else T0
    h = Holder(params, T0=T0)
    rng = random.Random(seed)
    path = os.path.join(tempfile.mkdtemp(), "tec.json")
    w, fpga, clock, latest = _controller(settings, schedule[0][1], path)
    t0 = clock.t
    err = crystal_error or (lambda T: 0.0)
    latest[0] = _Reading(clock.t, h.pt100)
    w.feed_crystal(clock.t, h.crystal + err(h.crystal))
    w.set_enabled(True)
    w._pid.reset(integral=h.u_hold)
    rec = []
    nxt = 1
    pt_read = h.pt100
    n = int(round(t_end / dt))
    per_s = int(round(1 / dt))
    for i in range(n):
        tnow = i * dt
        while nxt < len(schedule) and tnow >= schedule[nxt][0]:
            sp = schedule[nxt][1]
            if sp is None:
                w.set_enabled(False)
            else:
                if w.mode != "pid":
                    w.set_enabled(True)
                w.update_settings(setpoint=sp)
            nxt += 1
        if room is not None:
            h.Ta = room(tnow)
        u = fpga.drive()
        h.step(u, dt, enabled=w.mode in ("pid", "autotune"))
        clock.t = t0 + (i + 1) * dt
        if (i + 1) % per_s == 0:          # the LTC2986 reads once a second
            pt_read = h.pt100 + rng.gauss(0, pt_noise)
            latest[0] = _Reading(clock.t, pt_read)
        w.feed_crystal(clock.t, h.crystal + err(h.crystal))
        with w._lock:
            w._tick(clock.t)
        if (i + 1) % per_s == 0:
            rec.append((tnow + dt, h.crystal, h.pt100, pt_read, u))
    try:
        os.remove(path)
    except OSError:
        pass
    R = np.array(rec)
    return dict(t=R[:, 0], crystal=R[:, 1], pt100=R[:, 2], pt100_read=R[:, 3], drive=R[:, 4])


def sequence_schedule(setpoints, dwell_min):
    return [(i * dwell_min * 60.0, sp) for i, sp in enumerate(setpoints)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--setpoints", default=",".join(map(str, SEQUENCE)), help="comma-separated, degC")
    ap.add_argument("--dwell", type=float, default=25.0, help="minutes per setpoint")
    ap.add_argument("--source", choices=("pt100_crystal_d", "pt100"), default="pt100_crystal_d",
                    help="D on the crystal (default) or on the PT100")
    ap.add_argument("--gains", help="kp,ki,kd")
    ap.add_argument("--crystal-scale", type=float, default=1.0,
                    help="the crystal reads changes this many times too large (a profile from another crystal)")
    ap.add_argument("--room", type=float, help="room temperature, degC")
    ap.add_argument("--tf", help="print the small-signal transfer functions at these crystal temperatures")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()
    params = {"Ta": a.room} if a.room is not None else None
    if a.tf:
        h = Holder(params)
        def factored(num, den):
            """K (z s + 1).. / ((t s + 1)..) with time constants in s (real roots assumed)."""
            k = num[-1] / den[-1]
            zs = sorted((-1 / r.real for r in np.roots(num)), reverse=True) if len(num) > 1 else []
            ps = sorted((-1 / r.real for r in np.roots(den)), reverse=True)
            top = "".join(f"({z:.1f} s + 1)" for z in zs) or "1"
            return f"{k:.3f} degC/% · {top} / " + "".join(f"({p:.1f} s + 1)" for p in ps)
        for Tc in (float(x) for x in a.tf.split(",")):
            (nc, dc), (np_, dp), u0 = h.linearise(Tc)
            print(f"{Tc:g} degC (holding drive {u0:+.1f} %), both times exp(-{h.q['theta']:.2f} s):")
            print(f"  drive % -> crystal: {factored(nc, dc)}")
            print(f"  drive % -> PT100:   {factored(np_, dp)}")
            print(f"    crystal num {np.array2string(nc, precision=5)}  den {np.array2string(dc, precision=5)}")
        return
    settings = {"source": a.source}
    if a.gains:
        kp, ki, kd = (float(x) for x in a.gains.split(","))
        settings.update(kp=kp, ki=ki, kd=kd)
    elif a.source == "pt100":
        settings.update(kd=585.0)          # the 13 Nov autotune's Kd for the PT100
    g = a.crystal_scale
    sps = [float(x) for x in a.setpoints.split(",")]
    sched = sequence_schedule(sps, a.dwell)
    out = run(sched, len(sps) * a.dwell * 60, settings, params,
              crystal_error=(lambda T: (g - 1) * (T - 25.0)) if g != 1 else None)
    for t, sp in sched:
        m = (out["t"] > t) & (out["t"] <= t + a.dwell * 60)
        fin = out["crystal"][m][-60:].mean()
        off = np.flatnonzero(np.abs(out["crystal"][m] - fin) > 0.02)
        settle = (off[-1] + 1) / 60 if len(off) else 0.0
        print(f"setpoint {sp:5.1f}: crystal ends {fin:7.3f}, PT100 {out['pt100'][m][-60:].mean():7.3f}, "
              f"within 20 mK of that after {settle:5.1f} min")
    if not a.no_plot:
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(12, 7))
        ax1.plot(out["t"] / 60, out["crystal"], label="crystal")
        ax1.plot(out["t"] / 60, out["pt100"], label="PT100")
        ax1.step([t / 60 for t, _ in sched], [sp for _, sp in sched], where="post", color="k", lw=0.8, label="setpoint")
        ax1.set_ylabel("degC")
        ax1.legend()
        ax1.grid(alpha=0.3)
        ax2.plot(out["t"] / 60, out["drive"], color="gray")
        ax2.set_ylabel("drive (%)")
        ax2.set_xlabel("minutes")
        ax2.grid(alpha=0.3)
        plt.tight_layout()
        plt.show()


if __name__ == "__main__":
    main()
