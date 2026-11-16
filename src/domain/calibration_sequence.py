"""Automatic calibration: a list of TEC setpoints, worked through one by one.

Each step is one of:

  point  go to the setpoint and wait for the settle detector (CALIBRATE tab,
         domain/calibration_recorder.py) to take a point there, then go on;
  soak   go to the setpoint and hold it for `minutes` and, when `max_fm_drift`
         is set, until the mass mode drifts less than that (Hz/min, fitted over
         the settle window). No point is taken: a break-in, or an anneal before
         the points so an irreversible shift happens before them rather than
         among them.

The TEC (workers/tec_worker.py) sets the temperature; the recorder decides
when a point is due. While a soak runs, and in a point step until the sensor
is within `tolerance` of the setpoint, the recorder's automatic points are held
back, so it takes no point during a break-in and none on the way to a setpoint.
A point counts for its step only when it is settled and lies within the
tolerance of the setpoint. Each point step re-arms the recorder, so a
temperature can be taken again after a soak with no point in between (the
reference points around an anneal).

A step that has not finished within `timeout_min` (for a soak: beyond its
minutes) is skipped, taken as it stands, or ends the sequence, as `on_timeout`
says. At the end the TEC switches off, holds the last setpoint, or goes to the
reference temperature (`at_end`). The TEC switching off, by hand or on a
fault, ends the sequence; so does a restart of the service, after which the
TEC comes up off.

The server ticks it once a second, right after the recorder, from its event
loop, and the REST handlers change it, so every public method takes the one
lock. The configuration goes to data/calibration/sequence.json; the progress
is not kept.
"""

import json
import math
import os
import threading
import time

# data/calibration/ at the repo root (this file is src/domain/calibration_sequence.py).
SEQUENCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..",
                             "data", "calibration", "sequence.json")

KINDS = ("point", "soak")
ON_TIMEOUT = ("skip", "take", "stop")
AT_END = ("off", "hold", "reference")
MAX_STEPS = 200

LIMITS = {
    "setpoint":     (-20.0, 100.0),    # °C
    "minutes":      (0.0, 24 * 60.0),  # soak hold
    "max_fm_drift": (0.0, 100.0),      # Hz/min, 0 = no drift condition
    "timeout_min":  (5.0, 24 * 60.0),
    "tolerance":    (0.05, 10.0),      # °C
    "reference":    (-20.0, 100.0),    # °C
}

DEFAULT_CONFIG = {
    "steps": [],
    "timeout_min": 90.0,
    "on_timeout": "skip",
    "tolerance": 0.5,
    "at_end": "off",
    "reference": 25.0,
    "generator": {},    # the GUI's generator inputs, kept for it verbatim
}

HISTORY = 50   # finished steps kept in the status


def _number(value, key: str) -> float:
    lo, hi = LIMITS[key]
    value = float(value)
    if not (math.isfinite(value) and lo <= value <= hi):
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return value


def validated(config: dict) -> dict:
    """A complete, checked configuration from a submitted one; missing keys
    take their defaults. Raises ValueError naming the first problem."""
    out = {k: config.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    steps = out["steps"]
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise ValueError(f"steps must be a list of at most {MAX_STEPS}")
    clean = []
    for i, step in enumerate(steps, 1):
        try:
            kind = step.get("kind")
            if kind not in KINDS:
                raise ValueError(f"kind must be one of {', '.join(KINDS)}")
            s = {"kind": kind, "setpoint": _number(step.get("setpoint"), "setpoint")}
            if kind == "soak":
                s["minutes"] = _number(step.get("minutes", 0), "minutes")
                s["max_fm_drift"] = _number(step.get("max_fm_drift", 0), "max_fm_drift")
                if s["minutes"] == 0 and s["max_fm_drift"] == 0:
                    raise ValueError("a soak needs minutes, a drift limit, or both")
        except (AttributeError, TypeError, ValueError) as e:
            raise ValueError(f"step {i}: {e}")
        clean.append(s)
    out["steps"] = clean
    out["timeout_min"] = _number(out["timeout_min"], "timeout_min")
    out["tolerance"] = _number(out["tolerance"], "tolerance")
    out["reference"] = _number(out["reference"], "reference")
    if out["on_timeout"] not in ON_TIMEOUT:
        raise ValueError(f"on_timeout must be one of {', '.join(ON_TIMEOUT)}")
    if out["at_end"] not in AT_END:
        raise ValueError(f"at_end must be one of {', '.join(AT_END)}")
    if not isinstance(out["generator"], dict) or len(json.dumps(out["generator"])) > 4000:
        raise ValueError("generator must be a small object")
    return out


def describe(step: dict) -> str:
    if step["kind"] == "point":
        return f"point at {step['setpoint']:g} °C"
    conditions = []
    if step["minutes"]:
        conditions.append(f"{step['minutes']:g} min")
    if step["max_fm_drift"]:
        conditions.append(f"fM drift <= {step['max_fm_drift']:g} Hz/min")
    return f"soak at {step['setpoint']:g} °C ({', '.join(conditions)})"


def _mmss(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


class CalibrationSequence:
    def __init__(self, path: str = SEQUENCE_FILE):
        self.path = path
        self._lock = threading.Lock()
        self.config = dict(DEFAULT_CONFIG)
        self.save_error: str | None = None
        self.state = "idle"            # idle | running | done | stopped | failed
        self.message: str | None = None
        self.index: int | None = None  # step running
        self.started: float | None = None
        self._step_started: float | None = None
        self._steps: list[dict] = []   # the configuration's steps as started
        self.history: list[dict] = []  # finished steps: index, outcome, point id, times
        self._waiting: str | None = None   # what the step is waiting for, for the status
        self._auto_was_off = False         # AUTO was off before START, so goes off again at the end
        self._load()

    # ==================================================
    # Configuration
    # ==================================================

    def update_config(self, config: dict) -> dict:
        """Replace the configuration. Refused while a sequence runs: it works
        through the steps it was started with."""
        with self._lock:
            if self.state == "running":
                raise RuntimeError("a sequence is running: stop it before changing it")
            self.config = validated(config)
            self._save()
            return dict(self.config)

    def contents(self) -> dict:
        with self._lock:
            return {"config": dict(self.config), "status": self._status(time.time())}

    def status(self) -> dict:
        with self._lock:
            return self._status(time.time())

    # ==================================================
    # Running
    # ==================================================

    def start(self, tec, recorder):
        """Check everything a sequence needs, then switch the TEC on and go to
        the first step. Raises RuntimeError saying what is missing."""
        with self._lock:
            if self.state == "running":
                raise RuntimeError("a sequence is already running")
            steps = list(self.config["steps"])
            if not steps:
                raise RuntimeError("the sequence has no steps (Settings → Calibration Sequence)")
            if not any(s["kind"] == "point" for s in steps):
                raise RuntimeError("the sequence takes no points")
            if tec is None or not tec.status()["available"]:
                raise RuntimeError("TEC control is not available: "
                                   + (tec.status()["reason"] if tec else "no TEC worker"))
            if recorder.settings.source == "manual":
                raise RuntimeError("set the CALIBRATE tab's TEMP source to a sensor: the "
                                   "sequence takes its points automatically")
            t_min, t_max = tec.settings.t_min, tec.settings.t_max
            temps = [s["setpoint"] for s in steps]
            if self.config["at_end"] == "reference":
                temps.append(self.config["reference"])
            outside = sorted({t for t in temps if not t_min <= t <= t_max})
            if outside:
                raise RuntimeError(f"{', '.join(f'{t:g}' for t in outside)} °C lie outside the TEC's "
                                   f"safety limits ({t_min:g} to {t_max:g} °C)")
            # Hold automatic points back before anything else, so the recorder
            # cannot take one at the old temperature in between.
            recorder.hold_auto("calibration sequence starting")
            recorder.rearm()
            self._auto_was_off = not recorder.settings.auto_capture
            if self._auto_was_off:
                recorder.update_settings(auto_capture=True)
                print("[SEQ] AUTO switched on for the sequence")
            self._steps = steps
            self.history = []
            self.started = time.time()
            self.state = "running"
            self.message = None
            print(f"[SEQ] Started: {len(steps)} steps, "
                  f"{sum(s['kind'] == 'point' for s in steps)} points")
            try:
                self._begin(0, tec, recorder, time.time())
                tec.set_enabled(True)
            except Exception as e:
                self._end("failed", f"could not start: {e}", recorder)
                raise RuntimeError(str(e))

    def stop(self, recorder, reason: str = "stopped by hand"):
        """End the sequence. The TEC goes on holding the setpoint it has."""
        with self._lock:
            if self.state == "running":
                self._end("stopped", f"{reason}; the TEC goes on holding the setpoint", recorder)

    def skip(self, tec, recorder):
        with self._lock:
            if self.state == "running":
                self._finish_step("skipped by hand", None, tec, recorder, time.time())

    def tick(self, now: float, status: dict, added: dict | None, tec, recorder, capture):
        """Once a second, after the recorder: status is its status, added the
        point it just took (or None). capture() takes a point now."""
        with self._lock:
            if self.state != "running":
                return
            ts = tec.status()
            if not ts["enabled"]:
                why = ts["fault"] or "it was switched off"
                self._end("failed", f"the TEC is off ({why})", recorder)
                return
            step = self._steps[self.index]
            sp, tol = step["setpoint"], self.config["tolerance"]
            elapsed = now - self._step_started
            timeout = self.config["timeout_min"] * 60
            temp = status.get("temperature")

            if step["kind"] == "point":
                near = temp is not None and abs(temp - sp) <= tol
                recorder.hold_auto(None if near else f"sequence: on the way to {sp:g} °C")
                if (added and added.get("settled") and added["time"] >= self._step_started
                        and abs(added["temperature"] - sp) <= tol):
                    self._finish_step("point taken", added["id"], tec, recorder, now)
                    return
                if not near:
                    t = "—" if temp is None else f"{temp:.2f} °C"
                    self._waiting = f"on the way: {status.get('source_name', 'sensor')} {t}"
                else:
                    self._waiting = (status.get("state") or "waiting") + (
                        f": {status['reason']}" if status.get("reason") else "")
                if elapsed > timeout:
                    self._timed_out(f"no point within {self.config['timeout_min']:g} min",
                                    tec, recorder, now, capture)
            else:
                recorder.hold_auto("sequence: soaking")
                need_t = step["minutes"] * 60
                drift = status.get("fM_drift")
                drift_ok = not step["max_fm_drift"] or (
                    drift is not None and status.get("state") not in ("no_data", "unlocked", "collecting")
                    and abs(drift) <= step["max_fm_drift"])
                if elapsed >= need_t and drift_ok:
                    self._finish_step("soak done", None, tec, recorder, now)
                    return
                parts = []
                if elapsed < need_t:
                    parts.append(f"{_mmss(need_t - elapsed)} to go")
                if step["max_fm_drift"]:
                    parts.append("fM drift " + ("—" if drift is None else f"{drift:+.3f}") +
                                 f" of ≤ {step['max_fm_drift']:g} Hz/min")
                self._waiting = "soaking: " + ", ".join(parts)
                if elapsed > need_t + timeout:
                    self._timed_out(f"fM still drifting {drift:+.3f} Hz/min" if drift is not None
                                    else "no fM drift reading", tec, recorder, now, None)

    def _timed_out(self, why: str, tec, recorder, now: float, capture):
        policy = self.config["on_timeout"]
        if policy == "stop":
            self._end("failed", f"step {self.index + 1} timed out ({why})", recorder)
        elif policy == "take" and capture is not None:
            try:
                point = capture()
                self._finish_step(f"timed out ({why}); point taken unsettled", point["id"],
                                  tec, recorder, now)
            except ValueError as e:
                self._finish_step(f"timed out ({why}); no point: {e}", None, tec, recorder, now)
        else:
            self._finish_step(f"timed out ({why}); skipped", None, tec, recorder, now)

    def _finish_step(self, outcome: str, point_id, tec, recorder, now: float):
        step = self._steps[self.index]
        self.history.append({"index": self.index, "step": step, "outcome": outcome, "point": point_id,
                             "start": self._step_started, "end": now})
        self.history = self.history[-HISTORY:]
        print(f"[SEQ] Step {self.index + 1}/{len(self._steps)} ({describe(step)}): {outcome}")
        if self.index + 1 < len(self._steps):
            try:
                self._begin(self.index + 1, tec, recorder, now)
            except Exception as e:
                self._end("failed", f"step {self.index + 2}: {e}", recorder)
            return
        end = self.config["at_end"]
        try:
            if end == "off":
                tec.set_enabled(False)
                note = "TEC switched off"
            elif end == "reference":
                tec.update_settings(setpoint=self.config["reference"])
                note = f"TEC holding the reference {self.config['reference']:g} °C"
            else:
                note = f"TEC holding {step['setpoint']:g} °C"
        except Exception as e:
            note = f"end of sequence: {e}"
        points = sum(1 for h in self.history if h["point"] is not None)
        self._end("done", f"{points} points in {_mmss(now - self.started)}; {note}", recorder)

    def _begin(self, index: int, tec, recorder, now: float):
        self.index = index
        self._step_started = now
        step = self._steps[index]
        self._waiting = None
        if step["kind"] == "point":
            # The recorder will not take a second point where it took the last
            # one until another has been taken elsewhere, which would stall a
            # reference point after a soak. Forget the last one: points stay
            # held back until the sensor is at this setpoint, so the old
            # plateau cannot be taken again.
            recorder.hold_auto(f"sequence: on the way to {step['setpoint']:g} °C")
            recorder.rearm()
        else:
            recorder.hold_auto("sequence: soaking")
        tec.update_settings(setpoint=step["setpoint"])
        print(f"[SEQ] Step {index + 1}/{len(self._steps)}: {describe(step)}")

    def _end(self, state: str, message: str, recorder):
        self.state = state
        self.message = message
        self._waiting = None
        recorder.hold_auto(None)
        if self._auto_was_off:
            # Otherwise it goes on taking points by itself, at whatever the TEC
            # holds after the sequence.
            recorder.update_settings(auto_capture=False)
            self._auto_was_off = False
            print("[SEQ] AUTO switched off again")
        print(f"[SEQ] {state.capitalize()}: {message}")

    def _status(self, now: float) -> dict:
        running = self.state == "running"
        step = self._steps[self.index] if running else None
        return {
            "state": self.state, "message": self.message, "save_error": self.save_error,
            "index": self.index if running else None, "total": len(self._steps) if running else None,
            "step": step, "description": describe(step) if step else None,
            "elapsed": now - self._step_started if running else None,
            "started": self.started, "waiting": self._waiting,
            "points": sum(1 for h in self.history if h["point"] is not None),
            "history": self.history[-10:],
        }

    # ==================================================
    # Persistence
    # ==================================================

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                self.config = validated(json.load(f))
        except (OSError, ValueError, TypeError) as e:
            print(f"[SEQ] Saved sequence not read, starting empty: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.save_error = None
        except OSError as e:
            self.save_error = f"sequence not saved: {e}"
            print(f"[SEQ] {self.save_error}")
