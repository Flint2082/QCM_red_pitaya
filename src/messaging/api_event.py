from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from domain.measurement import MeasurementData

@dataclass(kw_only=True)
class ApiEvent:
    # timestamp: float = field(default_factory=time.time)
    pass

@dataclass
class ErrorEvent(ApiEvent):
    message: str

@dataclass
class StateEvent(ApiEvent):
    state: str

@dataclass
class SweepPointEvent(ApiEvent):
    frequency: float
    amplitude: float
    phase: float

@dataclass
class SweepCompleteEvent(ApiEvent):
    pass

@dataclass
class MeasurementEvent(ApiEvent):
    data: MeasurementData
    
@dataclass
class LockFailedEvent(ApiEvent):
    pass

@dataclass
class LockStatusEvent(ApiEvent):
    lock_mass: bool
    lock_temp: bool

@dataclass
class StartFreqAutoUpdatedEvent(ApiEvent):
    freq_mass: float
    freq_temp: float

@dataclass
class SystemStatusEvent(ApiEvent):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool

@dataclass
class OpcStatusEvent(ApiEvent):
    connected: bool
    ambient_temp:    float | None = None
    start_freq_mass: float | None = None
    start_freq_temp: float | None = None
    density:         float | None = None
    z_ratio:         float | None = None