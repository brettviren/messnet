"""Telling systemd how the service is doing (``sd_notify``), when it runs under ``Type=notify``.

Without ``$NOTIFY_SOCKET`` (cron, a terminal) every call is a harmless no-op.
"""

import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(state: str) -> bool:
    """Send STATE (``READY=1``, ``STOPPING=1``, ``STATUS=...``) to systemd. True if it was sent."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]                # abstract socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(state.encode(), addr)
    except OSError as err:
        log.warning("sd_notify %r: %s", state, err)
        return False
    return True


def ready(status: str = "") -> bool:
    """Say the service is up, then forget the socket so the processes it starts (links) don't use it."""
    sent = notify("READY=1" + (f"\nSTATUS={status}" if status else ""))
    os.environ.pop("NOTIFY_SOCKET", None)
    return sent
