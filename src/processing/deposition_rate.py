"""Deposition rate from a live thickness series.

A sample-to-sample difference is mostly noise, so the rate is the slope of a
least-squares straight line through the samples of the last `window` seconds,
the newest included. The window trails the newest sample, so the rate is causal
and lags a step in rate by about half a window.

tools/csv_plotter.py works the same figure out from the thickness of a run CSV
that predates the logged rate. Its trailing_rate() has to keep matching this
one: change the window rule or the warm-up here and change it there too.
"""

import math
from collections import deque

# The window as configured by the operator: seconds of thickness one rate value
# is fitted to. Short windows follow a change sooner, long ones are quieter.
DEFAULT_WINDOW = 1.0   # s
MIN_WINDOW = 0.1       # s; holds no slope at all below ~20 Hz (see update)
MAX_WINDOW = 300.0     # s


class RateEstimator:
    """update(t, thickness) adds one sample and returns the rate in thickness
    units per second, or None while there is none to give.

    There is none until the samples in the window span at least half of it: a
    slope through the first two or three samples of a run says nothing about the
    deposition. A thickness that is None or not finite (a failed solve) is left
    out of the fit, and the rate carries on from the samples around it.

    The fit is kept as running sums, so a sample costs the same at 1000 samples
    in the window as at 10: refitting the window with numpy took ~0.5 ms a
    sample on the Pitaya, nearly all of it call overhead. The sums are taken
    relative to an anchor sample, because the timestamps are ~1.7e9 s and
    squaring them as they are would cancel every digit of the slope, and they are
    recomputed exactly, from a new anchor, each time the window has turned over."""

    def __init__(self, window: float = DEFAULT_WINDOW):
        self.window = window
        self._t: deque = deque()
        self._v: deque = deque()
        self._recompute()

    @property
    def window(self) -> float:
        return self._window

    @window.setter
    def window(self, seconds: float):
        self._window = min(max(float(seconds), MIN_WINDOW), MAX_WINDOW)

    def reset(self):
        self._t.clear()
        self._v.clear()
        self._recompute()

    def _recompute(self):
        """Exact sums over the window, relative to its oldest sample."""
        self._t0, self._v0 = (self._t[0], self._v[0]) if self._t else (0.0, 0.0)
        st = sv = stt = stv = 0.0
        for t, v in zip(self._t, self._v):
            t -= self._t0
            v -= self._v0
            st += t
            sv += v
            stt += t * t
            stv += t * v
        self._st, self._sv, self._stt, self._stv = st, sv, stt, stv
        self._changes = 0  # samples added or dropped since

    def update(self, t: float, thickness) -> float | None:
        # A clock stepped backwards (NTP) would leave the samples taken before
        # the step in the window for good, since they all lie in its future.
        if self._t and t < self._t[-1]:
            self.reset()
        if thickness is not None and math.isfinite(thickness):
            v = float(thickness)
            if not self._t:
                self._recompute()  # zero sums
                self._t0, self._v0 = t, v
            self._t.append(t)
            self._v.append(v)
            t1, v1 = t - self._t0, v - self._v0
            self._st += t1
            self._sv += v1
            self._stt += t1 * t1
            self._stv += t1 * v1
            self._changes += 1
        while self._t and self._t[0] <= t - self._window:
            t1, v1 = self._t.popleft() - self._t0, self._v.popleft() - self._v0
            self._st -= t1
            self._sv -= v1
            self._stt -= t1 * t1
            self._stv -= t1 * v1
            self._changes += 1
        n = len(self._t)
        if self._changes > n:
            self._recompute()
        if n < 2 or self._t[-1] - self._t[0] < self._window / 2:
            return None
        den = n * self._stt - self._st * self._st
        if den <= 0:
            return None
        return (n * self._stv - self._st * self._sv) / den
