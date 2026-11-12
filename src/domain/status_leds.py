"""Front-panel status LEDs.

The bitstream exposes a 4-bit software register whose bits drive the Red
Pitaya's GPIO pins directly, so the LEDs are whatever the control software last
wrote there. Counting the LEDs 1-4 from the least significant bit:

| LED | bit | meaning |
|---|---|---|
| 1 | 0 | lit once the bitstream is loaded; flashes while a measurement has a problem |
| 2 | 1 | mass-mode PLL locked |
| 3 | 2 | temp-mode PLL locked |
| 4 | 3 | yellow, unassigned (set_aux) |

A "problem" is the worker's call, made once per acquisition cycle; see
QCMWorker._refresh_leds. The flashing is done here, from the time of each call,
so it needs no thread of its own: the worker's loop runs at ~10 Hz, which is
plenty for a 2 Hz flash, and it is never blocked for long during a run.

The register is written only when the pattern changes. write_register reads
every write back to check it, which is not worth paying ten times a second for
a value that has not moved.
"""

import time

# The register the LEDs hang off (a 4-bit From Processor sw_reg in qcm_rp.slx).
# A bitstream that names it differently is still found (see _find_register), so
# this only has to match when the bitstream has more than one register that
# looks like it.
LED_REGISTER = "led_control"


class StatusLEDs:
    READY     = 1 << 0   # LED 1: bitstream loaded / flashing on a measurement problem
    LOCK_MASS = 1 << 1   # LED 2
    LOCK_TEMP = 1 << 2   # LED 3
    AUX       = 1 << 3   # LED 4, yellow — not assigned to anything yet

    FLASH_PERIOD = 0.5   # s, one on/off cycle of LED 1 while flashing (2 Hz)

    def __init__(self, fpga):
        self.fpga = fpga
        self.register = self._find_register(fpga)
        self._value = None   # last pattern written; None = nothing written yet
        self._aux = False
        if self.register is None:
            print("[LED] Bitstream has no LED register — front-panel LEDs inactive")
        else:
            print(f"[LED] Front-panel LEDs on register '{self.register}'")
        # The bitstream is loaded by the time anything constructs this, which
        # is exactly what LED 1 says.
        self.update()

    @staticmethod
    def _find_register(fpga):
        """LED_REGISTER if the bitstream has it, else the one register whose name
        says LED or GPIO. Two or more candidates is ambiguous, and driving the
        wrong register is worse than driving none."""
        if fpga.has_register(LED_REGISTER):
            return LED_REGISTER
        names = [n for n in getattr(fpga, "register_map", {})
                 if "led" in n.lower() or "gpio" in n.lower()]
        if len(names) > 1:
            print(f"[LED] Several candidate LED registers ({', '.join(sorted(names))}); "
                  f"name the right one '{LED_REGISTER}' in the model to use it")
        return names[0] if len(names) == 1 else None

    def update(self, lock_mass=False, lock_temp=False, problem=False, now=None):
        """Show one cycle's state. `problem` flashes LED 1 instead of holding it."""
        now = time.time() if now is None else now
        ready = not problem or (now % self.FLASH_PERIOD) < self.FLASH_PERIOD / 2
        self._write((self.READY if ready else 0)
                    | (self.LOCK_MASS if lock_mass else 0)
                    | (self.LOCK_TEMP if lock_temp else 0)
                    | (self.AUX if self._aux else 0))

    def set_aux(self, on: bool):
        """LED 4. Shown from the next update()."""
        self._aux = bool(on)

    def off(self):
        """All LEDs dark — on shutdown, so a lit panel always means the software
        is running and the lock LEDs are never left claiming a stale lock."""
        self._write(0)

    def _write(self, value: int):
        if self.register is None or value == self._value:
            return
        try:
            self.fpga.write_register(self.register, value)
            self._value = value
        except Exception as e:
            # Not worth taking the measurement down for, and not worth a message
            # every cycle either: say it once and leave the LEDs alone.
            print(f"[LED] Writing '{self.register}' failed, LEDs disabled: {e}")
            self.register = None
