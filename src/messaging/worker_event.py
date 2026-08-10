from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from domain.measurement import MeasurementData
from messaging.defines import WorkerState

@dataclass(kw_only=True)
class Event:
    # timestamp: float = field(default_factory=time.time)
    pass

@dataclass
class ErrorEvent(Event):
    message: str

@dataclass
class StateEvent(Event):
    state: WorkerState

@dataclass
class SweepPointEvent(Event):
    frequency: float
    amplitude: float
    phase: float

@dataclass
class SweepCompleteEvent(Event):
    name: str | None = None  # CSV the sweep was recorded to, None = not recorded

@dataclass
class MeasurementEvent(Event):
    data: MeasurementData
    
@dataclass
class LockFailedEvent(Event):
    pass

@dataclass
class LockStatusEvent(Event):
    lock_mass: bool
    lock_temp: bool

@dataclass
class CapAdjustEvent(Event):
    amp_mass: float
    amp_temp: float

@dataclass
class StartFreqAutoUpdatedEvent(Event):
    freq_mass: float
    freq_temp: float

@dataclass
class LockAmpAutoUpdatedEvent(Event):
    amp_threshold: float  # auto-calibrated from the end-of-run amplitudes

@dataclass
class RunLogStartedEvent(Event):
    name: str  # CSV this run is being recorded to, on the Pitaya

@dataclass
class RunLogFailedEvent(Event):
    reason: str  # why the run is not being recorded (disk full, write error, ...)

@dataclass
class TargetReachedEvent(Event):
    """Target-thickness flag. Emitted when the target is crossed (reached=True)
    and again when it is cleared at the start of a run or on a target change
    (reached=False), so consumers never have to infer the flag from state."""
    reached: bool
    thickness: float  # compensated thickness at the moment of the transition
    target: float     # target that was in force (0 = target disabled)

@dataclass
class SystemStatusEvent(Event):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool