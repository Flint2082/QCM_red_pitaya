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

from domain.qcm_interface import QCMInterface
from domain.run_logger import RunLogger
from domain.sweep_logger import SweepLogger
from messaging.defines import WorkerState
from messaging.worker_event import *
from messaging.worker_command import *

class QCMWorker(threading.Thread):
    def __init__(self, qcm: QCMInterface, command_queue: queue.Queue, event_queue: queue.Queue,
                 version=None):
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
        self.logger = RunLogger(
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
        print("QCM worker stopped")

    def _set_state(self, new_state: WorkerState):
        self.state = new_state
        self.event_queue.put(StateEvent(state=new_state))

    def _acquire_lock(self, mass: float, temp: float):
        """Run the PLL startup around (mass, temp). If a measurement was running
        this re-locks without stopping it (the measurement reference is left
        untouched, so the deposition baseline and recorded data carry through);
        otherwise it returns to IDLE. Used by GET LOCK and by the automatic
        re-lock. Caches the frequencies for the auto re-lock to reuse."""
        self._lock_freqs = (mass, temp)
        resume_measuring = (self.state == WorkerState.MEASURING)
        self._set_state(WorkerState.LOCKING)
        locked = self.qcm.startupPLL(mass, temp)
        if not locked:
            self.event_queue.put(LockFailedEvent())
        if resume_measuring:
            self.logger.write_event("RELOCK", "lock re-established" if locked else "re-lock failed")
        self._lock_lost_since = None  # reset the loss timer after any attempt
        self._set_state(WorkerState.MEASURING if resume_measuring else WorkerState.IDLE)
        return locked

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
        self.logger.write_event("RUN_START", f"T={command.ambient_temp} mat_dens={command.mat_dens} z_ratio={command.z_ratio}")
        return True

    def handle_command(self, command: WorkerCommand):

        # ============================
        # Control commands
        # ============================

        if isinstance(command, StartupPLLCommand):
            self._acquire_lock(command.start_freq_mass, command.start_freq_temp)

        # Capacitor adjustment (open-loop tones for nulling the trim cap)
        elif isinstance(command, StartCapAdjustCommand) and self.state == WorkerState.IDLE:
            self.qcm.startCapAdjust(command.freq_mass, command.freq_temp)
            self._set_state(WorkerState.CAP_ADJUST)
        elif isinstance(command, StopCapAdjustCommand) and self.state == WorkerState.CAP_ADJUST:
            self.qcm.standby(1)
            self.qcm.standby(2)
            self._set_state(WorkerState.IDLE)

        # Start measurement
        elif isinstance(command, StartMeasurementCommand) and self.state == WorkerState.IDLE:
            self.qcm.setMeasurementReference(T=command.ambient_temp, mat_dens=command.mat_dens, z_ratio=command.z_ratio)
            self._run_command = command
            self._set_target_reached(False)  # a fresh run starts below its target again
            self._temp_held = False          # nothing to hold over from the previous run
            self._start_run_log(command)
            self._set_state(WorkerState.MEASURING)

        # Recording was disabled (usually a full disk) and the operator has since
        # freed space — pick the log back up for the rest of the run.
        elif isinstance(command, RetryRunLogCommand):
            if self.state == WorkerState.MEASURING and self._run_command and not self.logger.active:
                if self._start_run_log(self._run_command):
                    self.logger.write_event("RECORDING_RESUMED", "log re-opened mid-run; earlier points are lost")

        # Stop measurement
        elif isinstance(command, StopMeasurementCommand) and self.state == WorkerState.MEASURING:
            # Auto-update start frequencies to current locked frequencies (rounded to 1 kHz)
            lock_mass = self.qcm.getLockDetect(1)
            lock_temp = self.qcm.getLockDetect(2)
            if lock_mass and lock_temp:
                freq_mass = round(self.qcm.getFreq(1) / 100) * 100
                freq_temp = round(self.qcm.getFreq(2) / 100) * 100
                self.event_queue.put(StartFreqAutoUpdatedEvent(
                    freq_mass=freq_mass, freq_temp=freq_temp, source="run_stop"))
            self.logger.write_event("RUN_STOP")
            self.logger.stop()
            self._set_state(WorkerState.IDLE)

        # Sweep
        elif isinstance(command, StartSweepCommand) and self.state == WorkerState.IDLE:
            self._set_state(WorkerState.SWEEPING)
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

        Only sampled while locked, and only in states where the loops are closed:
        during a sweep or a capacitor adjustment the oscillators are parked
        open-loop, where the phase readout can sit quiet enough to pass the lock
        test at a frequency that is not a resonance at all.

        This does not touch the crystal profile on disk. The profile's start
        frequencies describe the crystal as calibrated; the operator saves a new
        set deliberately (the run-stop update prompts for exactly that).
        """
        if not self.freq_tracking:
            return
        if self.state not in (WorkerState.MEASURING, WorkerState.IDLE):
            return
        if not (lock_mass and lock_temp):
            return
        if now - self._last_freq_track < self.freq_track_interval:
            return
        self._last_freq_track = now
        # Rounded to 100 Hz: the capture window is thousands of Hz wide, so this
        # is far finer than a re-lock needs, and it keeps the value readable in
        # the settings field it lands in.
        freqs = (round(self.qcm.getFreq(1) / 100) * 100,
                 round(self.qcm.getFreq(2) / 100) * 100)
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
        status = (lock_mass, lock_temp)
        if status != self._last_lock_status or now - self._last_lock_emit >= self.LOCK_STATUS_HEARTBEAT:
            self.event_queue.put(LockStatusEvent(
                lock_mass=lock_mass, lock_temp=lock_temp,
                quality_mass=quality_mass, quality_temp=quality_temp))
            self._last_lock_status = status
            self._last_lock_emit = now

        # Keep the re-lock start frequencies on the frequencies being held, so a
        # lost lock is re-acquired from where the resonances are now.
        self._track_lock_freqs(lock_mass, lock_temp, now)

        # Live amplitude feedback while adjusting the trim capacitor. The drive
        # gain goes with it: the amplitude is proportional to it, so a bare
        # reading cannot be judged against any fixed scale once the drive is a
        # setting rather than a constant.
        if self.state == WorkerState.CAP_ADJUST:
            self.event_queue.put(CapAdjustEvent(
                amp_mass=self.qcm.getMag(1), amp_temp=self.qcm.getMag(2),
                gain_mass=self.qcm.getOutputGain(1), gain_temp=self.qcm.getOutputGain(2)))
            return

        # Perform measurement acquisition if in measuring state
        if self.state == WorkerState.MEASURING:
            # Measure first, decide about re-locking afterwards. A re-lock blocks
            # for as long as ten acquisition attempts, so sampling before that
            # decision is the difference between a run that keeps reporting
            # through a bad patch and one that goes silent for the whole of it —
            # and a cycle that ends in a re-lock still contributes its sample.
            #
            # A lost temp-mode lock leaves fT pointing at nothing in particular.
            # With the hold on, the last solved temperature is carried forward so
            # that only the thermal correction freezes and the compensated
            # thickness keeps following the film. With the hold off the operator
            # has asked for the live solve instead, and that is what they get:
            # the temperature comes out of whatever fT currently reads, with the
            # lock flag and the lock quality beside it to say how far to trust
            # it. Either way a sample is produced every cycle.
            hold_temp = self.temp_hold and not lock_temp
            data = self.qcm.getMeasurement(hold_temp=hold_temp)
            self.logger.write_measurement(data)  # durable on-disk record (WS-independent)
            self._note_temp_hold(hold_temp, data)
            self._update_agc(data, lock_mass, lock_temp)
            self._check_target(data)
            self.event_queue.put(MeasurementEvent(data=data))
            # Keep each PLL capture window centred on its current frequency — but
            # only for a channel that is locked. An unlocked loop's readout is not
            # a resonance, and following it walks the window away from where the
            # resonance actually is, which is precisely what the next re-lock
            # attempt has to start from. An unlocked channel holds its last good
            # centre instead.
            self.qcm.moveWindow(
                data.freq_mass_mode if lock_mass else None,
                data.freq_temp_mode if lock_temp else None,
            )

            # Automatic re-lock, and only for a lost *mass* mode. That is the
            # mode the thickness is measured from, and nothing in software can
            # retrieve it once it is off resonance.
            #
            # A temp-only loss is left alone whichever way the hold is set. There
            # is one global reset register, so startupPLL re-parks *both* NCOs at
            # pre-lock gain and blocks for up to ten attempts: it drags the mass
            # loop through a re-acquisition it did not need and stops the output
            # for seconds at a time, and when lock detection is marginal rather
            # than the lock genuinely being gone, it does that over and over
            # while breaking the very loops it is trying to rescue. The temp loop
            # left alone keeps running with its capture window where the lock was
            # last good (see moveWindow), which is the best place for it to come
            # back from on its own. GET LOCK re-acquires both on demand.
            if lock_mass:
                self._lock_lost_since = None
            elif self.auto_relock and self._lock_freqs is not None:
                if self._lock_lost_since is None:
                    self._lock_lost_since = now
                elif now - self._lock_lost_since >= self.AUTO_RELOCK_AFTER:
                    print("[QCM] Mass-mode lock lost > 1 s — attempting automatic re-lock")
                    self._acquire_lock(*self._lock_freqs)
            return

        # Not measuring: push the raw readings anyway. Frequencies, amplitudes,
        # phases and lock quality need neither a measurement reference nor a
        # calibration, so there is no reason for the monitor to sit blank while
        # the operator is locking up or judging a lock before starting a run.
        telemetry = self.qcm.getTelemetry()
        # The AGC runs while idle too. A run is started on a lock that is already
        # up, so the drive has to have settled *before* the operator presses
        # start — otherwise every run would open with the servo still hunting.
        self._update_agc(telemetry, lock_mass, lock_temp)
        self.event_queue.put(TelemetryEvent(data=telemetry))