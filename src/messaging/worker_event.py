from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
from datetime import datetime, time

from domain.measurement import MeasurementData

@dataclass(kw_only=True)
class Event:
    # timestamp: float = field(default_factory=time.time)
    pass

@dataclass
class ErrorEvent(Event):
    message: str

@dataclass
class MeasurementEvent(Event):
    data: MeasurementData
    
@dataclass
class SystemStatusEvent(Event):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool