"""Linux spidev access, without the py-spidev package.

py-spidev is a C extension with no wheel for the Pitaya (linux_armv7l), so a box
without internet could not install it (see src/README.md, "Without internet").
All it does is a few ioctls on /dev/spidevB.C, which Python's fcntl makes just
as well, so this module does them itself and adds no dependency.

Only full-duplex single transfers are needed: chip select is held for the whole
of one transfer() and released after it.
"""

import ctypes
import os
import struct

# <linux/spi/spidev.h>. _IOW('k', nr, size) = write << 30 | size << 16 | 'k' << 8 | nr
def _iow(nr: int, size: int) -> int:
    return (1 << 30) | (size << 16) | (ord("k") << 8) | nr

# struct spi_ioc_transfer: tx_buf, rx_buf (u64 pointers), len, speed_hz (u32),
# delay_usecs (u16), bits_per_word, cs_change, tx_nbits, rx_nbits,
# word_delay_usecs, pad (u8). 32 bytes.
_TRANSFER = struct.Struct("=QQIIHBBBBBB")

SPI_IOC_MESSAGE_1        = _iow(0, _TRANSFER.size)
SPI_IOC_WR_MODE          = _iow(1, 1)
SPI_IOC_WR_BITS_PER_WORD = _iow(3, 1)
SPI_IOC_WR_MAX_SPEED_HZ  = _iow(4, 4)


class SpiDevice:
    def __init__(self, path: str, mode: int = 0, speed_hz: int = 1_000_000):
        # Imported here rather than at the top so the module still imports on a
        # PC (no fcntl on Windows), where the backend is tested against fakes.
        import fcntl
        self._fcntl = fcntl
        self.path = path
        self.speed_hz = speed_hz
        self._fd = os.open(path, os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, SPI_IOC_WR_MODE, struct.pack("=B", mode))
            fcntl.ioctl(self._fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("=B", 8))
            fcntl.ioctl(self._fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("=I", speed_hz))
        except OSError:
            os.close(self._fd)
            raise

    def transfer(self, data: bytes) -> bytes:
        """Clock `data` out and return the bytes clocked in at the same time."""
        n = len(data)
        tx = ctypes.create_string_buffer(bytes(data), n)
        rx = ctypes.create_string_buffer(n)
        xfer = _TRANSFER.pack(ctypes.addressof(tx), ctypes.addressof(rx), n,
                              self.speed_hz, 0, 8, 0, 0, 0, 0, 0)
        self._fcntl.ioctl(self._fd, SPI_IOC_MESSAGE_1, xfer)
        return rx.raw

    def close(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
