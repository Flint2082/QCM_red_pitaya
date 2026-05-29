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
    pass

@dataclass
class StartMeasurementCommand(ApiCommand):
    ambient_temp: float

@dataclass
class StopMeasurementCommand(ApiCommand):
    pass

@dataclass
class StartSweepCommand(ApiCommand):
    start_freq: float
    stop_freq: float
    step_size: float
    settle_time: float

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
class SetIQGainCommand(ApiCommand):
    oscillator_idx: int
    gain: float
    
@dataclass
class SetOutputModeCommand(ApiCommand):
    oscillator_idx: int
    mode: OutputMode