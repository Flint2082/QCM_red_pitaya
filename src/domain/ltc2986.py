"""LTC2986 / LTC2986-1 multi-sensor temperature measurement IC, over SPI.

The chip does the whole measurement itself: excitation currents, the ADC, the
linearisation of each sensor type and cold-junction compensation. The host
describes what is wired to each of the 10 channels by writing a 32-bit
"channel assignment" word per channel, then asks for conversions and reads the
result back in degrees. Which board wiring this is used with lives in
domain/temperature_sensors.py; this module only knows the chip.

The encodings below come from the LTC2986 datasheet, cross-checked against
ADI's Linduino configuration constants and the Linux ltc2983 IIO driver, which
covers this chip.

SPI (mode 0): every transaction is an instruction byte, a 16-bit address and
data, all MSB first. Multi-byte values are big-endian.
"""

import time
from dataclasses import dataclass, field

_WRITE = 0x02
_READ  = 0x03

# ---- Memory map ----
COMMAND_STATUS = 0x000
RESULT_BASE    = 0x010   # conversion results, 4 bytes per channel
GLOBAL_CONFIG  = 0x0F0
ASSIGN_BASE    = 0x200   # channel assignment words, 4 bytes per channel
CUSTOM_TABLE   = 0x250   # custom sensor tables, 6 bytes per entry
CUSTOM_TABLE_END = 0x3CF

NUM_CHANNELS = 10

# Command/status register. Bits 7:6 read 01 when the chip is idle — straight
# after power-up and after every conversion — and bits 4:0 hold the channel.
_START       = 0x80
_STATE_MASK  = 0xC0
_IDLE        = 0x40

# Global configuration: mains rejection (bits 1:0) and °C (bit 2 clear).
REJECTION = {"50/60": 0x0, 60: 0x1, 50: 0x2}

# ---- Sensor types (assignment bits 31:27) ----
THERMOCOUPLE_TYPES = {"J": 1, "K": 2, "E": 3, "N": 4, "R": 5, "S": 6, "T": 7, "B": 8}
RTD_TYPES = {"PT-10": 10, "PT-50": 11, "PT-100": 12, "PT-200": 13, "PT-500": 14,
             "PT-1000": 15, "PT-1000 (0.00375)": 16, "NI-120": 17}
_SENSE_RESISTOR = 29
_ACTIVE_ANALOG  = 31

# RTD fields
_RTD_WIRES = {2: 0, 3: 1, 4: 2, "4-kelvin": 3}           # bits 21:20
_RTD_SHARING, _RTD_ROTATION_SHARING = 1, 2                 # bits 19:18
RTD_EXCITATION_UA = {5: 1, 10: 2, 25: 3, 50: 4, 100: 5,    # bits 17:14
                     250: 6, 500: 7, 1000: 8}
RTD_CURVES = {"european": 0, "american": 1, "japanese": 2, "its-90": 3}  # bits 13:12

# Thermocouple open-circuit check current (bits 19:18)
_TC_OPEN_CIRCUIT_UA = {10: 0, 100: 1, 500: 2, 1000: 3}

# Differential input range of the ADC: ±VREF/2, with the internal 2.5 V reference.
ADC_RANGE_V = 1.25


# ---- Channel assignment words ----

def sense_resistor(ohms: float) -> int:
    """A sense resistor, for the RTDs that point at this channel. The value is
    17.10 fixed point, i.e. ohms to 1/1024 Ω, up to 131 kΩ."""
    raw = round(ohms * 1024)
    if not 0 < raw < 1 << 27:
        raise ValueError(f"sense resistor {ohms} Ω is out of range")
    return _SENSE_RESISTOR << 27 | raw


def rtd(rsense_channel: int, rsense_ohms: float, kind: str = "PT-100", wires=4,
        rotate: bool = True, excitation_ua: int = 250, curve: str = "european") -> int:
    """An RTD. The channel it is assigned to is the upper of the pair its
    voltage is measured across; a 4-wire one takes its excitation from the
    channel above that.

    Only the sharing excitation modes are offered: they ground the low end of
    the sense resistor inside the chip. `rotate` also reverses the excitation
    on alternate readings, which cancels thermocouple voltages at the RTD's
    joints (4-wire only).

    The excitation flows through the sense resistor too, and its voltage there
    has to fit the ADC's ±1.25 V; rsense_ohms is only used to check that."""
    if excitation_ua * 1e-6 * rsense_ohms > ADC_RANGE_V:
        raise ValueError(f"{excitation_ua} µA through {rsense_ohms:.0f} Ω is "
                         f"{excitation_ua * 1e-6 * rsense_ohms:.2f} V, beyond the ADC's "
                         f"±{ADC_RANGE_V} V — pick a smaller excitation current")
    if rotate and wires not in (4, "4-kelvin"):
        raise ValueError("current rotation needs a 4-wire RTD")
    return (RTD_TYPES[kind] << 27 | rsense_channel << 22 | _RTD_WIRES[wires] << 20
            | (_RTD_ROTATION_SHARING if rotate else _RTD_SHARING) << 18
            | RTD_EXCITATION_UA[excitation_ua] << 14 | RTD_CURVES[curve] << 12)


def thermocouple(tc_type: str, cold_junction_channel: int, single_ended: bool = False,
                 open_circuit_ua: int | None = 10) -> int:
    """A thermocouple, compensated with the temperature of the cold-junction
    channel (0 = none). Differential, it is measured from the channel below the
    assigned one; single-ended, from COM. open_circuit_ua pulses that current
    before each conversion to detect a broken thermocouple (None = no check)."""
    word = (THERMOCOUPLE_TYPES[tc_type] << 27 | cold_junction_channel << 22
            | int(single_ended) << 21)
    if open_circuit_ua is not None:
        word |= 1 << 20 | _TC_OPEN_CIRCUIT_UA[open_circuit_ua] << 18
    return word


def active_analog(table_offset: int, table_rows: int, single_ended: bool = True) -> int:
    """A sensor with a voltage output (e.g. a TSic 50x analog), converted with a
    custom table written by write_table(). Can serve as a cold junction."""
    return (_ACTIVE_ANALOG << 27 | int(single_ended) << 26
            | table_offset << 6 | (table_rows - 1))


def active_analog_table(points) -> bytes:
    """Encode (millivolts, °C) points for an active analog sensor table: per row
    the voltage in 1/4096 mV (signed 24 bit, so within ±2048 mV), then the
    temperature in 1/1024 K (24 bit). The points must ascend in voltage.

    The first row is not part of the measured range: readings below the second
    row are flagged "sensor under range" and extrapolated towards the first
    (datasheet Table 68, Figure 54). Put it on the sensor's curve below its
    lowest output."""
    out = bytearray()
    for mv, celsius in points:
        if not -2048 < mv < 2048:
            raise ValueError(f"{mv} mV is outside the table's ±2048 mV")
        out += (round(mv * 4096) & 0xFFFFFF).to_bytes(3, "big")
        out += round((celsius + 273.15) * 1024).to_bytes(3, "big")
    return bytes(out)


# ---- Results ----

# Bits 31:24 of a result. A hard fault leaves no temperature (the chip reports
# -999 °C); a soft fault keeps it, but it is beyond what the chip vouches for.
_FAULTS = (
    (0x80, "sensor hard fault"),
    (0x40, "ADC hard fault"),
    (0x20, "cold-junction hard fault"),
    (0x10, "cold-junction soft fault"),
    (0x08, "sensor over range"),
    (0x04, "sensor under range"),
    (0x02, "ADC out of range"),
)
_HARD_FAULTS = 0xE0
_VALID = 0x01


@dataclass
class Reading:
    channel: int
    temperature: float | None    # °C; None when a fault leaves no usable number
    faults: list[str] = field(default_factory=list)


def decode_result(channel: int, word: int) -> Reading:
    flags = word >> 24
    faults = [name for bit, name in _FAULTS if flags & bit]
    if not flags & _VALID:
        faults.append("no valid result")
    value = word & 0xFFFFFF
    if value & 0x800000:
        value -= 1 << 24
    usable = flags & _VALID and not flags & _HARD_FAULTS
    return Reading(channel, value / 1024 if usable else None, faults)


# ---- The chip ----

class LTC2986:
    STARTUP_TIMEOUT    = 0.5   # s; the chip is ready ~200 ms after power-up
    # A conversion is two or three 82 ms ADC cycles (rotation, open-circuit
    # check, cold junction); a full second only ever trips on a dead chip.
    CONVERSION_TIMEOUT = 1.0   # s

    def __init__(self, spi):
        self.spi = spi

    # ---- raw memory access ----

    def read(self, address: int, length: int) -> bytes:
        rx = self.spi.transfer(bytes([_READ, address >> 8, address & 0xFF]) + bytes(length))
        return rx[3:]

    def write(self, address: int, data: bytes):
        self.spi.transfer(bytes([_WRITE, address >> 8, address & 0xFF]) + bytes(data))

    def read_word(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "big")

    def write_word(self, address: int, value: int):
        self.write(address, value.to_bytes(4, "big"))

    # ---- status ----

    def status(self) -> int:
        return self.read(COMMAND_STATUS, 1)[0]

    def wait_idle(self, timeout: float) -> int:
        """Poll until the chip is idle and return the status byte. Raises
        TimeoutError with the last status otherwise — 0x00 or 0xFF there means
        nothing is answering on the bus at all."""
        deadline = time.monotonic() + timeout
        while True:
            s = self.status()
            if s & _STATE_MASK == _IDLE:
                return s
            if time.monotonic() > deadline:
                raise TimeoutError(f"LTC2986 not ready (status 0x{s:02X})")
            time.sleep(0.01)

    # ---- configuration ----

    def set_global_config(self, rejection=50):
        self.write(GLOBAL_CONFIG, bytes([REJECTION[rejection]]))

    def assign(self, channel: int, word: int):
        self.write_word(ASSIGN_BASE + 4 * (channel - 1), word)

    def assignment(self, channel: int) -> int:
        return self.read_word(ASSIGN_BASE + 4 * (channel - 1))

    def write_table(self, offset: int, table: bytes):
        """Write a custom table at `offset` 6-byte rows into the table area."""
        address = CUSTOM_TABLE + 6 * offset
        if address + len(table) - 1 > CUSTOM_TABLE_END:
            raise ValueError("custom table does not fit")
        self.write(address, table)

    # ---- conversions ----

    def convert(self, channel: int) -> Reading:
        """Convert one channel and return its result. Blocks for the conversion,
        roughly 170–250 ms."""
        self.write(COMMAND_STATUS, bytes([_START | channel]))
        self.wait_idle(self.CONVERSION_TIMEOUT)
        return decode_result(channel, self.read_word(RESULT_BASE + 4 * (channel - 1)))
