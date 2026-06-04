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
    pass

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
class StartFreqAutoUpdatedEvent(Event):
    freq_mass: float
    freq_temp: float

@dataclass
class SystemStatusEvent(Event):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool