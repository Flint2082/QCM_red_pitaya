"""The temperature sensors on the QCM_hardware_V2 board, read through its LTC2986.

Wiring, from Hardware/QCM_hardware_V2/QCM_hardware_V2.SchDoc (U3):

| LTC2986   | connected to                         | configured as                         |
|-----------|--------------------------------------|---------------------------------------|
| CH1, CH2  | R22 "R_SENSE"                        | sense resistor, on CH2                |
| CH2, CH3  | CN2 "RTD-": both wires of one end    | 4-wire PT100 on CH4: sensed across    |
| CH4, CH5  | CN3 "RTD+": both wires of the other  |   CH3-CH4, excited from CH5 to CH2    |
| CH6       | CN4 "TSic 501F" (GND, signal, +5 V)  | active analog sensor = cold junction  |
| CH7, CH8  | CN5 "THERMOCOUPLE" (CN5.1 -, CN5.2 +)| differential thermocouple, on CH8     |
| CH9, CH10 | CN6 "SPARE"                          | unassigned                            |

The chip is on the SPI pins of the Pitaya's E2 connector (J10 21-24), which
Linux exposes as /dev/spidev2.0. Its INTERRUPT pin is not wired to the Pitaya,
so conversions are polled for.

The values under "check against the board" are not in the schematic. Each one
scales or shapes a reading, so set them to what is actually fitted.
"""

from dataclasses import dataclass

from domain import ltc2986
from domain.spi import SpiDevice

SPI_DEVICE = "/dev/spidev2.0"
SPI_SPEED_HZ = 1_000_000   # the LTC2986 allows 2 MHz; a few bytes a second need nothing near it

# ---- check against the board ----
# R22, the sense resistor (the schematic only calls it R_SENSE). The PT100 reads
# in direct proportion to it: 1 % off here is nearly 3 °C off at room
# temperature, so put the measured value in.
RSENSE_OHMS = 1000.0
# Type of the thermocouple on CN5.
THERMOCOUPLE_TYPE = "K"
# PT100 excitation. 250 µA through 1 kΩ is 0.25 V, well inside the ADC's range;
# a larger R22 needs less (ltc2986.rtd() refuses a combination that cannot fit).
RTD_EXCITATION_UA = 250
# TSic 501F: 0-1 V output, linear over -10..+60 °C (T = 70 °C/V * V - 10 °C).
TSIC_501F_TABLE = ((0.0, -10.0), (500.0, 25.0), (1000.0, 60.0))   # (mV, °C)
MAINS_HZ = 50

CH_RSENSE = 2
CH_PT100 = 4
CH_COLD_JUNCTION = 6
CH_THERMOCOUPLE = 8


@dataclass(frozen=True)
class Sensor:
    key: str       # stable identifier, used by the API and the GUI
    name: str
    channel: int


# The order they are converted and reported in.
SENSORS = (
    Sensor("pt100",         "PT100",                     CH_PT100),
    Sensor("thermocouple",  f"Thermocouple ({THERMOCOUPLE_TYPE})", CH_THERMOCOUPLE),
    Sensor("cold_junction", "Cold junction (TSic 501F)", CH_COLD_JUNCTION),
)


def channel_assignments() -> dict[int, int]:
    """The assignment word for every channel, unused ones zero."""
    words = {ch: 0 for ch in range(1, ltc2986.NUM_CHANNELS + 1)}
    words[CH_RSENSE] = ltc2986.sense_resistor(RSENSE_OHMS)
    words[CH_PT100] = ltc2986.rtd(CH_RSENSE, RSENSE_OHMS, "PT-100", wires=4, rotate=True,
                                  excitation_ua=RTD_EXCITATION_UA, curve="european")
    words[CH_COLD_JUNCTION] = ltc2986.active_analog(0, len(TSIC_501F_TABLE))
    words[CH_THERMOCOUPLE] = ltc2986.thermocouple(THERMOCOUPLE_TYPE, CH_COLD_JUNCTION,
                                                  single_ended=False, open_circuit_ua=10)
    return words


class TemperatureSensors:
    """The board's LTC2986, configured for the wiring above."""

    def __init__(self, spi):
        self.chip = ltc2986.LTC2986(spi)
        self._spi = spi
        self._assignments = channel_assignments()
        self._table = ltc2986.active_analog_table(TSIC_501F_TABLE)

    @classmethod
    def open(cls, path: str = SPI_DEVICE) -> "TemperatureSensors":
        sensors = cls(SpiDevice(path, mode=0, speed_hz=SPI_SPEED_HZ))
        try:
            sensors.configure()
        except Exception:
            sensors.close()
            raise
        return sensors

    def configure(self):
        """Program the chip and read it all back. The chip keeps its
        configuration in RAM, so this is needed after every power-up. The
        read-back is what proves the bus works in both directions."""
        try:
            self.chip.wait_idle(self.chip.STARTUP_TIMEOUT)
        except TimeoutError as e:
            raise RuntimeError(f"no LTC2986 answering on {self._path()}: {e}") from None
        self.chip.set_global_config(rejection=MAINS_HZ)
        self.chip.write_table(0, self._table)
        for ch, word in self._assignments.items():
            self.chip.assign(ch, word)
        if not self.configured():
            raise RuntimeError(f"LTC2986 on {self._path()} reads back a different configuration "
                               f"than was written — check the SPI wiring and logic levels")

    def configured(self) -> bool:
        """Whether the chip still holds our configuration (it loses it on a
        power cycle)."""
        return all(self.chip.assignment(ch) == word for ch, word in self._assignments.items())

    def read_all(self) -> list[dict]:
        """Convert every sensor once. Takes most of a second."""
        out = []
        for s in SENSORS:
            r = self.chip.convert(s.channel)
            out.append({"key": s.key, "name": s.name, "channel": s.channel,
                        "temperature": r.temperature, "faults": r.faults})
        return out

    def _path(self) -> str:
        return getattr(self._spi, "path", "SPI")

    def close(self):
        self._spi.close()
