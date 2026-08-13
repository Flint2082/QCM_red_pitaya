from dataclasses import dataclass


@dataclass
class TelemetryData:
    """Everything the hardware reports directly, with no temperature-compensation
    model in the path. Kept separate from MeasurementData so the monitor charts
    can stay live outside a run — before a measurement reference is set there is
    no compensation to run, but the frequencies, amplitudes, phases and lock
    state are all perfectly meaningful."""
    timestamp: float
    freq_mass_mode: float
    freq_temp_mode: float
    amp_mass: float
    phase_mass: float
    amp_temp: float
    phase_temp: float
    lock_mass: bool
    lock_temp: bool
    # Phase-error standard deviation (rad) over the lock window — "lock quality",
    # smaller is better. None until the window holds enough samples to judge.
    quality_mass: float | None
    quality_temp: float | None


@dataclass
class MeasurementData(TelemetryData):
    """A telemetry sample plus everything derived from it by the
    temperature-compensation algorithm."""
    uncompensated_thickness: float
    calculated_thickness: float
    calculated_temp: float
    compensated_freq: float
