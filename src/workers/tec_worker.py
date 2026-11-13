# Responsible for:
#
# running the calibration Peltier's temperature loop (domain/tec_controller.py)
# reading the feedback temperature: an LTC2986 sensor from the temperature
#   worker, the crystal's own temperature fed in by the server, or the PT100
#   and the crystal combined (domain/tec_controller.Fusion), which falls back
#   to the PT100 alone, on its own gains, whenever the crystal drops out
# writing the FPGA's PWM registers
# switching the TEC off on a limit, a lost reading, an error or a shutdown
# reporting every step as a TecStatusEvent
#
# A thread of its own so the loop keeps its pace whatever the QCM worker and the
# server are doing. Changed from the REST handlers, so every public method takes
# the one lock.
#
# The FPGA keeps driving whatever was last written if this process dies without
# switching it off, and the bitstream has no watchdog of its own: the bimetal
# thermostat on the holder is what stops a runaway then.

import threading
import time
import traceback
from dataclasses import asdict

from domain.tec_controller import (GAINS, LIMITS, PID, REGISTERS, RULES, SETTINGS_FILE, SOURCES, Fusion,
                                   RelayAutotune, Settings, load_settings, pwm_words, save_settings,
                                   validated, with_gains)
from messaging.api_event import TecStatusEvent


class TECWorker(threading.Thread):
    TICK_S = 0.1            # s between looks for a new reading
    MIN_STEP_S = 0.5        # s, least time between two control steps (the crystal reads at ~10 Hz)
    MAX_DT = 5.0            # s, longest step the integral takes in one go
    SENSOR_MAX_AGE = 3.0    # s, an LTC2986 round older than this is not current
    CRYSTAL_MAX_AGE = 2.0   # s, likewise for the crystal temperature
    STALE_S = 5.0           # s without a reading: drive to zero and hold the integral
    STALE_FAULT_S = 60.0    # s without a reading: switch off
    IDLE_PUBLISH_S = 1.0    # s between status events while nothing is stepping

    def __init__(self, fpga, event_queue, temperatures, settings_path: str = SETTINGS_FILE):
        super().__init__(daemon=True, name="tec")
        self.fpga = fpga
        self.event_queue = event_queue
        self._temperatures = temperatures   # () -> newest TemperatureEvent or None
        self._settings_path = settings_path
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        missing = [r for r in REGISTERS if not fpga.has_register(r)]
        self.available = not missing
        self.reason = None if self.available else f"the bitstream has no {', '.join(missing)} register"
        self._clock = fpga.sample_rate      # Hz, the PWM counter's clock

        self.settings: Settings = load_settings(settings_path)
        self.save_error: str | None = None
        self.mode = "off"                   # off | pid | autotune | fault
        self.fault: str | None = None       # why it switched itself off
        self.message: str | None = None     # the last thing worth telling the operator
        self.output = 0.0                   # %, signed, positive heats
        self._pid = PID()
        self._tune: RelayAutotune | None = None
        self.last_tune: dict | None = None
        self._crystal: tuple[float | None, float | None] = (None, None)   # (timestamp, °C)
        self._fusion = Fusion()
        self._fused = False                      # the crystal was in use at the last reading
        self._last_ts: float | None = None       # timestamp of the reading last stepped on
        self._last_good: float = time.time()     # when a usable reading last came in
        self._reading = (None, None, "no reading yet")   # (timestamp, °C, problem)
        self._last_publish = 0.0
        self._written: dict[str, int] = {}
        if self.available:
            # A restart must never resume an old drive.
            self._off(force=True)
        else:
            print(f"[TEC] Temperature control unavailable: {self.reason}")

    # ==================================================
    # Thread
    # ==================================================

    def stop(self):
        self._stop_event.set()

    def run(self):
        print("TEC worker started")
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    self._tick(time.time())
            except Exception:
                print(f"[TEC] Controller error:\n{traceback.format_exc()}")
                with self._lock:
                    self._trip("controller error, see the log")
            self._stop_event.wait(self.TICK_S)
        with self._lock:
            self._off()
            self.mode = "off"
        print("TEC worker stopped")

    def _tick(self, now: float):
        ts, pv, problem = self._reading = self._read(now)
        self._watch_fusion()
        fresh =(pv is not None and ts != self._last_ts
                 and (self._last_ts is None or ts - self._last_ts >= self.MIN_STEP_S))
        if fresh:
            self._last_good = now
        if self.mode in ("pid", "autotune"):
            if fresh:
                self._step(ts, pv, now)
                self._publish(now)
                return
            if now - self._last_good > self.STALE_FAULT_S:
                self._trip(f"no reading for {self.STALE_FAULT_S:g} s ({problem or 'stale'})")
            elif now - self._last_good > self.STALE_S and self.output != 0:
                self._drive(0.0)
                self.message = f"no reading ({problem or 'stale'}): drive held at zero"
        elif fresh:
            self._last_ts = ts
        if now - self._last_publish >= self.IDLE_PUBLISH_S:
            self._publish(now)

    def _watch_fusion(self):
        """React to the crystal joining or leaving the PT100 + crystal feedback."""
        fused = self.settings.source == "fused" and self._fusion.in_use
        if fused == self._fused:
            return
        self._fused = fused
        # The reading switches between crystal + offset and the PT100 alone, and
        # the gains with it: start the derivative afresh rather than take the
        # switch for a step. The integral, being drive, carries straight on.
        self._pid.forget_measurement()
        if fused or self.settings.source != "fused":
            if fused:
                print(f"[TEC] Crystal in use: PT100 - crystal {self._fusion.offset:+.3f} °C")
            if self.message and self.message.startswith("crystal dropped"):
                self.message = None
            return
        why = self._fusion.dropped
        print(f"[TEC] Crystal dropped ({why}): on the PT100 alone")
        if self.mode == "autotune":
            # The relay measures the loop it runs on; half on another one is void.
            self._tune.error = f"the crystal dropped out ({why})"
            self._tune_failed()
        elif self.mode == "pid":
            self.message = f"crystal dropped: {why}; on the PT100 alone"

    def _step(self, ts: float, pv: float, now: float):
        dt = min(ts - self._last_ts, self.MAX_DT) if self._last_ts is not None else 0.0
        self._last_ts = ts
        s = with_gains(self.settings, self._fused)
        over = self._over_limit(pv)
        if over:
            self._trip(over)
            return
        if self.mode == "autotune" and self._tune.approaching:
            # On the way to the setpoint: the PID drives, and its integral on
            # arrival is the bias the relay swings around.
            out = self._pid.step(s, pv, dt)
            if abs(s.setpoint - pv) <= self._tune.band:
                self._tune.begin(self._pid.i, ts)
                print(f"[TEC] Autotune: at the setpoint, relay ±{self._tune.d:g} % around {self._tune.bias:+.1f} %")
            else:
                self._tune.check_time(ts)
                if self._tune.error:
                    self._tune_failed()
        elif self.mode == "autotune":
            out = self._tune.step(pv, ts)
            if self._tune.result:
                self._tune_done(self._tune.result)
            elif self._tune.error:
                self._tune_failed()
        else:
            out = self._pid.step(s, pv, dt)
        if self.message and self.message.startswith("no reading"):
            self.message = None
        self._drive(out)

    def _over_limit(self, pv: float) -> str | None:
        s = self.settings
        if not s.t_min <= pv <= s.t_max:
            return (f"{SOURCES[s.source]} read {pv:.2f} °C, outside the limits "
                    f"{s.t_min:g} to {s.t_max:g} °C")
        # The PT100 watches the holder whatever the loop regulates on.
        if s.source != "pt100":
            pt100 = self._sensor("pt100", time.time())
            if pt100[1] is not None and pt100[1] > s.t_max:
                return f"PT100 read {pt100[1]:.2f} °C, above the limit of {s.t_max:g} °C"
        return None

    def _tune_failed(self):
        self.message = f"autotune stopped: {self._tune.error}; back on the previous gains"
        print(f"[TEC] {self.message}")
        if not self._tune.approaching:
            self._pid.reset(integral=self._tune.bias)
        self._tune = None
        self.mode = "pid"

    def _tune_done(self, r: dict):
        s = self.settings
        # Three significant figures: the measurement is not good to more, and
        # the settings form stays readable.
        gains = {k: float(f"{min(max(r[k], LIMITS[k][0]), LIMITS[k][1]):.3g}") for k in GAINS}
        # On PT100 + crystal the test measured that loop: its own gains, and
        # the single-sensor ones stay for when the crystal drops out.
        prefix = "fused_" if s.source == "fused" else ""
        self.settings = Settings(**{**asdict(s), **{prefix + k: v for k, v in gains.items()}})
        self.save_error = save_settings(self.settings, self._settings_path)
        self.last_tune = {**r, **gains, "source": s.source, "finished": time.time()}
        self._pid.reset(integral=r["bias"])
        self._tune = None
        self.mode = "pid"
        self.message = (f"autotune done: Ku {r['ku']:.3g} %/°C, Tu {r['tu']:.0f} s -> "
                        f"Kp {gains['kp']:.3g}, Ki {gains['ki']:.3g}, Kd {gains['kd']:.3g}"
                        + (" (PT100 + crystal gains)" if prefix else ""))
        print(f"[TEC] {self.message}")

    # ==================================================
    # Feedback temperature
    # ==================================================

    def _read(self, now: float) -> tuple[float | None, float | None, str | None]:
        """(timestamp, °C, None) for the selected source, or (None, None, why not)."""
        if self.settings.source == "fused":
            # The PT100 is required; the crystal only while it is current and
            # trusted, and the PT100 alone stands in otherwise.
            p_ts, p, p_problem = self._sensor("pt100", now)
            if p is None:
                return None, None, f"PT100: {p_problem}"
            c_ts, c = self._crystal
            problem = None
            if c is None or c_ts is None:
                problem = "crystal temperature not solved (both modes locked?)"
            elif now - c_ts > self.CRYSTAL_MAX_AGE:
                problem = "no current crystal temperature"
            if problem:
                c_ts = c = None
            ts, temp = self._fusion.update(self.settings, p_ts, p, c_ts, c, problem)
            return ts, temp, None
        if self.settings.source == "crystal":
            ts, temp = self._crystal
            if temp is None or ts is None:
                return None, None, "crystal temperature not solved (both modes locked?)"
            if now - ts > self.CRYSTAL_MAX_AGE:
                return None, None, "no current crystal temperature"
            return ts, temp, None
        return self._sensor(self.settings.source, now)

    def _sensor(self, key: str, now: float):
        reading = self._temperatures()
        if (reading is None or not reading.available or reading.timestamp is None
                or now - reading.timestamp > self.SENSOR_MAX_AGE):
            return None, None, "no current sensor reading"
        sensor = next((x for x in reading.sensors if x["key"] == key), None)
        if sensor is None:
            return None, None, f"no sensor {key}"
        if sensor["temperature"] is None or sensor["faults"]:
            return None, None, ", ".join(sensor["faults"]) or "no temperature"
        return reading.timestamp, float(sensor["temperature"]), None

    def feed_crystal(self, timestamp: float | None, temperature: float | None):
        """The crystal's absolute temperature, from the server's event loop; None
        while the modes are not both locked. No lock needed: one tuple, replaced whole."""
        self._crystal = (timestamp, temperature)

    # ==================================================
    # Registers
    # ==================================================

    def _drive(self, out: float):
        s = self.settings
        step, thresh, direction = pwm_words(self._clock, s.pwm_freq, out, s.heat_dir)
        self._write("PWM_freq", step)
        self._write("PWM_dir", direction)
        self._write("PWM_thresh", thresh)
        self._write("PWM_en", 1)
        self.output = out

    def _off(self, force: bool = False):
        """Bridge off: everything open. Enable first, so nothing is driven while
        the rest changes."""
        self.output = 0.0
        if not self.available:
            return
        try:
            self._write("PWM_en", 0, force)
            self._write("PWM_thresh", 0, force)
        except Exception as e:
            print(f"[TEC] Could not switch the bridge off: {e}")

    def _write(self, name: str, value: int, force: bool = False):
        if force or self._written.get(name) != value:
            self.fpga.write_register(name, int(value))
            self._written[name] = value

    def _trip(self, reason: str):
        self._off()
        self._tune = None
        self.mode = "fault"
        self.fault = reason
        self.message = None
        print(f"[TEC] Switched off: {reason}")
        self._publish(time.time())

    # ==================================================
    # Status
    # ==================================================

    def _status(self, now: float) -> dict:
        ts, pv, problem = self._reading
        s = self.settings
        return {
            "available": self.available, "reason": self.reason,
            "enabled": self.mode in ("pid", "autotune"), "mode": self.mode,
            "fault": self.fault, "message": self.message, "save_error": self.save_error,
            "source": s.source, "source_name": SOURCES[s.source],
            "temperature": pv, "reading_problem": problem,
            "setpoint": s.setpoint, "output": self.output,
            "p": self._pid.p, "i": self._pid.i, "d": self._pid.d,
            # PT100 + crystal: whether the crystal is in use (and why not), the
            # averaged PT100 - crystal and the newest crystal reading.
            "fusion": self._fusion.status() if s.source == "fused" else None,
            "gains": "fused" if self._fused else "single",
            "registers": dict(self._written),
            "autotune": self._tune.progress(now) if self._tune else None,
            "last_tune": self.last_tune,
            "timestamp": now,
        }

    def _publish(self, now: float):
        self._last_publish = now
        self.event_queue.put(TecStatusEvent(status=self._status(now)))

    def status(self) -> dict:
        with self._lock:
            return self._status(time.time())

    def contents(self) -> dict:
        """Settings, status and what the settings may be, for GET /tec."""
        with self._lock:
            return {"settings": asdict(self.settings), "status": self._status(time.time()),
                    "sources": SOURCES, "rules": {k: v[0] for k, v in RULES.items()},
                    "limits": LIMITS}

    # ==================================================
    # Commands (REST handlers)
    # ==================================================

    def update_settings(self, **changes) -> dict:
        """Change any of the settings; None leaves one as it is. Raises
        ValueError, changing nothing, if a value is out of range. Takes effect
        from the next step, without resetting the loop."""
        with self._lock:
            updated = validated(asdict(self.settings), changes)
            if updated["source"] != self.settings.source:
                self._pid.forget_measurement()   # the new reading is not a step in the old one
                self._fusion.reset()             # PT100 + crystal starts over when chosen again
            self.settings = Settings(**updated)
            self.save_error = save_settings(self.settings, self._settings_path)
            if self.mode in ("pid", "autotune") and self.output != 0:
                self._drive(self.output)   # a new frequency or heating direction at once
            self._publish(time.time())
            return asdict(self.settings)

    def set_enabled(self, enabled: bool):
        with self._lock:
            if enabled:
                if not self.available:
                    raise RuntimeError(self.reason)
                if self.mode not in ("pid", "autotune"):
                    self._pid.reset()
                    self._last_ts = None
                    self._last_good = time.time()
                    self.mode = "pid"
                    self.fault = None
                    self.message = None
                    print(f"[TEC] On: holding {self.settings.setpoint:g} °C on {SOURCES[self.settings.source]}")
            else:
                if self.mode != "off":
                    print("[TEC] Off")
                self._off()
                self._tune = None
                self.mode = "off"
                self.fault = None
                self.message = None
            self._publish(time.time())

    def start_autotune(self):
        with self._lock:
            if not self.available:
                raise RuntimeError(self.reason)
            ts, pv, problem = self._read(time.time())
            if pv is None:
                raise RuntimeError(f"no feedback temperature: {problem}")
            if self.mode == "autotune":
                return
            if self.settings.source == "fused" and not self._fusion.in_use:
                raise RuntimeError(f"the crystal is not in use yet ({self._fusion.dropped}): "
                                   f"PT100 + crystal can only be tuned with it")
            if self.mode != "pid":
                self._pid.reset()
                self._last_ts = None
            self._last_good = time.time()
            s = self.settings
            far = abs(s.setpoint - pv) > max(5 * s.at_hysteresis, 0.3)
            self._tune = RelayAutotune(s, self._pid.i, ts, approach=far)
            self.mode = "autotune"
            self.fault = None
            self.message = None
            if far:
                print(f"[TEC] Autotune started: first to {s.setpoint:g} °C on the PID")
            else:
                print(f"[TEC] Autotune started at {s.setpoint:g} °C, relay ±{self._tune.d:g} % "
                      f"around {self._tune.bias:+.1f} %")
            self._publish(time.time())

    def abort_autotune(self):
        with self._lock:
            if self.mode != "autotune":
                return
            if not self._tune.approaching:
                self._pid.reset(integral=self._tune.bias)
            self._tune = None
            self.mode = "pid"
            self.message = "autotune aborted; back on the previous gains"
            print(f"[TEC] {self.message}")
            self._publish(time.time())
