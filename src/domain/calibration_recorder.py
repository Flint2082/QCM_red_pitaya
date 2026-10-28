"""Calibration points for the CALIBRATE tab, decided and kept on the Pitaya.

A calibration steps the crystal through a series of temperatures (by hand,
with the Peltier), waits at each one for it to settle, and records the two mode
frequencies against a reference temperature. This module watches the readings
for that moment and keeps the points, so a calibration that takes hours depends
neither on a browser staying open nor on somebody clicking at the right time.

Settled means that over the last `window_s`:
  * both modes stayed locked;
  * fT moved less than `max_ft_drift` Hz/min — the crystal's own reading, and
    the criterion of the calibration method: 2 Hz/min is about 0.012 °C/min —
    over the whole window and over its last third alone;
  * with a sensor as the temperature source, that sensor reported no fault and
    moved less than `max_temp_drift` °C/min;
and that all of this has held for `hold_s` without a break. Drifts are
least-squares slopes. A point is the average over that same window, so fM, fT
and the reference temperature come from one and the same span. The last-third
fT drift is there because a three-minute fit hardly notices a change that has
only just begun: it would call the start of a ramp settled for a while.

With a sensor as the source and auto_capture on, the point is taken as soon as
the crystal has settled, and the operator only has to change the Peltier when
told. After a point, the crystal counts as having left it once fT has stayed
more than REARM_HZ away for DEPART_S seconds in a row. From then on the window
only reaches back to that moment, so the next point is judged and averaged on
the new temperature alone; it also has to average out REARM_HZ away from the
last one. (On 2026-10-28 a point was taken 1:44 after the previous one: fT had
jumped ~100 Hz in the last few seconds when the Peltier was turned down, which
the window fit did not register while the newest second already counted as a
new temperature, and the average was 5 Hz off the old plateau.)

A point can always be taken by hand too; with the manual source that is the
only way, since only the operator knows the temperature.

Frequency samples (~10 Hz) are averaged into one-second bins as they arrive:
that is all the resolution a settling with a ten-minute time constant needs, and
it keeps the once-a-second evaluation cheap on the Pitaya.

Points and settings go to data/calibration/points.json on every change, so they
survive a reload of the page, a restart of the service and a power cut.

Fed from the server's event loop and changed from its REST handlers, so every
public method takes the one lock.
"""

import json
import math
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, fields

from domain.temperature_sensors import SENSORS

# data/calibration/ at the repo root (this file is src/domain/calibration_recorder.py).
POINTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                           "data", "calibration", "points.json")

# Where a point's temperature comes from: typed in, or one of the LTC2986 sensors.
SOURCES = {"manual": "Manual", **{s.key: s.name for s in SENSORS}}

# How far fT has to move from the last point before the next one is taken
# automatically: 0.6-0.7 °C on the crystals in use, whose temp modes move
# 145-170 Hz/°C. Keeps a plateau from being captured twice, while still letting
# a temperature be visited again from the other direction.
REARM_HZ = 100.0
# ...and for how long in a row, so a brief excursion does not count as leaving.
DEPART_S = 10
# Locked seconds a point needs at the least; a shorter average is mostly noise.
MIN_POINT_SECONDS = 10
# Readings count as stopped after this long without one (a sweep or a capacitor
# adjustment is running, or the sensors have gone).
STALE_S = 5.0
# A window counts as full once this fraction of its seconds hold readings, so
# the odd second without a sample does not hold a point up.
COVERAGE = 0.9

# Allowed range of each numeric setting.
LIMITS = {
    "window_s":       (30.0, 1800.0),
    "max_ft_drift":   (0.01, 1000.0),
    "max_temp_drift": (0.0001, 10.0),
    "hold_s":         (0.0, 3600.0),
}
HISTORY_S = LIMITS["window_s"][1]  # readings kept: enough for the longest window


@dataclass
class Settings:
    source: str = "manual"          # a key of SOURCES
    window_s: float = 180.0         # drift fit and point average, s
    max_ft_drift: float = 2.0       # Hz/min
    max_temp_drift: float = 0.012   # °C/min
    hold_s: float = 60.0            # how long it has to stay steady, s
    auto_capture: bool = True       # take the point once settled (sensor sources only)


class _Bin:
    """The frequency samples of one second: sums of the locked ones, and
    whether any sample in it was unlocked."""
    __slots__ = ("t", "n", "fM", "fT", "locked")

    def __init__(self, t: int):
        self.t, self.n, self.fM, self.fT, self.locked = t, 0, 0.0, 0.0, True


def _slope_per_min(ts, ys) -> float | None:
    """Least-squares slope of ys against ts (s), per minute."""
    n = len(ts)
    if n < 3:
        return None
    mt, my = sum(ts) / n, sum(ys) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx == 0:
        return None
    return sum((t - mt) * (y - my) for t, y in zip(ts, ys)) / sxx * 60.0


def _mean_std(values) -> tuple[float, float]:
    n = len(values)
    mean = sum(values) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1)) if n > 1 else 0.0
    return mean, std


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class CalibrationRecorder:
    def __init__(self, path: str | None = None):
        self.path = path or POINTS_FILE
        self._lock = threading.Lock()
        self.settings = Settings()
        self.points: list[dict] = []
        self._next_id = 1
        self._bins: deque[_Bin] = deque()
        # (timestamp, {key: (temperature, faults)}), or None for a round in
        # which the chip was unavailable.
        self._temps: deque = deque()
        self._steady_since: float | None = None
        # The last settled point: its fT, id and the end of its window, and when
        # fT left it (None while it has not). Deleting that point forgets it.
        self._last_fT: float | None = None
        self._last_point: int | None = None
        self._last_end: float = 0.0
        self._left_at: float | None = None
        self._auto_error: str | None = None   # last reason an automatic point failed, said once
        self.save_error: str | None = None    # why the last save failed, shown in the status
        self._load()

    # ==================================================
    # Readings in
    # ==================================================

    def add_frequencies(self, timestamp: float, fM, fT, locked: bool):
        """One sample of both modes; `locked` only when both are."""
        with self._lock:
            second = int(timestamp)
            if self._bins and second < self._bins[-1].t:
                self._bins.clear()   # the clock stepped back (NTP): start over
            if not self._bins or self._bins[-1].t != second:
                self._bins.append(_Bin(second))
                while self._bins[0].t < second - HISTORY_S:
                    self._bins.popleft()
            b = self._bins[-1]
            if (locked and fM is not None and fT is not None
                    and math.isfinite(fM) and math.isfinite(fT)):
                b.n += 1
                b.fM += fM
                b.fT += fT
            else:
                b.locked = False

    def add_temperatures(self, timestamp: float, available: bool, sensors: list[dict]):
        """One round of LTC2986 readings, as in a TemperatureEvent."""
        with self._lock:
            readings = ({s["key"]: (s.get("temperature"), list(s.get("faults") or []))
                         for s in sensors} if available else None)
            if self._temps and timestamp < self._temps[-1][0]:
                self._temps.clear()
            self._temps.append((timestamp, readings))
            while self._temps[0][0] < timestamp - HISTORY_S:
                self._temps.popleft()

    # ==================================================
    # Evaluation
    # ==================================================

    def tick(self, run: str | None = None, now: float | None = None) -> tuple[dict, dict | None]:
        """Evaluate the readings; call about once a second. Takes the point
        itself when it is time to. Returns the status, and the point if one was
        taken. `run` is the run log being written, recorded with the point."""
        with self._lock:
            now = time.time() if now is None else now
            status = self._evaluate(now)
            if not (status["state"] == "settled" and status["armed"]):
                return status, None
            try:
                point = self._capture(now, None, auto=True, run=run, status=status)
            except ValueError as e:
                if str(e) != self._auto_error:
                    print(f"[CAL] Automatic point not taken: {e}")
                    self._auto_error = str(e)
                return status, None
            self._auto_error = None
            return self._evaluate(now), point

    def _sensor_values(self, start: float, key: str) -> tuple[list[tuple[float, float]], str | None]:
        """The sensor's readings since start, and the first problem among them."""
        name = SOURCES[key]
        values, problem = [], None
        for t, readings in self._temps:
            if t < start:
                continue
            if readings is None:
                problem = problem or "temperature sensors unavailable"
                continue
            temperature, faults = readings.get(key, (None, ["not reported"]))
            if faults or temperature is None:
                problem = problem or f"{name}: {', '.join(faults) or 'no reading'}"
            else:
                values.append((t, temperature))
        return values, problem

    def _departure(self) -> float | None:
        """When fT left the last point: the first second of a run of DEPART_S
        seconds in a row more than REARM_HZ away from it. Found once, then kept."""
        if self._left_at is None and self._last_fT is not None:
            run_start, run = None, 0
            for b in self._bins:
                if b.t < self._last_end or not b.n:
                    continue
                if abs(b.fT / b.n - self._last_fT) > REARM_HZ:
                    run_start = b.t if run == 0 else run_start
                    run += 1
                    if run >= DEPART_S:
                        self._left_at = run_start
                        break
                else:
                    run = 0
        return self._left_at

    def _window_start(self, now: float) -> float:
        """Where the window starts: window_s back, but never before fT left the
        last point, so the next point is judged and averaged on its own
        temperature and never partly on the old one."""
        start = now - self.settings.window_s
        left = self._departure()
        return start if left is None else max(start, left)

    def _evaluate(self, now: float) -> dict:
        s = self.settings
        start = self._window_start(now)
        bins = [b for b in self._bins if b.t >= start]
        good = [b for b in bins if b.n]
        latest = self._bins[-1] if self._bins else None
        status = {
            "state": None, "reason": None,
            "source": s.source, "source_name": SOURCES[s.source],
            "window_s": s.window_s, "hold_s": s.hold_s, "covered_s": len(good),
            "max_ft_drift": s.max_ft_drift, "max_temp_drift": s.max_temp_drift,
            "fM": None, "fT": None, "fM_drift": None, "fT_drift": None, "fT_recent_drift": None,
            "temperature": None, "temp_faults": [], "temp_drift": None,
            "steady_for": None, "auto_capture": s.auto_capture, "armed": False,
            "points": len(self.points), "save_error": self.save_error,
        }
        window_fT = None   # average over the window
        if good:
            status["fM"] = good[-1].fM / good[-1].n
            status["fT"] = good[-1].fT / good[-1].n
            ts = [b.t for b in good]
            fTs = [b.fT / b.n for b in good]
            window_fT = sum(fTs) / len(fTs)
            status["fM_drift"] = _slope_per_min(ts, [b.fM / b.n for b in good])
            status["fT_drift"] = _slope_per_min(ts, fTs)
            recent = [(t, f) for t, f in zip(ts, fTs) if t >= now - s.window_s / 3]
            if len(recent) >= 10:
                status["fT_recent_drift"] = _slope_per_min([t for t, _ in recent],
                                                           [f for _, f in recent])

        sensor = s.source != "manual"
        problem, values = None, []
        if sensor:
            if self._temps and now - self._temps[-1][0] <= STALE_S and self._temps[-1][1] is not None:
                temperature, faults = self._temps[-1][1].get(s.source, (None, ["not reported"]))
                status["temperature"], status["temp_faults"] = temperature, faults
            values, problem = self._sensor_values(start, s.source)
            if not self._temps or now - self._temps[-1][0] > STALE_S:
                problem = f"no current {SOURCES[s.source]} reading"
            elif problem and status["temperature"] is not None and not status["temp_faults"]:
                problem += " within the window"   # over now, but in the average
            status["temp_drift"] = _slope_per_min([t for t, _ in values], [v for _, v in values])

        fT_drift, temp_drift = status["fT_drift"], status["temp_drift"]
        fT_recent = status["fT_recent_drift"]
        steady = False
        if latest is None or now - latest.t > STALE_S:
            state, reason = "no_data", "no frequency readings"
        elif not latest.locked or not latest.n:
            state, reason = "unlocked", "both modes have to be locked"
        elif any(not b.locked for b in bins):
            state, reason = "unlocked", "the lock dropped within the window"
        elif problem:
            state, reason = "sensor", problem
        elif len(good) < COVERAGE * s.window_s or fT_drift is None or (
                sensor and (len(values) < COVERAGE * s.window_s / 2 or temp_drift is None)):
            state, reason = "collecting", f"{_mmss(len(good))} of {_mmss(s.window_s)} collected"
        elif abs(fT_drift) > s.max_ft_drift:
            state, reason = "settling", f"fT drifting {fT_drift:+.2f} Hz/min"
        elif fT_recent is not None and abs(fT_recent) > s.max_ft_drift:
            state, reason = ("settling", f"fT drifting {fT_recent:+.2f} Hz/min "
                                         f"over the last {_mmss(s.window_s / 3)}")
        elif sensor and abs(temp_drift) > s.max_temp_drift:
            state, reason = "settling", f"{SOURCES[s.source]} drifting {temp_drift:+.4f} °C/min"
        else:
            steady = True
            state, reason = "steady", None

        # Whether the last point was taken at this temperature: fT has not left
        # it, or has come back to within REARM_HZ of it on average.
        point_here = self._last_fT is not None and (
            self._departure() is None
            or (window_fT is not None and abs(window_fT - self._last_fT) <= REARM_HZ))
        if not steady:
            self._steady_since = None
        elif self._steady_since is None:
            self._steady_since = now
        if steady:
            held = now - self._steady_since
            status["steady_for"] = held
            if held < s.hold_s:
                reason = f"steady for {_mmss(held)} of {_mmss(s.hold_s)}"
            else:
                state = "settled"
                if point_here:
                    reason = "point taken here: set the next temperature"
                elif not sensor:
                    reason = "settled: enter the temperature and add the point"
                elif not s.auto_capture:
                    reason = "settled: add the point"
                else:
                    reason = "settled"

        status["point_here"] = point_here
        status["armed"] = bool(sensor and s.auto_capture and status["fT"] is not None
                               and not point_here)
        status["state"], status["reason"] = state, reason
        return status

    # ==================================================
    # Points
    # ==================================================

    def capture(self, temperature: float | None = None, run: str | None = None) -> dict:
        """Take a point now, settled or not; the caller asks the operator first.
        Raises ValueError when there is nothing to average or no temperature."""
        with self._lock:
            now = time.time()
            return self._capture(now, temperature, auto=False, run=run, status=self._evaluate(now))

    def _capture(self, now: float, temperature, auto: bool, run, status: dict) -> dict:
        s = self.settings
        start = self._window_start(now)
        # Only seconds in which every sample was locked.
        good = [b for b in self._bins if b.t >= start and b.n and b.locked]
        if len(good) < MIN_POINT_SECONDS:
            raise ValueError(f"only {len(good)} s of locked readings in the last "
                             f"{_mmss(s.window_s)}: lock both modes first")
        fM, fM_std = _mean_std([b.fM / b.n for b in good])
        fT, fT_std = _mean_std([b.fT / b.n for b in good])
        if s.source == "manual":
            if temperature is None or not math.isfinite(temperature):
                raise ValueError("enter the temperature of this point")
            temp, temp_std = float(temperature), None
        else:
            values, problem = self._sensor_values(start, s.source)
            if problem:
                raise ValueError(f"{problem} within the window")
            if not values:
                raise ValueError(f"no {SOURCES[s.source]} reading within the window")
            temp, temp_std = _mean_std([v for _, v in values])
        point = {
            "id": self._next_id,
            "time": now,
            "start": good[0].t, "end": good[-1].t + 1,   # the span averaged
            "temperature": round(temp, 4),
            "source": s.source, "source_name": SOURCES[s.source],
            "fM": fM, "fT": fT,
            # Scatter of the one-second averages over the window
            "fM_std": fM_std, "fT_std": fT_std, "temp_std": temp_std,
            "seconds": len(good),
            "fM_drift": status["fM_drift"], "fT_drift": status["fT_drift"],
            "temp_drift": status["temp_drift"],
            "settled": status["state"] == "settled",
            "auto": auto,
            "run": run,
        }
        self.points.append(point)
        self._next_id += 1
        # A settled point is the one for this temperature. One taken on the
        # way there is not, and must not hold up the point where it settles.
        if point["settled"]:
            self._remember(point)
        self._save()
        print(f"[CAL] Point {point['id']} {'taken automatically' if auto else 'added'}: "
              f"{point['temperature']:.3f} °C ({point['source_name']}), fM {fM:.2f} Hz, "
              f"fT {fT:.2f} Hz, {len(good)} s averaged"
              + ("" if point["settled"] else ", NOT settled"))
        return dict(point)

    def delete(self, point_id: int) -> bool:
        with self._lock:
            kept = [p for p in self.points if p["id"] != point_id]
            if len(kept) == len(self.points):
                return False
            self.points = kept
            if point_id == self._last_point:
                self._remember(None)   # this temperature may be taken again
            self._save()
            return True

    def clear(self):
        with self._lock:
            self.points = []
            self._remember(None)
            self._save()

    def _remember(self, point: dict | None):
        """Make `point` the last settled one, which the next automatic point
        has to move away from; None forgets it."""
        self._last_fT = point["fT"] if point else None
        self._last_point = point["id"] if point else None
        self._last_end = point["end"] if point else 0.0
        self._left_at = None

    # ==================================================
    # Settings
    # ==================================================

    def update_settings(self, **changes) -> dict:
        """Change any of the Settings fields; None leaves one as it is. Raises
        ValueError, changing nothing, if any value is out of range."""
        with self._lock:
            updated = self._validated(asdict(self.settings), changes)
            if updated["source"] != self.settings.source:
                self._steady_since = None   # a different thermometer starts over
            self.settings = Settings(**updated)
            self._save()
            return asdict(self.settings)

    @staticmethod
    def _validated(current: dict, changes: dict) -> dict:
        out = dict(current)
        for key, value in changes.items():
            if value is None:
                continue
            if key == "source":
                if value not in SOURCES:
                    raise ValueError(f"source must be one of {', '.join(SOURCES)}")
            elif key == "auto_capture":
                value = bool(value)
            elif key in LIMITS:
                lo, hi = LIMITS[key]
                value = float(value)
                if not lo <= value <= hi:
                    raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
            else:
                raise ValueError(f"unknown setting {key}")
            out[key] = value
        return out

    # ==================================================
    # Snapshots
    # ==================================================

    def status(self) -> dict:
        with self._lock:
            return self._evaluate(time.time())

    def contents(self) -> dict:
        """Settings and points, as sent to the GUI."""
        with self._lock:
            return {"settings": asdict(self.settings), "points": [dict(p) for p in self.points]}

    # ==================================================
    # Persistence
    # ==================================================

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            # A setting that no longer validates falls back to its default
            # rather than costing the points.
            settings = asdict(Settings())
            known = {f.name for f in fields(Settings)}
            for key, value in (saved.get("settings") or {}).items():
                if key in known:
                    try:
                        settings = self._validated(settings, {key: value})
                    except (TypeError, ValueError) as e:
                        print(f"[CAL] Ignoring saved setting {key}={value!r}: {e}")
            self.settings = Settings(**settings)
            self.points = list(saved.get("points") or [])
            self._next_id = max([int(saved.get("next_id") or 1)]
                                + [p["id"] + 1 for p in self.points])
            # A restart on a plateau that already has its point must not take
            # it again: wait, as before, to move away from the last settled one.
            settled = [p for p in self.points if p.get("settled")]
            if settled:
                self._remember(max(settled, key=lambda p: p["time"]))
            print(f"[CAL] Loaded {len(self.points)} calibration point(s) from {self.path}")
        except Exception as e:
            # Keep the unreadable file for inspection rather than overwrite it.
            print(f"[CAL] Could not read {self.path} ({e}); starting with no points")
            try:
                os.replace(self.path, self.path + ".bad")
            except OSError:
                pass
            self.settings, self.points, self._next_id = Settings(), [], 1
            self._remember(None)

    def _save(self):
        """Write everything, atomically: a power cut leaves the old file or the
        new one, never half of one."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "settings": asdict(self.settings),
                           "next_id": self._next_id, "points": self.points}, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.save_error = None
        except OSError as e:
            self.save_error = f"calibration points not saved: {e}"
            print(f"[CAL] {self.save_error}")
