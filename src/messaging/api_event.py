from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from domain.measurement import MeasurementData, TelemetryData

@dataclass(kw_only=True)
class ApiEvent:
    # timestamp: float = field(default_factory=time.time)
    pass

@dataclass
class ErrorEvent(ApiEvent):
    message: str

@dataclass
class StateEvent(ApiEvent):
    state: str

@dataclass
class SweepPointEvent(ApiEvent):
    frequency: float
    amplitude: float
    phase: float

@dataclass
class SweepCompleteEvent(ApiEvent):
    name: str | None = None  # CSV the sweep was recorded to, None = not recorded

@dataclass
class MeasurementEvent(ApiEvent):
    data: MeasurementData

@dataclass
class TelemetryEvent(ApiEvent):
    """Raw hardware readings, pushed while no run is in progress — see the
    worker-side event of the same name."""
    data: TelemetryData

@dataclass
class LockFailedEvent(ApiEvent):
    pass

@dataclass
class LogEvent(ApiEvent):
    level:     str    # INFO / WARNING / ERROR / DEBUG
    message:   str
    timestamp: float

@dataclass
class LockStatusEvent(ApiEvent):
    lock_mass: bool
    lock_temp: bool
    quality_mass: float | None  # phase-error std (rad); None = window not full yet
    quality_temp: float | None

@dataclass
class CapAdjustEvent(ApiEvent):
    amp_mass: float
    amp_temp: float

@dataclass
class StartFreqAutoUpdatedEvent(ApiEvent):
    freq_mass: float
    freq_temp: float

@dataclass
class RunLogStartedEvent(ApiEvent):
    name: str  # CSV this run is being recorded to, on the Pitaya

@dataclass
class RunLogFailedEvent(ApiEvent):
    reason: str  # why the run is not being recorded (disk full, write error, ...)

@dataclass
class TargetReachedEvent(ApiEvent):
    """Target-thickness flag: True when the target is crossed, False when it is
    cleared (run start / target change). Drives the UI popup, the REST flag and
    the OPC TargetReached node."""
    reached: bool
    thickness: float
    target: float

@dataclass
class SystemStatusEvent(ApiEvent):
    integrator_gain_mass_mode: float
    integrator_gain_temp_mode: float
    inv_mass_mode: bool
    inv_temp_mode: bool
    lock_status_mass_mode: bool
    lock_status_temp_mode: bool

@dataclass
class OpcStatusEvent(ApiEvent):
    connected: bool
    ambient_temp:    float | None = None
    start_freq_mass: float | None = None
    start_freq_temp: float | None = None
    density:         float | None = None
    z_ratio:         float | None = None