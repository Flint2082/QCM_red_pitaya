from dataclasses import dataclass
from messaging.defines import OutputMode

@dataclass
class ApiCommand:
    pass

# =========================================== 
# Control commands
# ===========================================

@dataclass
class StartupPLLCommand(ApiCommand):
    start_freq_mass: float
    start_freq_temp: float

@dataclass
class SetCoefficientsCommand(ApiCommand):
    fM_0: float
    fM_1: float
    fM_2: float
    fM_3: float
    fT_0: float
    fT_1: float
    fT_2: float
    fT_3: float

@dataclass
class StartMeasurementCommand(ApiCommand):
    ambient_temp: float
    mat_dens: float = 19320.0  # kg/m^3 — deposited film density
    z_ratio: float = 1.0       # quartz/film acoustic impedance ratio

@dataclass
class StopMeasurementCommand(ApiCommand):
    pass

@dataclass
class RetryRunLogCommand(ApiCommand):
    """Re-open the run log for a measurement that is running unrecorded, after
    the operator has freed disk space."""
    pass

@dataclass
class StartSweepCommand(ApiCommand):
    oscillator_idx: int
    start_freq: float
    stop_freq: float
    step_size: float
    settle_time: float

@dataclass
class AbortSweepCommand(ApiCommand):
    pass

# ===========================================
# Setting commands
# ===========================================

@dataclass
class SetFrequencyCommand(ApiCommand):
    oscillator_idx: int
    frequency: float

@dataclass
class SetIntegratorGainCommand(ApiCommand):
    oscillator_idx: int
    gain: float

@dataclass
class SetInvertedCommand(ApiCommand):
    oscillator_idx: int
    inverted: bool

@dataclass
class SetLPFFreqCommand(ApiCommand):
    oscillator_idx: int
    freq: float  # LPF cutoff frequency in Hz

@dataclass
class SetAGCCommand(ApiCommand):
    """Drive gain for one oscillator. With `enabled` the gain is servoed to hold
    the demodulated amplitude at `target_amp` while that channel is locked;
    without it — the manual override — the drive stays at `manual_gain`, which is
    also what an unlocked channel gets either way."""
    oscillator_idx: int
    enabled: bool
    manual_gain: float  # output_gain_N while not servoing; 0..1.0, 1.0 = undivided
    target_amp: float   # demodulated amplitude the AGC holds

@dataclass
class SetOutputModeCommand(ApiCommand):
    # One analog output, one selector: the mode itself names the oscillator
    # (MODE_1_* / MODE_2_*), so there is no separate oscillator index.
    mode: OutputMode

@dataclass
class SetLockDetectCommand(ApiCommand):
    phase_tolerance: float  # rad — max |mean phase error|
    phase_std: float        # rad — max phase-error std ("lock quality")

@dataclass
class SetAutoRelockCommand(ApiCommand):
    enabled: bool  # re-acquire automatically when lock is lost mid-measurement

@dataclass
class SetTempHoldCommand(ApiCommand):
    """Hold the last calculated temperature while the temp-mode lock is lost,
    instead of solving the compensation from an untrustworthy fT."""
    enabled: bool

@dataclass
class SetFreqTrackingCommand(ApiCommand):
    """Periodically re-point the lock start frequencies at the frequencies the
    PLLs are actually holding, so a re-lock starts from where the resonances
    are now rather than where they were when the crystal was activated."""
    enabled: bool
    interval: float  # seconds between updates

@dataclass
class SetStorageReclaimCommand(ApiCommand):
    """What the run logger may delete when the data partition runs low. Both are
    permanent — a run CSV is the only copy of that run."""
    prune_old_runs: bool     # delete finished runs, oldest first
    trim_current_run: bool   # drop the oldest parts of the run in progress

@dataclass
class SetSensorParamsCommand(ApiCommand):
    mass_sensitivity: float
    sens_area: float
    freq_virgin: float = 0.0  # Hz — pristine crystal frequency, 0 = unset
    tooling_ratio: float = 1.0  # proportional scaling of reported thickness

@dataclass
class SetTargetThicknessCommand(ApiCommand):
    target: float  # nm of compensated thickness; 0 disables the target

@dataclass
class StartCapAdjustCommand(ApiCommand):
    freq_mass: float
    freq_temp: float

@dataclass
class StopCapAdjustCommand(ApiCommand):
    pass