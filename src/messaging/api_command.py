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
class SetProportionalGainCommand(ApiCommand):
    oscillator_idx: int
    gain: float

@dataclass
class SetInvertedCommand(ApiCommand):
    oscillator_idx: int
    inverted: bool

@dataclass
class SetPhaseDetectCommand(ApiCommand):
    oscillator_idx: int
    mode: int  # mult_sel: phase-detector type (0 = ATAN, 1 = multiplier)

@dataclass
class SetLPFFreqCommand(ApiCommand):
    oscillator_idx: int
    freq: float  # LPF cutoff frequency in Hz
    
@dataclass
class SetOutputModeCommand(ApiCommand):
    oscillator_idx: int
    mode: OutputMode

@dataclass
class SetLockDetectCommand(ApiCommand):
    amp_threshold: float
    phase_tolerance: float

@dataclass
class SetAutoRelockCommand(ApiCommand):
    enabled: bool  # re-acquire automatically when lock is lost mid-measurement

@dataclass
class SetSensorParamsCommand(ApiCommand):
    mass_sensitivity: float
    sens_area: float
    freq_virgin: float = 0.0  # Hz — pristine crystal frequency, 0 = unset

@dataclass
class StartCapAdjustCommand(ApiCommand):
    freq_mass: float
    freq_temp: float

@dataclass
class StopCapAdjustCommand(ApiCommand):
    pass