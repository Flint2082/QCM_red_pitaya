"""Deposition rate from a live thickness series.

A sample-to-sample difference at ~10 Hz is mostly noise, so the rate is the
slope of a least-squares straight line through the samples of the last `window`
seconds, the newest included. The window trails the newest sample, so the rate
is causal and lags a step in rate by about half a window.

tools/csv_plotter.py works the same figure out from the thickness of a run CSV
that predates the logged rate. Its trailing_rate() has to keep matching this
one: change the window rule or the warm-up here and change it there too.
"""

import math
from collections import deque

import numpy as np

# The window as configured by the operator: seconds of thickness one rate value
# is fitted to. Short windows follow a change sooner, long ones are quieter.
DEFAULT_WINDOW = 1.0   # s
MIN_WINDOW = 0.5       # s; a few samples at ~10 Hz, the least a slope needs
MAX_WINDOW = 300.0     # s; every sample in the window is refitted each cycle


class RateEstimator:
    """update(t, thickness) adds one sample and returns the rate in thickness
    units per second, or None while there is none to give.

    There is none until the samples in the window span at least half of it: a
    slope through the first two or three samples of a run says nothing about the
    deposition. A thickness that is None or not finite (a failed solve) is left
    out of the fit, and the rate carries on from the samples around it."""

    def __init__(self, window: float = DEFAULT_WINDOW):
        self.window = window
        self._t: deque = deque()
        self._v: deque = deque()

    @property
    def window(self) -> float:
        return self._window

    @window.setter
    def window(self, seconds: float):
        self._window = min(max(float(seconds), MIN_WINDOW), MAX_WINDOW)

    def reset(self):
        self._t.clear()
        self._v.clear()

    def update(self, t: float, thickness) -> float | None:
        # A clock stepped backwards (NTP) would leave the samples taken before
        # the step in the window for good, since they all lie in its future.
        if self._t and t < self._t[-1]:
            self.reset()
        if thickness is not None and math.isfinite(thickness):
            self._t.append(t)
            self._v.append(float(thickness))
        while self._t and self._t[0] <= t - self._window:
            self._t.popleft()
            self._v.popleft()
        if len(self._t) < 2 or self._t[-1] - self._t[0] < self._window / 2:
            return None
        # Centred before the products are taken: the timestamps are ~1.7e9 s, and
        # squaring them as they are would cancel every digit of the slope.
        ts = np.fromiter(self._t, float, len(self._t))
        vs = np.fromiter(self._v, float, len(self._v))
        ts -= ts.mean()
        return float(np.dot(ts, vs - vs.mean()) / np.dot(ts, ts))
