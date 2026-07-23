from enum import Enum, auto

class WorkerState(Enum):
    IDLE = auto()
    LOCKING = auto()
    MEASURING = auto()
    SWEEPING = auto()
    CALIBRATING = auto()
    CAP_ADJUST = auto()
    
class OutputMode(Enum):
    DELTA = 0
    MOCK_SINE = 1
    MODE_1_FREQ_FINE = 2
    MODE_1_FREQ_COARSE = 3
    MODE_1_MULTIPLIER = 4
    MODE_1_LPF_MAG = 5
    MODE_1_LPF_PHASE = 6
    MODE_2_FREQ_FINE = 7
    MODE_2_FREQ_COARSE = 8
    MODE_2_MULTIPLIER = 9
    MODE_2_LPF_MAG = 10
    MODE_2_LPF_PHASE = 11
    
    