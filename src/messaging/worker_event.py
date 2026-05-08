from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

@dataclass
class Event:
    pass

@dataclass
class ErrorEvent(Event):
    message: str    
    
@dataclass
class MeasurementEvent(Event):
    freq_mass_mode: float 
    freq_diss_mode: float
    

