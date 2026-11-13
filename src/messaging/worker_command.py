from dataclasses import dataclass
from messaging.defines import OutputMode

@dataclass
class WorkerCommand:
    pass

# =========================================== 
# Control commands
# ===========================================

@dataclass
class StartupPLLCommand(WorkerCommand):
    start_freq_mass: float
    start_freq_temp: float

@dataclass
class SetCoefficientsCommand(WorkerCommand):
    fM_0: float
    fM_1: float
    fM_2: float
    fM_3: float
    fT_0: float
    fT_1: float
    fT_2: float
    fT_3: float

@dataclass
class ReferenceParams(WorkerCommand):
    """What a thickness reference is taken with (QCMWorker._take_reference)."""
    ambient_temp: float
    mat_dens: float = 19320.0  # kg/m^3 — deposited film density
    z_ratio: float = 1.0       # quartz/film acoustic impedance ratio
    # "manual": T start is ambient_temp. An LTC2986 sensor key: T start is that
    # sensor's reading when the reference is taken, ambient_temp if it has none.
    # "crystal": the crystal's own absolute temperature from its two modes at
    # that moment, ambient_temp if they do not give one.
    t_start_source: str = "manual"

@dataclass
class StartMeasurementCommand(ReferenceParams):
    """Start logging: open the run log. The measurement itself runs whenever
    the mass mode is locked. zero takes a new reference first, with these
    parameters (the PLC's start); without it they are not used."""
    zero: bool = False

@dataclass
class ZeroCommand(ReferenceParams):
    """Take a new thickness reference: the thickness reads 0 from here."""
    pass

@dataclass
class SetReferenceParamsCommand(ReferenceParams):
    """The parameters an automatic reference (after a lock, before any ZERO)
    is taken with: the current measurement settings."""
    pass

@dataclass
class StopMeasurementCommand(WorkerCommand):
    pass

@dataclass
class RetryRunLogCommand(WorkerCommand):
    """Re-open the run log for a measurement that is running unrecorded, after
    the operator has freed disk space. Records the remainder of the run."""
    pass

@dataclass
class StartSweepCommand(WorkerCommand):
    oscillator_idx: int
    start_freq: float
    stop_freq: float
    step_size: float
    settle_time: float

@dataclass
class AbortSweepCommand(WorkerCommand):
    pass

# ===========================================
# Setting commands
# ===========================================

@dataclass
class SetFrequencyCommand(WorkerCommand):
    oscillator_idx: int
    frequency: float

@dataclass
class SetIntegratorGainCommand(WorkerCommand):
    oscillator_idx: int
    gain: float

@dataclass
class SetInvertedCommand(WorkerCommand):
    oscillator_idx: int
    inverted: bool

@dataclass
class SetLPFFreqCommand(WorkerCommand):
    oscillator_idx: int
    freq: float  # LPF cutoff frequency in Hz

@dataclass
class SetAGCCommand(WorkerCommand):
    """Drive gain for one oscillator. With `enabled` the gain is servoed to hold
    the demodulated amplitude at `target_amp` while that channel is locked;
    without it — the manual override — the drive stays at `manual_gain`, which is
    also what an unlocked channel gets either way."""
    oscillator_idx: int
    enabled: bool
    manual_gain: float  # output_gain_N while not servoing; 0..0.5 (1.0 = undivided drive)
    target_amp: float   # demodulated amplitude the AGC holds

@dataclass
class SetOutputModeCommand(WorkerCommand):
    # One analog output, one selector: the mode itself names the oscillator
    # (MODE_1_* / MODE_2_*), so there is no separate oscillator index.
    mode: OutputMode

@dataclass
class SetLockDetectCommand(WorkerCommand):
    phase_tolerance: float  # rad — max |mean phase error|
    phase_std: float        # rad — max phase-error std ("lock quality")

@dataclass
class SetAutoRelockCommand(WorkerCommand):
    enabled: bool  # re-acquire automatically when lock is lost mid-measurement

@dataclass
class SetMeasurementModeCommand(WorkerCommand):
    """Compensated (both modes locked, temperature solved from fT) or
    uncompensated (mass mode only; the temp oscillator is parked). Read at GET
    LOCK and at run start, so a run keeps the mode it was started in."""
    compensated: bool

@dataclass
class SetTempHoldCommand(WorkerCommand):
    """Hold the last calculated temperature while the temp-mode lock is lost,
    instead of solving the compensation from an untrustworthy fT."""
    enabled: bool

@dataclass
class SetFreqTrackingCommand(WorkerCommand):
    """Periodically re-point the lock start frequencies at the frequencies the
    PLLs are actually holding, so a re-lock starts from where the resonances
    are now rather than where they were when the crystal was activated."""
    enabled: bool
    interval: float  # seconds between updates

@dataclass
class SetStorageReclaimCommand(WorkerCommand):
    """What the run logger may delete when the data partition runs low. Both are
    permanent — a run CSV is the only copy of that run."""
    prune_old_runs: bool     # delete finished runs, oldest first
    trim_current_run: bool   # drop the oldest parts of the run in progress

@dataclass
class SetSensorParamsCommand(WorkerCommand):
    mass_sensitivity: float
    sens_area: float
    freq_virgin: float = 0.0  # Hz — pristine crystal frequency, 0 = unset
    tooling_ratio: float = 1.0  # proportional scaling of reported thickness

@dataclass
class SetAcquisitionCommand(WorkerCommand):
    """Acquisition timing (domain/acquisition.py). A new sample rate waits for a
    run in progress to end; the other two take effect at once."""
    sample_rate: float   # Hz the worker samples at
    publish_rate: float  # Hz the GUI and the PLC are sent samples at
    log_rate: float      # Hz the run log takes samples at; 0 = every sample

@dataclass
class SetRateWindowCommand(WorkerCommand):
    """Seconds of thickness the deposition rate is fitted over."""
    window: float

@dataclass
class SetTargetThicknessCommand(WorkerCommand):
    target: float  # nm of compensated thickness; 0 disables the target

@dataclass
class StartCapAdjustCommand(WorkerCommand):
    freq_mass: float
    freq_temp: float

@dataclass
class StopCapAdjustCommand(WorkerCommand):
    pass