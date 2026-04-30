from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


# =========================================================
# Base command types
# =========================================================

class CommandSource(Enum):
    REST_API = auto()
    OPCUA = auto()
    INTERNAL = auto()


@dataclass
class Command:
    source: CommandSource


# =========================================================
# QCM control commands
# =========================================================

@dataclass
class StartSweepWorkerCommand(Command):
    start_frequency: float
    stop_frequency: float
    step_size: float
    dwell_time_ms: int


@dataclass
class StopSweepWorkerCommand(Command):
    pass


@dataclass
class StartMeasurementWorkerCommand(Command):
    pass

@dataclass
class StopMeasurementWorkerCommand(Command):
    pass


# =========================================================
# Calibration commands
# =========================================================

@dataclass
class CalibrateCommand(Command):

    reference_frequency: Optional[float] = None


# =========================================================
# System commands
# =========================================================

@dataclass
class ShutdownCommand(Command):

    pass