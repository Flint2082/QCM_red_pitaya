"""The Zynq's own die temperature, from its XADC.

Every Red Pitaya has this sensor, with or without the V2 board: Linux exposes
the XADC through IIO as /sys/bus/iio/devices/iio:deviceN with the name "xadc"
(the current image registers it twice; either one will do).

It measures neither the crystal nor the room. The die runs ~30 °C above
ambient and follows its own power draw. It is logged because the Pitaya's
125 MHz reference oscillator sits on the same board: a change in that clock
shifts both mode frequencies by the same fraction, which the dual-mode
compensation cannot tell apart from mass. Over the 30 Oct - 2 Nov 2026 weekend
both modes moved by ~0.3 ppm per °C of the V2 board's temperature, and this
reading may track the oscillator more closely.
"""

import glob
import os

IIO_DEVICES = "/sys/bus/iio/devices"

# One conversion resolves 0.12 °C and the reading jitters by about ±2 counts,
# so a reading averages this many: ~45 ms on the Pitaya, ±0.025 °C.
SAMPLES = 128


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


class ZynqDieTemperature:
    def __init__(self, device_dir: str):
        self.device_dir = device_dir
        self._raw_path = os.path.join(device_dir, "in_temp0_raw")
        self.offset = float(_read(os.path.join(device_dir, "in_temp0_offset")))  # counts
        self.scale = float(_read(os.path.join(device_dir, "in_temp0_scale")))    # m°C per count

    @classmethod
    def open(cls, root: str = IIO_DEVICES) -> "ZynqDieTemperature":
        for d in sorted(glob.glob(os.path.join(root, "iio:device*"))):
            try:
                name = _read(os.path.join(d, "name"))
            except OSError:
                continue
            if name == "xadc" and os.path.exists(os.path.join(d, "in_temp0_raw")):
                return cls(d)
        raise RuntimeError(f"no XADC under {root}")

    def read(self) -> float:
        """Die temperature in °C, the mean of SAMPLES conversions. Each read of
        the raw file is a fresh conversion."""
        total = sum(int(_read(self._raw_path)) for _ in range(SAMPLES))
        return (total / SAMPLES + self.offset) * self.scale / 1000.0
