import io
import contextlib
import struct
import time

import usb.core
import usb.util
import usb.backend.libusb1

_CONTROL_SUCCESS       = 0
_SERVICER_COMMAND_RETRY = 64

_VID = 0x2886
_PID = 0x001A

_DOA_RESID  = 20
_DOA_CMDID  = 18
_DOA_CNT    = 2
_DOA_TYPE   = "uint16"

_DIR_LABELS = ["前", "右前", "右", "右后", "后", "左后", "左", "左前"]


class _ReSpeaker:
    TIMEOUT = 100000

    def __init__(self, dev):
        self.dev = dev

    def read_doa_vad(self):
        windex   = _DOA_RESID
        wvalue   = 0x80 | _DOA_CMDID
        length   = _DOA_CNT * 2 + 1

        response = self.dev.ctrl_transfer(
            usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
            0, wvalue, windex, length, self.TIMEOUT,
        )

        for _ in range(100):
            if response[0] == _CONTROL_SUCCESS:
                break
            if response[0] == _SERVICER_COMMAND_RETRY:
                time.sleep(0.01)
                response = self.dev.ctrl_transfer(
                    usb.util.CTRL_IN | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE,
                    0, wvalue, windex, length, self.TIMEOUT,
                )
            else:
                raise ValueError(f"XVF3800: unknown status code {response[0]}")
        else:
            raise ValueError("XVF3800: read_doa_vad exceeded 100 retry attempts")

        doa, vad = struct.unpack_from("<HH", response.tobytes(), 1)
        return int(doa), int(vad)

    def close(self):
        usb.util.dispose_resources(self.dev)


def _find_device():
    try:
        import libusb_package
        backend = usb.backend.libusb1.get_backend(find_library=libusb_package.find_library)
        dev = usb.core.find(idVendor=_VID, idProduct=_PID, backend=backend)
    except Exception:
        dev = usb.core.find(idVendor=_VID, idProduct=_PID)
    if dev is None:
        return None
    return _ReSpeaker(dev)


class DoaVadReader:
    """Read DOA and VAD from a ReSpeaker XVF3800 4-Mic Array over USB.

    Usage::

        with DoaVadReader() as reader:
            angle, vad = reader.read()   # angle: 0-359 degrees, vad: 0/1

    Raises RuntimeError on construction if the device is not found.
    """

    def __init__(self):
        self._dev = _find_device()
        if self._dev is None:
            raise RuntimeError(
                f"ReSpeaker XVF3800 not found (VID:0x{_VID:04X} PID:0x{_PID:04X}). "
                "Check USB connection."
            )

    def read(self):
        """Return ``(doa_angle: int, vad: int)`` — angle 0-359°, vad 0/1."""
        with contextlib.redirect_stdout(io.StringIO()):
            return self._dev.read_doa_vad()

    def direction_label(self, doa_angle):
        """Map 0-359° to one of 8 cardinal direction labels."""
        return _DIR_LABELS[int(doa_angle % 360) // 45]

    def close(self):
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
