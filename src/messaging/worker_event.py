from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
from datetime import datetime, time

@dataclass(kw_only=True)
class Event:
    # timestamp: float = field(default_factory=time.time)
    pass

@dataclass
class ErrorEvent(Event):
    message: str    
    
    
@dataclass
class MeasurementEvent(Event):
    freq_mass_mode: float
    freq_temp_mode: float
    uncompensated_thickness: float
    calculated_thickness: float
    calculated_temp: float
    compensated_freq: float
    amp_mass: float
    phase_mass: float
    amp_temp: float
    phase_temp: float
    
@dataclass
class SystemStatusEvent(Event):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool