from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
from datetime import datetime, time

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
class SystemStatusEvent(ApiEvent):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool