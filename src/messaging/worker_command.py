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
class StartMeasurementCommand(WorkerCommand):
    ambient_temp: float

@dataclass
class StopMeasurementCommand(WorkerCommand):
    pass

@dataclass
class StartSweepCommand(WorkerCommand):
    oscillator_idx: int
    start_freq: float
    stop_freq: float
    step_size: float
    settle_time: float

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
class SetIQGainCommand(WorkerCommand):
    oscillator_idx: int
    gain: float
    
@dataclass
class SetOutputModeCommand(WorkerCommand):
    oscillator_idx: int
    mode: OutputMode