# Responsible for:
#
# running the calibration Peltier's temperature loop (domain/tec_controller.py)
# reading the feedback temperature: the PT100 from the temperature worker, and
#   on "PT100 + crystal for D" the crystal's own temperature fed in by the
#   server for the derivative, while domain/tec_controller.CrystalCheck trusts
#   it (the PT100 at a quarter of Kd otherwise)
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

from domain.tec_controller import (GAINS, LIMITS, PID, REGISTERS, RULES, SETTINGS_FILE, SOURCES, CrystalCheck,
                                   RelayAutotune, Settings, load_settings, pwm_words, save_settings,
                                   validated)
from messaging.api_event import TecStatusEvent


class TECWorker(threading.Thread):
    TICK_S = 0.1            # s between looks for a new reading
    MIN_STEP_S = 0.5        # s, least time between two control steps (the crystal reads at ~10 Hz)
    CRYSTAL_D = "pt100_crystal_d"   # the source with the derivative on the crystal
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
        self._check = CrystalCheck()
        self._crystal_d = False                  # the derivative was on the crystal at the last reading
        self._last_ts: float | None = None       # timestamp of the reading last stepped on
        self._last_good: float = time.time()     # when a usable reading last came in
        self._reading = (None, None, "no reading yet")   # (timestamp, PT100 °C, problem)
        self._d_pv: float | None = None          # what the derivative acts on: the crystal, or the PT100
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
        ts, pv, self._d_pv, problem = self._read(now)
        self._reading = (ts, pv, problem)
        self._watch_crystal()
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

    def _watch_crystal(self):
        """React to the derivative moving between the crystal and the PT100."""
        crystal_d = self.settings.source == self.CRYSTAL_D and self._check.in_use
        if crystal_d == self._crystal_d:
            return
        self._crystal_d = crystal_d
        # The derivative's reading switches between the crystal and the PT100:
        # start it afresh rather than take the switch for a step. P and I act
        # on the PT100 either way and carry straight on.
        self._pid.forget_measurement()
        if crystal_d or self.settings.source != self.CRYSTAL_D:
            if crystal_d:
                print("[TEC] Derivative on the crystal")
            if self.message and self.message.startswith("crystal dropped"):
                self.message = None
            return
        why = self._check.dropped
        print(f"[TEC] Crystal dropped ({why}): derivative on the PT100")
        if self.mode in ("pid", "autotune"):
            self.message = f"crystal dropped: {why}"   # the status says where the derivative is

    def _step(self, ts: float, pv: float, now: float):
        dt = min(ts - self._last_ts, self.MAX_DT) if self._last_ts is not None else 0.0
        self._last_ts = ts
        s = self.settings
        # The derivative: on the crystal while it is trusted, on the PT100 with
        # its own source, and on the PT100 at a share of Kd when the crystal
        # has dropped out (see CrystalCheck).
        kd = s.kd * CrystalCheck.PT100_KD_SHARE if s.source == self.CRYSTAL_D and not self._crystal_d else s.kd
        over = self._over_limit(pv)
        if over:
            self._trip(over)
            return
        if self.mode == "autotune" and self._tune.approaching:
            # On the way to the setpoint: the PID drives, and its integral on
            # arrival is the bias the relay swings around.
            out = self._pid.step(s, pv, dt, self._d_pv, kd)
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
            out = self._pid.step(s, pv, dt, self._d_pv, kd)
        if self.message and self.message.startswith("no reading"):
            self.message = None
        self._drive(out)

    def _over_limit(self, pv: float) -> str | None:
        """Why the PT100 reading pv trips the loop, or None. The crystal's
        reading is never checked: it need not be calibrated."""
        s = self.settings
        if not s.t_min <= pv <= s.t_max:
            return f"PT100 read {pv:.2f} °C, outside the limits {s.t_min:g} to {s.t_max:g} °C"
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
        # The relay runs on the PT100, so it measures what P and I on the
        # PT100 can take, and a derivative on the PT100. With D on the crystal
        # Kd stays: the crystal leads the PT100 and takes about four times the
        # derivative (see CrystalCheck), which the relay cannot tell.
        keep_kd = s.source == self.CRYSTAL_D
        taken = {k: v for k, v in gains.items() if not (keep_kd and k == "kd")}
        self.settings = Settings(**{**asdict(s), **taken})
        self.save_error = save_settings(self.settings, self._settings_path)
        self.last_tune = {**r, **gains, "source": s.source, "kd_kept": keep_kd, "finished": time.time()}
        self._pid.reset(integral=r["bias"])
        self._tune = None
        self.mode = "pid"
        self.message = (f"autotune done: Ku {r['ku']:.3g} %/°C, Tu {r['tu']:.0f} s -> "
                        f"Kp {gains['kp']:.3g}, Ki {gains['ki']:.3g}, "
                        + (f"Kd kept at {s.kd:.3g} (on the crystal; the PT100 alone would take "
                           f"{gains['kd']:.3g})" if keep_kd else f"Kd {gains['kd']:.3g}"))
        print(f"[TEC] {self.message}")

    # ==================================================
    # Feedback temperature
    # ==================================================

    def _read(self, now: float) -> tuple[float | None, float | None, float | None, str | None]:
        """(timestamp, PT100 °C, °C for the derivative, None), or (None, None,
        None, why not). The PT100 is required. With D on the crystal, a crystal
        reading CrystalCheck trusts goes to the derivative and its timestamp
        paces the steps (the crystal reads ten times a second, the PT100 once);
        otherwise the PT100 serves both."""
        p_ts, p, problem = self._sensor("pt100", now)
        if p is None:
            return None, None, None, f"PT100: {problem}"
        if self.settings.source != self.CRYSTAL_D:
            return p_ts, p, p, None
        c_ts, c = self._crystal
        c_problem = None
        if c is None or c_ts is None:
            c_problem = "crystal temperature not solved (both modes locked?)"
        elif now - c_ts > self.CRYSTAL_MAX_AGE:
            c_problem = "no current crystal temperature"
        if c_problem:
            c_ts = c = None
        if self._check.update(p, c_ts, c, c_problem):
            return c_ts, p, c, None
        return p_ts, p, p, None

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
            # D on the crystal: whether the crystal is in use (and why not),
            # its newest reading and how it moves against the PT100.
            "crystal_d": self._check.status() if s.source == self.CRYSTAL_D else None,
            "d_on": "crystal" if self._crystal_d else "pt100",
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
                self._check.reset()              # the crystal's checks start over when chosen again
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
            ts, pv, _, problem = self._read(time.time())
            if pv is None:
                raise RuntimeError(f"no feedback temperature: {problem}")
            if self.mode == "autotune":
                return
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
