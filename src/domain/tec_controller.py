"""Closed-loop temperature control of the calibration Peltier (TEC).

The FPGA makes the PWM for an H-bridge (an X-NUCLEO-IHM04A1 on E1 pins), and
the software sets it through four registers:

  PWM_en      1 = drive the bridge, 0 = everything off
  PWM_freq    27 bits: the step of a 27-bit phase accumulator clocked at
              125 MHz, so the PWM runs at PWM_freq · 125 MHz / 2^27, 0.93 Hz
              per unit (1000 Hz = 1074)
  PWM_thresh  27 bits: the duty cycle as a fraction of 2^27; the output is high
              while the accumulator is below it
  PWM_dir     which way the current runs through the TEC

pwm_words() is the one place that knows how a frequency and a duty cycle become
those words. PWM_thresh being 27 bits wide, full drive is 2^27 - 1: 2^27 itself
would wrap to 0 and turn full drive into none.

This module is the logic: the settings and their limits, a PID controller on a
temperature reading, the PT100 + crystal combination it can regulate on, and a
relay autotuner that measures the loop and proposes gains.
workers/tec_worker.py runs it: it reads the thermometers, steps the controller
and writes the registers.

The controller works in percent of full drive, positive = heating. Which
PWM_dir heats depends on how the TEC is wired and which face is on the holder;
heat_dir records it, found once by running a little drive and watching which
way the temperature goes.

Settings go to data/tec.json on every change. Whether the loop is switched on
is deliberately not among them: after a restart the TEC stays off until someone
switches it on again.
"""

import json
import math
import os
from dataclasses import asdict, dataclass, fields

from domain.temperature_sensors import SENSORS

# data/tec.json at the repo root (this file is src/domain/tec_controller.py).
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data", "tec.json")

REGISTERS = ("PWM_en", "PWM_freq", "PWM_thresh", "PWM_dir")
PHASE_BITS = 27   # width of the PWM's phase accumulator, PWM_freq and PWM_thresh

# What the loop can regulate on: an LTC2986 sensor, the crystal's own absolute
# temperature from its two locked modes (see absolute_temperature in
# processing/TempCompAlgorithm.py), or the two combined (see Fusion).
SOURCES = {**{s.key: s.name for s in SENSORS}, "crystal": "Crystal", "fused": "PT100 + crystal"}

# Tuning rules for the relay autotuner, as multiples of the ultimate gain Ku and
# the ultimate period Tu: Kp = a·Ku, Ti = b·Tu, Td = c·Tu.
RULES = {
    "tyreus_luyben":   ("Tyreus-Luyben (gentle)",  1 / 2.2, 2.2,  1 / 6.3),
    "no_overshoot":    ("Ziegler-Nichols, no overshoot", 0.2, 0.5, 1 / 3),
    "ziegler_nichols": ("Ziegler-Nichols (fast)",  0.6,     0.5,  0.125),
}

# Allowed range of each numeric setting.
LIMITS = {
    "pwm_freq":      (100.0, 100_000.0),   # Hz; the L6206 switches up to 100 kHz
    "kp":            (0.0, 1000.0),        # % per °C
    "ki":            (0.0, 100.0),         # % per °C·s
    "kd":            (0.0, 100_000.0),     # % per °C/s
    "max_output":    (0.0, 100.0),         # %
    "setpoint":      (-20.0, 100.0),       # °C
    "t_min":         (-20.0, 100.0),       # °C
    "t_max":         (-20.0, 100.0),       # °C
    "at_amplitude":  (1.0, 100.0),         # %
    "at_hysteresis": (0.0, 5.0),           # °C
    "i_band":        (0.0, 100.0),         # °C, 0 = no band
    "fused_kp":      (0.0, 1000.0),        # as kp, ki and kd
    "fused_ki":      (0.0, 100.0),
    "fused_kd":      (0.0, 100_000.0),
    "fuse_tau_s":    (30.0, 7200.0),       # s
    "fuse_lag_s":    (0.0, 300.0),         # s
}


@dataclass
class Settings:
    pwm_freq: float = 1000.0        # Hz
    kp: float = 20.0                # % per °C
    ki: float = 0.2                 # % per °C·s
    kd: float = 0.0                 # % per °C/s
    source: str = "pt100"           # a key of SOURCES
    max_output: float = 100.0       # %, cap on the drive either way
    heat_dir: int = 0               # the PWM_dir value that heats
    setpoint: float = 25.0          # °C
    t_min: float = 5.0              # °C: below this the loop switches itself off
    t_max: float = 75.0             # °C: above this likewise
    at_amplitude: float = 30.0      # %, relay step of the autotuner
    at_hysteresis: float = 0.1      # °C, relay hysteresis: above the sensor noise
    at_rule: str = "tyreus_luyben"  # a key of RULES
    i_band: float = 1.0             # °C: the integral only moves this close to the setpoint (0 = always)
    # PT100 + crystal: its own gains (kp, ki and kd above stay for the single
    # sensors, and for the PT100 alone when the crystal drops out), how long
    # the PT100 - crystal offset is averaged over, and how far the PT100 lags
    # the crystal.
    fused_kp: float = 20.0
    fused_ki: float = 0.2
    fused_kd: float = 0.0
    fuse_tau_s: float = 900.0       # s
    fuse_lag_s: float = 20.0        # s


GAINS = ("kp", "ki", "kd")


def with_gains(s: Settings, fused: bool) -> Settings:
    """s with the gains the loop runs on: the PT100 + crystal ones while the
    crystal is in use, the single-sensor ones otherwise."""
    if not fused:
        return s
    return Settings(**{**asdict(s), **{k: getattr(s, "fused_" + k) for k in GAINS}})


def validated(current: dict, changes: dict) -> dict:
    """current with changes applied; None leaves a setting as it is. Raises
    ValueError, changing nothing, when a value is out of range."""
    out = dict(current)
    for key, value in changes.items():
        if value is None:
            continue
        if key == "source":
            if value not in SOURCES:
                raise ValueError(f"source must be one of {', '.join(SOURCES)}")
        elif key == "at_rule":
            if value not in RULES:
                raise ValueError(f"at_rule must be one of {', '.join(RULES)}")
        elif key == "heat_dir":
            value = int(value)
            if value not in (0, 1):
                raise ValueError("heat_dir must be 0 or 1")
        elif key in LIMITS:
            lo, hi = LIMITS[key]
            value = float(value)
            if not (math.isfinite(value) and lo <= value <= hi):
                raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
        else:
            raise ValueError(f"unknown setting {key}")
        out[key] = value
    if out["t_min"] >= out["t_max"]:
        raise ValueError("t_min must be below t_max")
    if not out["t_min"] <= out["setpoint"] <= out["t_max"]:
        raise ValueError(f"the setpoint must lie between t_min ({out['t_min']:g} °C) "
                         f"and t_max ({out['t_max']:g} °C)")
    return out


def load_settings(path: str = SETTINGS_FILE) -> Settings:
    """The saved settings, each one that no longer validates replaced by its
    default."""
    settings = asdict(Settings())
    if not os.path.exists(path):
        return Settings()
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[TEC] Settings not read, using the defaults: {e}")
        return Settings()
    known = {f.name for f in fields(Settings)}
    for key, value in saved.items():
        if key in known:
            try:
                settings = validated(settings, {key: value})
            except (TypeError, ValueError) as e:
                print(f"[TEC] Ignoring saved setting {key}={value!r}: {e}")
    # Settings from before PT100 + crystal: start its gains from the tuned
    # single-sensor ones rather than the defaults.
    for k in GAINS:
        if "fused_" + k not in saved:
            settings["fused_" + k] = settings[k]
    return Settings(**settings)


def save_settings(settings: Settings, path: str = SETTINGS_FILE) -> str | None:
    """Write the settings atomically; returns why not, or None."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(settings), f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return None
    except OSError as e:
        print(f"[TEC] Settings not saved: {e}")
        return f"settings not saved: {e}"


def pwm_words(clock_hz: float, freq_hz: float, output: float, heat_dir: int) -> tuple[int, int, int]:
    """(PWM_freq, PWM_thresh, PWM_dir) for a signed output in % (positive heats)."""
    top = (1 << PHASE_BITS) - 1
    step = max(1, min(round(freq_hz * (1 << PHASE_BITS) / clock_hz), top))
    thresh = min(round(min(abs(output), 100.0) / 100.0 * (1 << PHASE_BITS)), top)
    direction = heat_dir if output >= 0 else 1 - heat_dir
    return step, thresh, direction


def _clamp(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


class PID:
    """Parallel-form PID in % of drive. The derivative acts on the measurement,
    not the error, so a setpoint change does not kick the output, and it is
    low-pass filtered. The integral stops growing while the output is saturated
    in the direction it would push (conditional integration), so it does not
    wind up during a long ramp. It also only moves within i_band of the
    setpoint, or where the temperature has stalled short of it: on the way to
    a new setpoint it holds the drive that held the last one, rather than
    piling up the error of the whole approach and overshooting by it."""

    D_FILTER_S = 5.0   # time constant of the low-pass on the derivative, s

    def __init__(self):
        self.reset()

    def reset(self, integral: float = 0.0):
        self.p = self.d = 0.0
        self.i = integral
        self._last_pv: float | None = None
        self._slope = 0.0   # filtered dPV/dt, °C/s

    def forget_measurement(self):
        """The next reading starts the derivative afresh (a new source, say)."""
        self._last_pv = None
        self._slope = 0.0

    def step(self, s: Settings, pv: float, dt: float) -> float:
        e = s.setpoint - pv
        if self._last_pv is not None and dt > 0:
            raw = (pv - self._last_pv) / dt
            self._slope += dt / (dt + self.D_FILTER_S) * (raw - self._slope)
        self._last_pv = pv
        umax = s.max_output
        self.p = s.kp * e
        self.d = -s.kd * self._slope
        i = _clamp(self.i + (s.ki * e * dt if self._integrates(s, e) else 0.0), -umax, umax)
        u = self.p + i + self.d
        if (u > umax and e > 0) or (u < -umax and e < 0):
            i = _clamp(self.i, -umax, umax)
            u = self.p + i + self.d
        self.i = i
        return _clamp(u, -umax, umax)

    def _integrates(self, s: Settings, e: float) -> bool:
        """Whether the integral moves this step: within i_band of the setpoint,
        or outside it while the temperature has stalled short of the setpoint,
        heading there too slowly to arrive within one integral time Kp/Ki. The
        proportional term alone settles short of the setpoint, by more than the
        band when the drive held at the last setpoint is far off, and the
        integral then has to carry it in."""
        if s.i_band <= 0 or abs(e) <= s.i_band or s.ki <= 0 or s.kp <= 0:
            return True
        toward = self._slope if e > 0 else -self._slope   # °C/s towards the setpoint
        return toward * (s.kp / s.ki) < abs(e)


class Fusion:
    """The PT100 + crystal feedback: the PT100 says where the temperature is,
    the crystal how it moves.

    The loop is fed crystal + offset, the offset being PT100 - crystal averaged
    over fuse_tau_s. Slower than that the PT100 decides: the offset makes the
    two agree on average, so the PT100 ends up at the setpoint. Faster, the loop
    follows the crystal, which reads ten times a second with next to no noise,
    leads the PT100 (it sits closer to the Peltier), and feels what the PT100
    never sees, such as the heat it loses to the air around it. Only the
    crystal's changes count, never its absolute reading, so a calibration that
    is off, or one still being made, only scales the short-term corrections by
    the error in its slope.

    The PT100 is compared with the crystal passed through a first-order lag of
    fuse_lag_s, standing in for the PT100's own lag: during a ramp the PT100
    running behind is then not taken for a change in the offset.

    A crystal reading is not trusted when it jumps by more than JUMP_C between
    two samples (a mode hop or a false lock moves it by degrees at once; a real
    temperature cannot), or when it is more than APART_C from the PT100. It is
    then dropped until it has read steadily, with neither, for REJOIN_S (or
    three PT100 delays, if longer), and meanwhile the loop runs on the PT100
    alone. The offset is worked out afresh over that wait (see _filter).
    """

    JUMP_C = 0.5      # °C between two crystal samples
    APART_C = 5.0     # °C between the crystal and the PT100
    REJOIN_S = 30.0   # s of steady crystal readings before it is used again

    def __init__(self):
        self.reset()

    def reset(self):
        self.offset: float | None = None    # PT100 - lagged crystal, averaged; None while not in use
        self.dropped: str | None = "waiting for a steady crystal reading"   # None while in use
        self.crystal: float | None = None   # the newest crystal reading taken in
        self._lagged: float | None = None   # the crystal through the PT100's lag
        self._estimate: float | None = None # the running PT100 - lagged crystal average
        self._t: float | None = None        # timestamp the filters last stepped to
        self._last: tuple[float, float] | None = None   # (timestamp, °C) of the newest crystal sample
        self._steady_since: float | None = None
        self._out: tuple[float, float] | None = None    # the newest fused (timestamp, °C)

    @property
    def in_use(self) -> bool:
        return self.dropped is None

    def update(self, s: Settings, p_ts: float, p: float, c_ts: float | None, c: float | None,
               problem: str | None = None) -> tuple[float, float]:
        """(timestamp, °C) for the loop from the newest PT100 reading (p, taken
        at p_ts) and crystal reading (c at c_ts; None when there is no current
        one, with `problem` saying why): the crystal plus the offset while the
        crystal is in use, the PT100 otherwise. The same crystal sample again
        changes nothing."""
        if c is None or c_ts is None:
            self._drop(problem or "no crystal temperature")
            return p_ts, p
        if self._last is not None and c_ts == self._last[0]:
            return self._out if self.in_use else (p_ts, p)
        prev, self._last, self.crystal = self._last, (c_ts, c), c
        if prev is not None and abs(c - prev[1]) > self.JUMP_C:
            self._drop(f"the crystal reading jumped {c - prev[1]:+.2f} °C (mode hop or false lock?)")
            return p_ts, p
        if abs(c - p) > self.APART_C:
            self._drop(f"the crystal reads {c - p:+.1f} °C from the PT100 (wrong mode?)")
            return p_ts, p
        if self._steady_since is None:
            # The first sample of a steady stretch: the filters start here.
            self._steady_since = self._t = c_ts
            self._lagged = c
            self._estimate = None
        else:
            self._filter(s, c_ts, p, c)
        if not self.in_use:
            wait = max(self.REJOIN_S, 3 * s.fuse_lag_s)
            if c_ts - self._steady_since < wait or self._estimate is None:
                self.dropped = (f"crystal steady for {c_ts - self._steady_since:.0f} of "
                                f"{wait:g} s before it is used")
                return p_ts, p
            self.dropped = None
        self.offset = self._estimate
        self._out = (c_ts, c + self.offset)
        return self._out

    def _filter(self, s: Settings, ts: float, p: float, c: float):
        """Step the lag on the crystal and the average of PT100 - lagged crystal.
        The average only starts once the lag has had 2·fuse_lag_s to catch up,
        and then as a running mean whose span grows to fuse_tau_s: a start in
        the middle of a ramp, with the lag not quite matched, washes out within
        minutes instead of lingering for the whole averaging time."""
        dt = max(ts - self._t, 0.0)
        self._t = ts
        self._lagged = c if s.fuse_lag_s <= 0 else self._lagged + dt / (dt + s.fuse_lag_s) * (c - self._lagged)
        since = ts - self._steady_since - 2 * s.fuse_lag_s
        if since < 0:
            return
        if self._estimate is None:
            self._estimate = p - self._lagged
            return
        span = min(s.fuse_tau_s, max(since, dt))
        self._estimate += dt / (dt + span) * (p - self._lagged - self._estimate)

    def _drop(self, reason: str):
        self.dropped = reason
        self.offset = None
        self._steady_since = None

    def status(self) -> dict:
        return {"in_use": self.in_use, "dropped": self.dropped, "offset": self.offset,
                "crystal": self.crystal}


class RelayAutotune:
    """Relay (Åström-Hägglund) autotuning.

    The output switches between bias + amplitude and bias - amplitude whenever
    the temperature crosses the setpoint by more than the hysteresis. That makes
    the loop oscillate at its ultimate period Tu, and the size of the swing gives
    the ultimate gain Ku = 4·d / (π·√(a² - h²)), with d the relay amplitude, a
    the half peak-to-peak of the temperature and h the hysteresis. The PID gains
    follow from Ku and Tu by the chosen rule.

    The relay swings around a bias: the drive that holds the setpoint. Started
    away from the setpoint (approach=True), the worker first brings the
    temperature there on the PID and calls begin() with the PID's integral as
    the bias once it is within `band`. A bias too far off for one side of the
    relay to cross the setpoint at all is nudged by half the amplitude whenever
    the relay has sat without switching for STALL_MIN_S, or three of the longest
    half cycles so far. And away from room temperature a symmetric relay spends
    longer heating than cooling (or the other way round), which skews the
    result, so after every cycle the bias moves to even out the two halves.

    The first cycle is the approach to the oscillation and is thrown away; the
    result is the mean of the next NEEDED cycles once their periods and
    amplitudes agree.
    """

    DISCARD = 1             # cycles thrown away at the start
    NEEDED = 3              # cycles averaged for the result
    MAX_CYCLES = 12         # give up when they have not agreed by then
    AGREE = 0.15            # spread allowed in the averaged periods and amplitudes
    MAX_ASYMMETRY = 0.25    # |t_heat - t_cool| / (t_heat + t_cool) allowed in them
    TIMEOUT_S = 3 * 3600.0  # the whole test, approach included
    NO_SWITCH_S = 60 * 60.0 # without the relay switching (or the approach arriving)
    STALL_MIN_S = 10 * 60.0 # without switching before the bias is nudged

    def __init__(self, s: Settings, bias: float, now: float, approach: bool = False):
        self.setpoint = s.setpoint
        self.max = s.max_output
        self.d = min(s.at_amplitude, self.max)
        self.h = s.at_hysteresis
        self.rule = s.at_rule
        self.bias = _clamp(bias, -(self.max - self.d), self.max - self.d)
        self.started = now
        self.approaching = approach
        self.band = max(5 * self.h, 0.3)   # °C from the setpoint that ends the approach
        self.heating: bool | None = None   # decided by the first reading
        self._last_switch = now
        self._last_nudge = now
        self._last_up: float | None = None     # heating began
        self._last_down: float | None = None   # cooling began
        self._extreme: float | None = None     # peak while cooling, trough while heating
        self._peak: float | None = None
        self._trough: float | None = None
        self.cycles: list[dict] = []
        self.result: dict | None = None
        self.error: str | None = None

    def begin(self, bias: float, now: float):
        """End of the approach: start the relay around this bias."""
        self.approaching = False
        self.bias = _clamp(bias, -(self.max - self.d), self.max - self.d)
        self._last_switch = self._last_nudge = now

    def check_time(self, now: float):
        """Give up on the overall timeout, or when nothing has happened for too long."""
        if self.result is not None or self.error is not None:
            return
        if now - self.started > self.TIMEOUT_S:
            self.error = f"no steady oscillation within {self.TIMEOUT_S / 3600:g} h"
        elif now - self._last_switch > self.NO_SWITCH_S:
            what = f"did not get within {self.band:g} °C of" if self.approaching else "has not crossed"
            self.error = (f"the temperature {what} {self.setpoint:g} °C in {self.NO_SWITCH_S / 60:g} min: "
                          f"raise the maximum drive or the relay amplitude, or pick a reachable setpoint")

    def step(self, pv: float, now: float) -> float:
        if self.heating is None:
            self.heating = pv < self.setpoint
            self._last_switch = self._last_nudge = now
        elif self.heating and pv > self.setpoint + self.h:
            self._switch(pv, now, heating=False)
        elif not self.heating and pv < self.setpoint - self.h:
            self._switch(pv, now, heating=True)
        else:
            if self._extreme is not None:
                self._extreme = max(self._extreme, pv) if not self.heating else min(self._extreme, pv)
            longest = max([max(c["t_heat"], c["t_cool"]) for c in self.cycles] or [0.0])
            if now - max(self._last_switch, self._last_nudge) > max(self.STALL_MIN_S, 3 * longest):
                # This side of the relay cannot reach the setpoint: move the bias its way.
                step = self.d / 2 if self.heating else -self.d / 2
                self.bias = _clamp(self.bias + step, -(self.max - self.d), self.max - self.d)
                self._last_nudge = now
        self.check_time(now)
        return self.output()

    def output(self) -> float:
        return _clamp(self.bias + (self.d if self.heating else -self.d), -self.max, self.max)

    def _switch(self, pv: float, now: float, heating: bool):
        # The extreme tracked since the previous switch belongs to the half now ending.
        if self._extreme is not None:
            if heating:
                self._peak = self._extreme
            else:
                self._trough = self._extreme
        self._extreme = None
        if heating:
            self._last_up = now
        else:
            # A cycle runs from one start of cooling to the next.
            if self._last_down is not None and self._last_up is not None and self._last_up > self._last_down:
                self._cycle_done(now)
            self._last_down = now
        self.heating = heating
        self._last_switch = now
        self._extreme = pv

    def _cycle_done(self, now: float):
        t_cool = self._last_up - self._last_down
        t_heat = now - self._last_up
        amplitude = (self._peak - self._trough) / 2 if self._peak is not None and self._trough is not None else None
        self.cycles.append({"period": now - self._last_down, "amplitude": amplitude,
                            "t_heat": t_heat, "t_cool": t_cool, "bias": self.bias})
        # Even out the halves: heating that takes longer than cooling wants more bias.
        self.bias = _clamp(self.bias + 0.5 * self.d * (t_heat - t_cool) / (t_heat + t_cool),
                           -(self.max - self.d), self.max - self.d)
        usable = [c for c in self.cycles[self.DISCARD:] if c["amplitude"]]
        if len(usable) >= self.NEEDED:
            last = usable[-self.NEEDED:]
            periods = [c["period"] for c in last]
            amps = [c["amplitude"] for c in last]
            spread = lambda xs: (max(xs) - min(xs)) / (sum(xs) / len(xs))
            symmetric = all(abs(c["t_heat"] - c["t_cool"]) / (c["t_heat"] + c["t_cool"]) <= self.MAX_ASYMMETRY
                            for c in last)
            if spread(periods) <= self.AGREE and spread(amps) <= self.AGREE and symmetric:
                self._finish(sum(periods) / len(periods), sum(amps) / len(amps))
                return
        if len(self.cycles) >= self.MAX_CYCLES:
            self.error = f"the oscillation did not settle in {self.MAX_CYCLES} cycles"

    def _finish(self, tu: float, a: float):
        ku = 4 * self.d / (math.pi * math.sqrt(a * a - self.h * self.h)) if a > self.h else 4 * self.d / (math.pi * a)
        _, fa, fb, fc = RULES[self.rule]
        kp = fa * ku
        self.result = {"ku": ku, "tu": tu, "amplitude": a, "rule": self.rule, "bias": self.bias,
                       "kp": kp, "ki": kp / (fb * tu), "kd": kp * fc * tu,
                       "cycles": len(self.cycles)}

    def progress(self, now: float) -> dict:
        last = self.cycles[-1] if self.cycles else {}
        return {"phase": "approach" if self.approaching else "heating" if self.heating else "cooling",
                "cycles": len(self.cycles), "needed": self.DISCARD + self.NEEDED,
                "max_cycles": self.MAX_CYCLES, "period": last.get("period"),
                "amplitude": last.get("amplitude"), "bias": self.bias,
                "relay": self.d, "elapsed": now - self.started}
