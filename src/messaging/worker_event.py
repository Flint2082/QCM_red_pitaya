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
    uncompensated_mass: float
    calculated_mass: float
    calculated_temp: float
    compensated_freq: float
    
    