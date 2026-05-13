from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional
from datetime import datetime



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
    
    

@dataclass
class Event:
    timestamp: datetime = datetime.now()