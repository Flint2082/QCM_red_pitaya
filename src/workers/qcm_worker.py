# Responsible for:

# executing commands
# owning hardware access
# running sweeps
# acquisition loops

# async 
# Listens for commands from application, executes them
# Sends events back to application (e.g. measurement complete, sweep step done, etc.)

import math
import time
import threading
import queue
from dataclasses import dataclass

from domain.qcm_interface import QCMInterface
from domain.run_logger import RunLogger
from domain.sweep_logger import SweepLogger
from messaging.defines import WorkerState
from messaging.worker_event import *
from messaging.worker_command import *


@dataclass
class _LockAttempt:
    """A GET LOCK (or automatic re-lock) in progress — see QCMWorker._start_lock."""
    active: tuple          # oscillators being locked (the rest are parked)
    started: float         # time.time() when the loops were released
    resume: bool           # a run was in progress: back to MEASURING afterwards
    settled: bool = False  # past LOCK_SETTLE: the lock windows now hold judging samples


class QCMWorker(threading.Thread):
    # Commands that need IDLE. While a lock blocked the worker they simply waited
    # in the queue until it was done; a lock that runs alongside the acquisition
    # holds them back until it ends instead, so none is dropped for arriving
    # mid-lock (an OPC GetLock followed straight by StartMeasurement, say).
    _DEFER_WHILE_LOCKING = (StartMeasurementCommand, StartSweepCommand, StartCapAdjustCommand)

    def __init__(self, qcm: QCMInterface, command_queue: queue.Queue, event_queue: queue.Queue,
                 version=None, temperatures=None):
        super().__init__()
        self.qcm = qcm
        self.version = version  # domain.version.VersionInfo, recorded with each run
        self.command_queue = command_queue
        self.event_queue = event_queue
        self.running = True
        self.state = WorkerState.IDLE
        # Durable server-side CSV log of each run, independent of the WebSocket.
        # Logging problems are pushed to the UI: the live plots keep working when
        # the log is dead, so nothing else would give the operator a clue.
        # temperatures() supplies the LTC2986 readings written beside each sample.
        self.logger = RunLogger(
            temperatures=temperatures,
            on_start=lambda name: self.event_queue.put(RunLogStartedEvent(name=name)),
            on_error=lambda reason: self.event_queue.put(RunLogFailedEvent(reason=reason)),
            on_reclaim=lambda summary, free: self.event_queue.put(
                StorageReclaimedEvent(summary=summary, free_bytes=free)),
        )
        # Durable server-side CSV of each sweep. Unlike the run log a failure here
        # is only printed: the sweep still streams to the UI, and a sweep is cheap
        # to repeat, so it does not warrant its own alarm in the GUI.
        self.sweep_logger = SweepLogger()
        self._run_command = None  # last StartMeasurementCommand, for RetryRunLogCommand
        # Target thickness (nm of compensated thickness; 0 = no target). Checked
        # against every measurement; crossing it raises a flag for the operator,
        # the REST API and the PLC, but never stops the run on its own.
        self.target_thickness = 0.0
        self._target_reached = False
        self._last_thickness = 0.0  # most recent compensated thickness, for target changes
        # Lock-status emission throttle: send on change, plus a periodic
        # heartbeat so new WS clients converge. Unthrottled this produced
        # ~10 events/sec of WS traffic even when idle.
        self.LOCK_STATUS_HEARTBEAT = 1.0  # seconds
        self._last_lock_status: tuple | None = None
        self._last_lock_emit = 0.0
        # Automatic re-lock: if the lock stays lost this long during a
        # measurement, re-acquire around the last lock frequencies. A temp-only
        # loss that the temperature hold covers does not count — see update().
        self.AUTO_RELOCK_AFTER = 1.0  # seconds
        self.auto_relock = True  # configurable; when off a lost lock is left alone
        self._lock_freqs: tuple | None = None  # (mass, temp) from the last GET LOCK
        self._lock_lost_since: float | None = None
        # Lock attempts. A single attempt — one release of the loops, judged once
        # they have had LOCK_SETTLE to pull in: success as soon as every lock
        # window has filled and passes, failure if that has not happened by
        # LOCK_TIMEOUT. The worker keeps acquiring all the while (see update).
        self.LOCK_SETTLE = 1.0   # s before the lock windows start counting
        self.LOCK_TIMEOUT = 5.0  # s from release to giving up
        self._lock: _LockAttempt | None = None
        self._lock_active: tuple = (1, 2)  # oscillators the last attempt locked
        self._deferred: list = []          # _DEFER_WHILE_LOCKING commands held back
        # Measurement mode. Compensated locks both modes and solves the
        # temperature from fT; uncompensated locks the mass mode alone and parks
        # the temp oscillator. A run keeps the mode it started in, re-locks
        # included, whatever the setting does meanwhile.
        self.compensated = True
        self._run_compensated = True
        self._comp_ok = True  # last sample produced a thickness (LED 1 flashes if not)
        # Temperature hold: while the temp-mode lock is lost, carry the last
        # solved temperature into the compensation instead of solving it from an
        # fT that is no longer a resonance. The mass mode is still measured, so
        # only the thermal correction freezes — see TempCompAlgorithm.FreqToTemp.
        self.temp_hold = True
        self._temp_held = False  # currently holding, for one-shot logging
        # Lock-frequency tracking: while both modes are locked, periodically
        # re-point the re-lock start frequencies at what the PLLs are actually
        # holding. Over a long deposition the resonances walk away from the
        # crystal profile's stored frequencies (film loading, temperature), and a
        # re-lock aimed at the stored values can end up outside the capture
        # window; aimed at the last known good frequencies it lands in it.
        self.freq_tracking = True
        self.freq_track_interval = 10.0  # seconds between updates
        self._last_freq_track = 0.0
        # Whether a lock attempt has aimed the loops at the start frequencies,
        # with nothing taking them off since. Not at power-up, where the NCOs
        # start from 0 Hz with no resonance anywhere near, nor after a sweep or a
        # capacitor adjustment, which leave them open-loop: the lock detect then
        # judges a phase that no resonance is holding, and a quiet one passes it.
        # Nothing read in that state may move the start frequencies — that is
        # how a fresh start used to overwrite them with 0 Hz.
        self._loops_aimed = False
        # Automatic gain control. The QCM owns the servo itself; the worker owns
        # when it is allowed to run (see _update_agc) and reports a channel that
        # has run out of drive range.
        self._agc_railed = {1: False, 2: False}
        
    def stop(self):
        self.running = False
        self.command_queue.put(None) # unblock queue.get()

    def run(self):
        print("QCM worker started")

        while self.running:
            try:
                try:
                    command = self.command_queue.get(timeout=0.1) # non-blocking with timeout

                    if command is not None:
                        self.handle_command(command)

                except queue.Empty:
                    pass

                self.update()

            except Exception as e:
                self.event_queue.put(
                    ErrorEvent(str(e))
                )

        self.logger.stop()  # flush & close any open run log on shutdown
        self.qcm.leds.off()
        print("QCM worker stopped")

    def _set_state(self, new_state: WorkerState):
        self.state = new_state
        self.event_queue.put(StateEvent(state=new_state))

    def _in_run(self) -> bool:
        """A run is in progress: measuring, or re-locking in the middle of one."""
        return self.state == WorkerState.MEASURING or (self._lock is not None and self._lock.resume)

    def _compensated_now(self) -> bool:
        """The measurement mode in force: the run's own while one is going."""
        return self._run_compensated if self._in_run() else self.compensated

    # ============================
    # Locking
    # ============================

    def _start_lock(self, mass: float, temp: float):
        """Release the loops around (mass, temp) for a single lock attempt and
        return at once; update() carries it to the end (_advance_lock). If a run
        is in progress it re-locks without stopping it: the measurement reference
        is left alone, so the deposition baseline carries through. Used by GET
        LOCK and by the automatic re-lock; a GET LOCK during an attempt starts it
        over. Caches the frequencies for the auto re-lock to reuse.

        In uncompensated mode only the mass mode is locked, and either mode is
        parked rather than locked when its start frequency is 0 Hz."""
        self._lock_freqs = (mass, temp)
        resume = self._in_run()
        oscs = (1, 2) if self._compensated_now() else (1,)
        self._lock = None
        self._loops_aimed = False  # until beginLock has set them up
        try:
            active = self.qcm.beginLock(mass, temp, oscs)
        except Exception as e:
            self._abort_lock(e, resume)
            return
        self._lock_active = active
        self._loops_aimed = bool(active)
        if not active:
            self.qcm.finishLock(active, False)  # says why
            self.event_queue.put(LockFailedEvent())
            self._lock_lost_since = None
            self._set_state(WorkerState.MEASURING if resume else WorkerState.IDLE)
            return
        self._lock = _LockAttempt(active=active, started=time.time(), resume=resume)
        if resume:
            self.logger.write_event("RELOCK_START", "re-acquiring osc " + ", ".join(map(str, active)))
        self._set_state(WorkerState.LOCKING)

    def _advance_lock(self, now: float):
        """One acquisition cycle of the lock attempt in flight."""
        a = self._lock
        if a is None:
            # LOCKING with nothing in flight would never end — and LOCKING
            # refuses START, sweeps and the capacitor adjust.
            self._set_state(WorkerState.IDLE)
            return
        try:
            if not a.settled:
                if now - a.started >= self.LOCK_SETTLE:
                    # Judge only on samples taken once the loops have pulled in.
                    self.qcm.resetLockStats()
                    a.settled = True
                return
            if all(self.qcm.isLockWindowFull(i) and self.qcm.getLockDetect(i) for i in a.active):
                self._finish_lock(True)
            elif now - a.started >= self.LOCK_TIMEOUT:
                self._finish_lock(False)
        except Exception as e:
            self._abort_lock(e, a.resume)

    def _finish_lock(self, locked: bool):
        a, self._lock = self._lock, None
        self.qcm.finishLock(a.active, locked)
        if not locked:
            self.event_queue.put(LockFailedEvent())
        if a.resume:
            self.logger.write_event("RELOCK", "lock re-established" if locked else "re-lock failed")
        self._lock_lost_since = None  # reset the loss timer after any attempt
        self._set_state(WorkerState.MEASURING if a.resume else WorkerState.IDLE)
        self._replay_deferred()

    def _abort_lock(self, error, resume: bool):
        """End a lock attempt that raised. Whatever went wrong, the worker must not
        stay in LOCKING: that is how a parked (0 Hz) mode used to hang GET LOCK
        until the service was restarted."""
        print(f"[QCM] Lock attempt aborted: {error}")
        # Raised in beginLock, there is no attempt yet; the channels it meant to
        # lock are the ones it did not park.
        active = (self._lock.active if self._lock is not None
                  else tuple(i for i in (1, 2) if not self.qcm.isParked(i)))
        self._lock = None
        try:
            self.qcm.finishLock(active, False, report=False)
        except Exception as e:
            print(f"[QCM] Could not restore the loop settings after the failed lock: {e}")
        self.event_queue.put(ErrorEvent(f"lock attempt aborted: {error}"))
        self.event_queue.put(LockFailedEvent())
        if resume:
            self.logger.write_event("RELOCK", f"re-lock aborted: {error}")
        self._lock_lost_since = None
        self._set_state(WorkerState.MEASURING if resume else WorkerState.IDLE)
        self._replay_deferred()

    def _replay_deferred(self):
        pending, self._deferred = self._deferred, []
        for command in pending:
            try:
                self.handle_command(command)
            except Exception as e:
                self.event_queue.put(ErrorEvent(str(e)))

    def _held_lock_freqs(self) -> tuple:
        """(mass, temp) being held, rounded to 100 Hz, for the start frequencies.
        A channel the last lock did not lock keeps the start frequency it had:
        what a parked oscillator reads is not a resonance."""
        prev = self._lock_freqs or (0.0, 0.0)
        return tuple(round(self.qcm.getFreq(i) / 100) * 100 if i in self._lock_active else prev[i - 1]
                     for i in (1, 2))

    def _set_target_reached(self, reached: bool, thickness: float = 0.0):
        """Raise or clear the target-thickness flag and tell everyone once.
        Clearing is emitted too, so the UI, the REST API and the PLC never have
        to guess when a new run invalidates the previous run's flag."""
        if reached == self._target_reached:
            return
        self._target_reached = reached
        self.event_queue.put(TargetReachedEvent(
            reached=reached, thickness=thickness, target=self.target_thickness))

    def _settings_snapshot(self, command) -> dict:
        """Everything that affects how a run is acquired: the loop/lock config the
        QCM owns, plus the worker's own automation flags, this run's material
        parameters and the firmware version. Recorded once at run start."""
        snapshot = self.qcm.getSettingsSnapshot()
        snapshot["auto_relock"] = self.auto_relock
        snapshot["temp_hold"] = self.temp_hold
        snapshot["storage_reclaim"] = {
            "prune_old_runs":   self.logger.prune_old_runs,
            "trim_current_run": self.logger.trim_current_run,
        }
        snapshot["freq_tracking"] = {
            "enabled":  self.freq_tracking,
            "interval": self.freq_track_interval,
        }
        snapshot["measurement"] = {
            "mode":             "compensated" if self._run_compensated else "uncompensated",
            "ambient_temp":     command.ambient_temp,
            "mat_dens":         command.mat_dens,
            "z_ratio":          command.z_ratio,
            "target_thickness": self.target_thickness,
        }
        # Which software and bitstream took the run.
        if self.version is not None:
            snapshot["version"] = self.version.as_dict()
        return snapshot

    def _start_run_log(self, command) -> bool:
        """Open the run log and write its opening rows. Returns whether recording
        is live — RunLogger reports the failure itself, so callers just carry on."""
        if not self.logger.start():
            return False
        self.logger.write_settings(self._settings_snapshot(command))
        mode = "compensated" if self._run_compensated else "uncompensated"
        self.logger.write_event("RUN_START", f"T={command.ambient_temp} mat_dens={command.mat_dens} z_ratio={command.z_ratio} mode={mode}")
        return True

    def handle_command(self, command: WorkerCommand):

        if self.state == WorkerState.LOCKING and isinstance(command, self._DEFER_WHILE_LOCKING):
            self._deferred.append(command)
            return

        # ============================
        # Control commands
        # ============================

        if isinstance(command, StartupPLLCommand):
            self._start_lock(command.start_freq_mass, command.start_freq_temp)

        # Capacitor adjustment (open-loop tones for nulling the trim cap)
        elif isinstance(command, StartCapAdjustCommand) and self.state == WorkerState.IDLE:
            self._loops_aimed = False  # open-loop tones, and standby after them
            self.qcm.startCapAdjust(command.freq_mass, command.freq_temp)
            self._set_state(WorkerState.CAP_ADJUST)
        elif isinstance(command, StopCapAdjustCommand) and self.state == WorkerState.CAP_ADJUST:
            self.qcm.standby(1)
            self.qcm.standby(2)
            self._set_state(WorkerState.IDLE)

        # Start measurement
        elif isinstance(command, StartMeasurementCommand) and self.state == WorkerState.IDLE:
            self.qcm.setMeasurementReference(T=command.ambient_temp, mat_dens=command.mat_dens,
                                             z_ratio=command.z_ratio, compensated=self.compensated)
            self._run_compensated = self.compensated
            self._run_command = command
            self._set_target_reached(False)  # a fresh run starts below its target again
            self._temp_held = False          # nothing to hold over from the previous run
            self._comp_ok = True
            self._start_run_log(command)
            self._set_state(WorkerState.MEASURING)

        # Recording was disabled (usually a full disk) and the operator has since
        # freed space — pick the log back up for the rest of the run.
        elif isinstance(command, RetryRunLogCommand):
            if self.state == WorkerState.MEASURING and self._run_command and not self.logger.active:
                if self._start_run_log(self._run_command):
                    self.logger.write_event("RECORDING_RESUMED", "log re-opened mid-run; earlier points are lost")

        # Stop measurement
        elif isinstance(command, StopMeasurementCommand) and self._in_run():
            # Auto-update start frequencies to current locked frequencies (rounded
            # to 100 Hz) — if a lock attempt aimed the loops; see _loops_aimed.
            if (self.state == WorkerState.MEASURING and self._loops_aimed
                    and all(self.qcm.getLockDetect(i) for i in self._lock_active)):
                freq_mass, freq_temp = self._held_lock_freqs()
                self.event_queue.put(StartFreqAutoUpdatedEvent(
                    freq_mass=freq_mass, freq_temp=freq_temp, source="run_stop"))
            self.logger.write_event("RUN_STOP")
            self.logger.stop()
            if self._lock is not None:
                # Stopped mid re-lock: the run ends now, the attempt carries on
                # and finishes in IDLE.
                self._lock.resume = False
            else:
                self._set_state(WorkerState.IDLE)

        # Sweep
        elif isinstance(command, StartSweepCommand) and self.state == WorkerState.IDLE:
            self._loops_aimed = False  # the sweep leaves both oscillators open-loop
            self._set_state(WorkerState.SWEEPING)
            self._refresh_leds(False, False, time.time())  # loops open for the whole sweep
            self._run_sweep(command)
            self._set_state(WorkerState.IDLE)

        # ============================
        # Setting commands
        # ============================    
            
        elif isinstance(command, SetFrequencyCommand):
            self.qcm.setFreq(command.oscillator_idx, command.frequency)
        elif isinstance(command, SetIntegratorGainCommand):
            self.qcm.setOscConfig(command.oscillator_idx, int_gain=command.gain)
        elif isinstance(command, SetLPFFreqCommand):
            self.qcm.setOscConfig(command.oscillator_idx, lpf_freq=command.freq)
        elif isinstance(command, SetAGCCommand):
            self.qcm.setAGC(command.oscillator_idx, enabled=command.enabled,
                            manual_gain=command.manual_gain, target_amp=command.target_amp)
        elif isinstance(command, SetInvertedCommand):
            self.qcm.setOscConfig(command.oscillator_idx, inverted=command.inverted)
        elif isinstance(command, SetOutputModeCommand):
            self.qcm.setOutputMode(command.mode.value)
        elif isinstance(command, SetLockDetectCommand):
            self.qcm.setLockDetect(command.phase_tolerance, command.phase_std)
        elif isinstance(command, SetAutoRelockCommand):
            self.auto_relock = bool(command.enabled)
            self._lock_lost_since = None  # drop any in-flight timer so toggling can't fire a stale re-lock
        elif isinstance(command, SetTempHoldCommand):
            self.temp_hold = bool(command.enabled)
        elif isinstance(command, SetMeasurementModeCommand):
            self.compensated = bool(command.compensated)
            if self._in_run() and self.compensated != self._run_compensated:
                print(f"[QCM] Measurement mode set to "
                      f"{'compensated' if self.compensated else 'uncompensated'} — "
                      f"applies from the next run; this one stays as it started")
        elif isinstance(command, SetFreqTrackingCommand):
            self.freq_tracking = bool(command.enabled)
            # A sub-second interval would poll the frequency registers as fast as
            # the acquisition loop itself for no benefit: the resonances move over
            # minutes, not milliseconds.
            self.freq_track_interval = max(1.0, float(command.interval))
        elif isinstance(command, SetTargetThicknessCommand):
            self.target_thickness = max(0.0, float(command.target))
            # Raising the target mid-run means the film is below it again, so the
            # flag has to drop; the next crossing then reports honestly.
            if self._target_reached and (self.target_thickness == 0.0 or
                                         self.target_thickness > self._last_thickness):
                self._set_target_reached(False, self._last_thickness)
        elif isinstance(command, SetStorageReclaimCommand):
            self.logger.set_reclaim(command.prune_old_runs, command.trim_current_run)
        elif isinstance(command, SetSensorParamsCommand):
            self.qcm.setSensorParams(command.mass_sensitivity, command.sens_area, command.freq_virgin, command.tooling_ratio)
        elif isinstance(command, SetCoefficientsCommand):
            self.qcm.setCoefficients(
                command.fM_0, command.fM_1, command.fM_2, command.fM_3,
                command.fT_0, command.fT_1, command.fT_2, command.fT_3,
            )
        else:
            raise ValueError(f"Unknown command type: {type(command)}")
        
        
        
    def _run_sweep(self, command: StartSweepCommand):
        self.qcm.standby(1)
        self.qcm.standby(2)
        # make sure the LPF cutoff is set to the default for sweeps to keep things consistent
        self.qcm.setLPFFreq(command.oscillator_idx, self.qcm.LPF_FREQ)
        n_points = int(math.floor((command.stop_freq - command.start_freq) / command.step_size)) + 1
        # Record the sweep to disk as it is taken, so it survives a WS drop and
        # can be downloaded or re-plotted later. An abort still yields the file
        # written so far — a partial sweep is worth keeping.
        self.sweep_logger.start({
            "oscillator_idx": command.oscillator_idx,
            "start_freq":     command.start_freq,
            "stop_freq":      command.stop_freq,
            "center_freq":    (command.start_freq + command.stop_freq) / 2,
            "span":           command.stop_freq - command.start_freq,
            "step_size":      command.step_size,
            "settle_time":    command.settle_time,
            "n_points":       n_points,
            "lpf_freq":       self.qcm.LPF_FREQ,
        })
        for i in range(n_points):
            # Check for abort between points
            try:
                cmd = self.command_queue.get_nowait()
                if isinstance(cmd, AbortSweepCommand):
                    self.sweep_logger.write_event("SWEEP_ABORTED", f"stopped after {i} of {n_points} points")
                    self.event_queue.put(SweepCompleteEvent(name=self.sweep_logger.stop()))
                    return
            except queue.Empty:
                pass

            freq = command.start_freq + i * command.step_size
            self.qcm.setFreq(command.oscillator_idx, freq)
            self.qcm.reset() # resets the PLL integratos, making sure the frequency is the desired one.
            time.sleep(command.settle_time)
            amplitude = self.qcm.getMag(command.oscillator_idx)
            phase = self.qcm.getPhase(command.oscillator_idx)
            self.sweep_logger.write_point(freq, amplitude, phase, time.time())
            self.event_queue.put(SweepPointEvent(frequency=freq, amplitude=amplitude, phase=phase))
        self.event_queue.put(SweepCompleteEvent(name=self.sweep_logger.stop()))

    def _track_lock_freqs(self, lock_mass: bool, lock_temp: bool, now: float):
        """While both modes are locked, keep the re-lock start frequencies on the
        frequencies actually being held.

        The stored crystal frequencies are where the resonances were when the
        profile was written. Film loading pulls the mass mode down by kHz over a
        deposition and temperature moves both, so by the middle of a long run the
        stored values can sit outside the PLL capture window (+/- WINDOW_SIZE/2)
        of where the resonance now is — and a re-lock aimed there fails however
        healthy the crystal is. Following the lock while it is good means a lost
        lock is re-acquired from the last place it was known to be.

        Only sampled while locked, and only once a lock attempt has aimed the
        loops (see _loops_aimed): from power-up until the first lock the NCOs
        run from 0 Hz, and during and after a sweep or a capacitor adjustment
        they sit open-loop. The phase readout there can be quiet enough to pass
        the lock test at a frequency that is not a resonance at all.

        This does not touch the crystal profile on disk. The profile's start
        frequencies describe the crystal as calibrated; the operator saves a new
        set deliberately (the run-stop update prompts for exactly that).

        Only the modes the last lock locked are followed; a parked one keeps its
        start frequency.
        """
        if not (self.freq_tracking and self._loops_aimed):
            return
        if self.state not in (WorkerState.MEASURING, WorkerState.IDLE):
            return
        locks = {1: lock_mass, 2: lock_temp}
        if not (self._lock_active and all(locks[i] for i in self._lock_active)):
            return
        if now - self._last_freq_track < self.freq_track_interval:
            return
        self._last_freq_track = now
        # Rounded to 100 Hz: the capture window is thousands of Hz wide, so this
        # is far finer than a re-lock needs, and it keeps the value readable in
        # the settings field it lands in.
        freqs = self._held_lock_freqs()
        if freqs == self._lock_freqs:
            return  # nothing moved — no event, no UI churn
        self._lock_freqs = freqs
        self.event_queue.put(StartFreqAutoUpdatedEvent(
            freq_mass=freqs[0], freq_temp=freqs[1], source="tracking"))

    def _update_agc(self, data, lock_mass: bool, lock_temp: bool):
        """Advance the drive-gain servo by one cycle per channel, using the
        amplitudes this cycle's telemetry has already read.

        Gated on the worker state for the same reason _track_lock_freqs is:
        during a sweep or a capacitor adjustment the oscillators are parked
        open-loop, where the phase can sit quiet enough to pass the lock test at
        a frequency that is not a resonance at all. Those states want a fixed,
        known drive and set it themselves — the servo must not walk it.
        """
        if self.state not in (WorkerState.MEASURING, WorkerState.IDLE):
            return
        for osc, amp, locked in ((1, data.amp_mass, lock_mass),
                                 (2, data.amp_temp, lock_temp)):
            self.qcm.updateAGC(osc, amp, locked)
            self._note_agc_rail(osc)

    def _note_agc_rail(self, osc: int):
        """Report the edges of a railed AGC, once each.

        A channel sitting at the end of its gain range with the amplitude still
        off target is not a fault worth stopping a run for — the loop is locked
        and the frequency is good — but the drive has stopped holding the
        amplitude anywhere near where it was asked to, which is the one thing the
        AGC was there to do. Said once at each edge, it cannot pass silently and
        cannot flood the log either."""
        railed = self.qcm.getAGCRailed(osc)
        if railed == self._agc_railed[osc]:
            return
        self._agc_railed[osc] = railed
        gain = self.qcm.getOutputGain(osc)
        where = "?" if gain is None else f"{gain:.4f}"
        if railed:
            print(f"[QCM] AGC osc {osc} out of range at gain {where} — "
                  f"the target amplitude cannot be reached")
            self.logger.write_event(
                "AGC_RAIL", f"osc {osc} at drive gain limit {where}; target amplitude unreachable")
        else:
            self.logger.write_event("AGC_RAIL_END", f"osc {osc} back in range at drive gain {where}")

    def _note_temp_hold(self, holding: bool, data):
        """Record the edges of a temperature hold in the run log, once each.

        Every measurement row already carries lock_temp, so the interval is
        recoverable from the data either way; these rows are here so that reading
        the log top to bottom says plainly where the temperature stopped being a
        measurement and started being a carried-over value."""
        if holding == self._temp_held:
            return
        self._temp_held = holding
        # calculated_temp is None when the compensation itself failed, which is a
        # separate fault with its own message — don't format it as a number.
        T = data.calculated_temp
        held = f"{T:.4f}" if isinstance(T, (int, float)) and math.isfinite(T) else "?"
        if holding:
            print(f"[QCM] Temp-mode lock lost — holding T = {held} degC; "
                  f"thickness output continues from the mass mode")
            self.logger.write_event(
                "TEMP_HOLD",
                f"temp-mode lock lost; holding T={held} C, compensated thickness "
                f"still tracking the mass mode; no re-lock attempted")
        else:
            self.logger.write_event("TEMP_HOLD_END", "temp-mode lock back; solving T from fT again")

    def _check_target(self, data):
        """Raise the target-thickness flag the first time the compensated
        thickness reaches the target. The run is left running on purpose — the
        operator (or the PLC reading the flag) decides what happens next, and an
        unattended stop on a mis-set threshold would end the deposition early.

        Only the upward crossing counts: thickness dips back below the target
        (noise, a re-lock) must not retract a flag the PLC has already acted on.
        """
        thickness = data.calculated_thickness
        if thickness is None or not math.isfinite(thickness):
            return
        self._last_thickness = thickness
        if self.target_thickness <= 0 or self._target_reached:
            return
        if thickness >= self.target_thickness:
            print(f"[QCM] Target thickness reached: {thickness:.3f} nm >= {self.target_thickness:.3f} nm")
            self.logger.write_event("TARGET_REACHED", f"{thickness:.4f} nm >= target {self.target_thickness:.4f} nm")
            self._set_target_reached(True, thickness)

    def update(self):
        # Advance the phase-error window by exactly one sample per channel per
        # cycle. This is the only place that samples it during normal running, so
        # the window length translates directly into a time constant.
        lock_mass, quality_mass = self.qcm.sampleLock(1)
        lock_temp, quality_temp = self.qcm.sampleLock(2)

        # Emit lock status on change (immediate UI feedback) or as a periodic
        # heartbeat — not every loop iteration, which floods the WS clients.
        now = time.time()
        parked_mass, parked_temp = self.qcm.isParked(1), self.qcm.isParked(2)
        status = (lock_mass, lock_temp, parked_mass, parked_temp)
        if status != self._last_lock_status or now - self._last_lock_emit >= self.LOCK_STATUS_HEARTBEAT:
            self.event_queue.put(LockStatusEvent(
                lock_mass=lock_mass, lock_temp=lock_temp,
                quality_mass=quality_mass, quality_temp=quality_temp,
                parked_mass=parked_mass, parked_temp=parked_temp))
            self._last_lock_status = status
            self._last_lock_emit = now

        # Keep the re-lock start frequencies on the frequencies being held, so a
        # lost lock is re-acquired from where the resonances are now.
        self._track_lock_freqs(lock_mass, lock_temp, now)

        # A lock attempt runs alongside the acquisition rather than instead of
        # it: it advances here, once per cycle, and the readings below keep
        # streaming while the loops pull in. It may end this cycle, back in
        # MEASURING or IDLE.
        if self.state == WorkerState.LOCKING:
            self._advance_lock(now)

        if self.state == WorkerState.CAP_ADJUST:
            # Live amplitude feedback while adjusting the trim capacitor. The
            # drive gain goes with it: the amplitude is proportional to it, so a
            # bare reading cannot be judged against any fixed scale once the
            # drive is a setting rather than a constant.
            self.event_queue.put(CapAdjustEvent(
                amp_mass=self.qcm.getMag(1), amp_temp=self.qcm.getMag(2),
                gain_mass=self.qcm.getOutputGain(1), gain_temp=self.qcm.getOutputGain(2)))
        elif self.state == WorkerState.MEASURING:
            self._measure(lock_mass, lock_temp, now)
        else:
            # Not measuring — idle, or locking: push the raw readings anyway.
            # Frequencies, amplitudes, phases and lock quality need neither a
            # measurement reference nor a calibration, so there is no reason for
            # the monitor to sit blank while the operator is locking up or
            # judging a lock before starting a run.
            #
            # A re-lock in the middle of a run lands here too. Its loops are
            # being walked across the capture window, so no thickness from them
            # means anything: the run log gets RELOCK_START / RELOCK rows around
            # the gap instead, and the target cannot be tripped by the walk.
            telemetry = self.qcm.getTelemetry()
            # The AGC runs while idle too. A run is started on a lock that is
            # already up, so the drive has to have settled *before* the operator
            # presses start — otherwise every run would open with the servo
            # still hunting.
            self._update_agc(telemetry, lock_mass, lock_temp)
            self.event_queue.put(TelemetryEvent(data=telemetry))

        self._refresh_leds(lock_mass, lock_temp, now)

    def _measure(self, lock_mass: bool, lock_temp: bool, now: float):
        """One measurement cycle of a run."""
        compensated = self._run_compensated
        # A lost temp-mode lock leaves fT pointing at nothing in particular.
        # With the hold on, the last solved temperature is carried forward so
        # that only the thermal correction freezes and the compensated thickness
        # keeps following the film. With the hold off the operator has asked for
        # the live solve instead, and that is what they get: the temperature
        # comes out of whatever fT currently reads, with the lock flag and the
        # lock quality beside it to say how far to trust it. Either way a sample
        # is produced every cycle.
        #
        # The hold engages on the newest sample as well as on the lock flag. The
        # flag is judged over a window and drops a few samples into a loss, and
        # the temperatures solved in between — from an fT already sliding off
        # resonance — were what the hold then carried forward. One sample's
        # phase error says straight away that fT has left the resonance, so the
        # temperature held is the last one solved on it.
        hold_temp = compensated and self.temp_hold and not (lock_temp and self.qcm.isPhaseTrusted(2))
        data = self.qcm.getMeasurement(hold_temp=hold_temp)
        thickness = data.calculated_thickness
        self._comp_ok = thickness is not None and math.isfinite(thickness)
        self.logger.write_measurement(data)  # durable on-disk record (WS-independent)
        if compensated:
            self._note_temp_hold(hold_temp, data)
        self._update_agc(data, lock_mass, lock_temp)
        self._check_target(data)
        self.event_queue.put(MeasurementEvent(data=data))
        # Keep each PLL capture window centred on its current frequency — but
        # only for a channel that is locked. An unlocked loop's readout is not a
        # resonance, and following it walks the window away from where the
        # resonance actually is, which is precisely what the next re-lock
        # attempt has to start from. An unlocked channel holds its last good
        # centre instead. (A parked channel is never locked.)
        self.qcm.moveWindow(
            data.freq_mass_mode if lock_mass else None,
            data.freq_temp_mode if lock_temp else None,
        )

        # Automatic re-lock, and only for a lost *mass* mode. That is the mode
        # the thickness is measured from, and nothing in software can retrieve
        # it once it is off resonance.
        #
        # A temp-only loss is left alone whichever way the hold is set. There is
        # one global reset register, so a lock attempt re-parks *both* NCOs at
        # pre-lock gain: it drags the mass loop through a re-acquisition it did
        # not need and interrupts the thickness output while it runs, and when
        # lock detection is marginal rather than the lock genuinely being gone,
        # it does that over and over while breaking the very loops it is trying
        # to rescue. The temp loop left alone keeps running with its capture
        # window where the lock was last good (see moveWindow), which is the
        # best place for it to come back from on its own. GET LOCK re-acquires
        # both on demand.
        if lock_mass or 1 not in self._lock_active:
            self._lock_lost_since = None
        elif self.auto_relock and self._lock_freqs is not None:
            if self._lock_lost_since is None:
                self._lock_lost_since = now
            elif now - self._lock_lost_since >= self.AUTO_RELOCK_AFTER:
                print(f"[QCM] Mass-mode lock lost > {self.AUTO_RELOCK_AFTER:g} s — attempting automatic re-lock")
                self._start_lock(*self._lock_freqs)

    def _refresh_leds(self, lock_mass: bool, lock_temp: bool, now: float):
        """Front-panel LEDs (see domain/status_leds.py).

        LEDs 2 and 3 show each mode's lock while the loops are closed, but not
        through a sweep or a capacitor adjustment, which park them open-loop:
        a quiet phase there can pass the lock test at a frequency that is no
        resonance at all. LED 1 flashes while a run has a problem worth walking
        over to the instrument for."""
        closed = self.state in (WorkerState.IDLE, WorkerState.MEASURING, WorkerState.LOCKING)
        problem = self._in_run() and (
            self.state == WorkerState.LOCKING                 # re-locking mid-run
            or not lock_mass                                 # thickness off resonance
            or (self._run_compensated and not lock_temp)     # temperature held / unreliable
            or not self.logger.active                        # run is not being recorded
            or not self._comp_ok)                            # no thickness came out
        self.qcm.leds.update(lock_mass=closed and lock_mass, lock_temp=closed and lock_temp,
                             problem=problem, now=now)
