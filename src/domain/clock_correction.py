"""Correction for the Red Pitaya's reference clock, from the board temperature.

Both mode frequencies are counted against the Pitaya's 125 MHz oscillator. When
that oscillator warms, every frequency reads off by the same fraction: about
+0.25-0.3 ppm per °C of the Zynq die on rp-f0ea58 (13 Nov 2026 calibration run;
0.33 ppm per °C of the V2 board's TSic on 2 Nov). The dual-mode compensation
cannot see it: a common fraction leaves the temperature it solves alone and
passes straight into the compensated mass frequency, about 1.7 Hz per °C, as if
film had come off. Turning the TEC on warmed the box 1.4 °C in a quarter of an
hour, and the first calibration point read 1 Hz low for it.

The Zynq die temperature is the thermometer because every Red Pitaya has it,
the production units without the LTC2986 included (domain/zynq_temperature.py).
It is not the oscillator's temperature: the die heats itself and reacts to
processor load within minutes, while the oscillator follows the board over
several. A first-order lag of LAG_S (8 min) on the die reading tracked the clock
best on 13 Nov (0.42 Hz rms left against 0.54 without it).

The coefficient belongs to the Red Pitaya, not to the crystal: each board's
oscillator has its own. A crystal profile is therefore calibrated on corrected
frequencies, and stays valid on another instrument with that one's coefficient.
Corrected means scaled to what the clock would read with the die at REF_C; the
reference only sets a constant scale, which nothing downstream depends on.
"""

import math

# Die temperature the corrected frequencies are referred to, °C.
REF_C = 45.0
# Allowed settings.
TEMPCO_RANGE = (-5.0, 5.0)      # ppm per °C
LAG_RANGE = (0.0, 3600.0)       # s
DEFAULT_LAG_S = 480.0


class ClockCorrection:
    def __init__(self, tempco_ppm: float = 0.0, lag_s: float = DEFAULT_LAG_S):
        self.tempco_ppm = 0.0
        self.lag_s = DEFAULT_LAG_S
        self.configure(tempco_ppm, lag_s)
        self._board: float | None = None   # the lagged die temperature, °C
        self._last_ts: float | None = None

    def configure(self, tempco_ppm: float, lag_s: float):
        """Set the coefficient (ppm per °C of die temperature; 0 = no
        correction) and the lag. Raises ValueError out of range."""
        tempco_ppm, lag_s = float(tempco_ppm), float(lag_s)
        if not (math.isfinite(tempco_ppm) and TEMPCO_RANGE[0] <= tempco_ppm <= TEMPCO_RANGE[1]):
            raise ValueError(f"clock tempco must be {TEMPCO_RANGE[0]:g} to {TEMPCO_RANGE[1]:g} ppm/°C")
        if not (math.isfinite(lag_s) and LAG_RANGE[0] <= lag_s <= LAG_RANGE[1]):
            raise ValueError(f"clock lag must be {LAG_RANGE[0]:g} to {LAG_RANGE[1]:g} s")
        self.tempco_ppm, self.lag_s = tempco_ppm, lag_s

    def update(self, timestamp: float | None, die_c: float | None):
        """Take a die reading (the temperature worker's, once a second). The
        same reading offered again, or none, changes nothing: the lagged value
        holds through a gap."""
        if timestamp is None or die_c is None or not math.isfinite(die_c):
            return
        if self._last_ts is not None and timestamp <= self._last_ts:
            return
        if self._board is None:
            self._board = float(die_c)
        else:
            dt = timestamp - self._last_ts
            self._board += (dt / (dt + self.lag_s) if self.lag_s > 0 else 1.0) * (die_c - self._board)
        self._last_ts = timestamp

    @property
    def board_c(self) -> float | None:
        """The lagged die temperature, °C; None before the first reading."""
        return self._board

    def ppm_at(self, board_c: float | None) -> float:
        """The clock error at a lagged board temperature, ppm: frequencies read
        this much high. 0 without a reading or with the correction off."""
        if board_c is None or not self.tempco_ppm:
            return 0.0
        return self.tempco_ppm * (board_c - REF_C)

    @property
    def ppm(self) -> float:
        return self.ppm_at(self._board)

    @property
    def scale(self) -> float:
        """Measured frequency over corrected frequency."""
        return 1.0 + self.ppm * 1e-6
