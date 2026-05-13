from enum import Enum, auto

class WorkerState(Enum):
    IDLE = auto()
    MEASURING = auto()
    SWEEPING = auto()
    CALIBRATING = auto()
    
class OutputMode(Enum):
    DELTA = 0
    MODE_1_FREQ_FINE = 1
    MODE_1_FREQ_COARSE = 2
    MODE_1_MULTIPLIER = 3
    MODE_1_I = 4
    MODE_1_Q = 5
    MODE_2_FREQ_FINE = 6
    MODE_2_FREQ_COARSE = 7 
    MODE_2_MULTIPLIER = 8
    MODE_2_I = 9
    MODE_2_Q = 10
    
    