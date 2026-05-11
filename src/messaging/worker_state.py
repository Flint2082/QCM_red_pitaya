from enum import Enum, auto

class WorkerState(Enum):
    IDLE = auto()
    MEASURING = auto()
    SWEEPING = auto()
    CALIBRATING = auto()