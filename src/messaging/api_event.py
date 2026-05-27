from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
from datetime import datetime, time

@dataclass(kw_only=True)
class ApiEvent:
    # timestamp: float = field(default_factory=time.time)
    pass

@dataclass
class ErrorEvent(ApiEvent):
    message: str    
    
    
@dataclass
class MeasurementEvent(ApiEvent):
    freq_mass_mode: float 
    freq_temp_mode: float
    uncompensated_mass: float
    calculated_mass: float
    calculated_temp: float
    compensated_freq: float
    
@dataclass
class SystemStatusEvent(ApiEvent):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool