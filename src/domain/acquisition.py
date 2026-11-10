"""Acquisition timing: how often the QCM worker samples the instrument, and how
often those samples go out to the GUI and the PLC and into the run log.

The FPGA updates the frequency registers far faster than any of these, so the
sample rate is purely a question of how often the Python side reads them. The
deposition rate is fitted to every sample; the GUI, the PLC and the log each
take a subset on a schedule of their own (Decimator)."""

# 100 Hz keeps a short, quick-reacting rate window full of samples; it takes
# about half of one core on the Pitaya.
DEFAULT_SAMPLE_RATE = 100.0  # Hz
MIN_SAMPLE_RATE = 1.0
MAX_SAMPLE_RATE = 100.0

# The GUI (over the WebSocket) and the PLC. Each message is drawn on every open
# browser and written to the PLC, and the old lab PCs' browsers struggle well
# before the instrument does.
DEFAULT_PUBLISH_RATE = 10.0  # Hz
MIN_PUBLISH_RATE = 1.0
MAX_PUBLISH_RATE = 25.0

# The run log. 0 logs every sample. A row is ~300 bytes: ~250 MB a day at
# 10 Hz, ~2.6 GB at 100 Hz. Each logged row still carries the rate fitted to
# every sample.
DEFAULT_LOG_RATE = 10.0      # Hz, 0 = every sample
MAX_LOG_RATE = MAX_SAMPLE_RATE


class Decimator:
    """Picks the samples of the acquisition loop that go out at `rate` Hz.

    It keeps a schedule of its own, so 10 Hz out of a 100 Hz loop is every tenth
    sample rather than whichever lands nearest a 100 ms mark, and a sample up to
    half a sample period early still counts. A rate of 0, or one at or above the
    sample rate, takes every sample."""

    def __init__(self, rate: float = 0.0):
        self.rate = rate
        self._next = 0.0

    def due(self, now: float, sample_period: float) -> bool:
        if self.rate <= 0:
            return True
        if now + 0.5 * sample_period < self._next:
            return False
        period = 1.0 / self.rate
        # More than a period behind (the loop stalled): start again from now.
        self._next = self._next + period if now - self._next < period else now + period
        return True
