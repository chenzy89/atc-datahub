"""UDP 报文接收器"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from datetime import datetime
from typing import Callable, Optional

from .config import EndpointConfig

logger = logging.getLogger("aftn_web.receiver")


class UdpReceiver:
    """UDP 报文接收线程"""

    def __init__(
        self,
        config: EndpointConfig,
        on_message: Callable[[bytes, str, int, datetime], None],
    ) -> None:
        self.config = config
        self.on_message = on_message
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            logger.warning("receiver already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="udp-receiver")
        self._thread.start()
        logger.info(
            "UDP receiver started on %s:%d (multicast=%s)",
            self.config.bind_host,
            self.config.port,
            self.config.multicast_group or "none",
        )

    def stop(self) -> None:
        self._stop.set()
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("UDP receiver stopped")

    def _run(self) -> None:
        sock = self._create_socket()
        if sock is None:
            return
        self._socket = sock
        while not self._stop.is_set():
            try:
                payload, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                logger.exception("socket error")
                continue
            now = datetime.utcnow()
            try:
                self.on_message(payload, addr[0], addr[1], now)
            except Exception:
                logger.exception("message handler error")

    def _create_socket(self) -> Optional[socket.socket]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.settimeout(1.0)
            sock.bind((self.config.bind_host, self.config.port))

            if self.config.multicast_group:
                iface = self.config.interface_ip or "0.0.0.0"
                mreq = struct.pack(
                    "=4s4s",
                    socket.inet_aton(self.config.multicast_group),
                    socket.inet_aton(iface),
                )
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                logger.info(
                    "joined multicast group %s on interface %s",
                    self.config.multicast_group,
                    iface,
                )

            return sock
        except OSError as exc:
            logger.error("failed to create socket: %s", exc)
            return None
