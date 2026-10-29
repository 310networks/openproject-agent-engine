"""Minimal systemd notification support (PLAN section 15).

The engine sends ``READY=1`` once startup resolution succeeded and
``WATCHDOG=1`` after every poll, so the unit can optionally use
``Type=notify`` with ``WatchdogSec=``. No third-party dependency is needed:
the messages go straight to the ``NOTIFY_SOCKET`` datagram socket.
"""

from __future__ import annotations

import contextlib
import os
import socket

_NOTIFY_SOCKET_ENV = "NOTIFY_SOCKET"


def notify(message: str) -> bool:
    """Send one sd_notify-style state string. Return False when unconfigured."""
    address = os.environ.get(_NOTIFY_SOCKET_ENV)
    if not address:
        return False
    # An abstract socket is conventionally written as ``@name`` on the unit
    # side; the kernel expects a leading NUL byte.
    socket_address = "\0" + address[1:] if address.startswith("@") else address
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.connect(socket_address)
        sock.send(message.encode("utf-8"))
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return True
