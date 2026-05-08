from dataclasses import dataclass

@dataclass
class WorkerCommand:
    pass

# =========================================== 
# Control commands
# ===========================================

@dataclass
class StartMeasurementCommand(WorkerCommand):
    pass

@dataclass
class StopMeasurementCommand(WorkerCommand):
    pass

@dataclass
class StartSweepCommand(WorkerCommand):
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
    
