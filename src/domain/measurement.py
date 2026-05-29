from dataclasses import dataclass


@dataclass
class MeasurementData:
    timestamp: float
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
    lock_mass: bool
    lock_temp: bool
