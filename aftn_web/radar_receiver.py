"""CAT062 雷达数据接收器 — 解析飞行跑道和飞行程序 (SID/STAR)

监听组播 228.28.28.28:8107，接收 ASTERIX CAT062 格式雷达数据报文，
解析航班呼号、使用跑道、飞行程序，并更新到飞行计划表。

CAT062 解析逻辑委托给参考项目中已验证的解析器:
  /home/share/ATC_Display/atc_display/cat062.py
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger("aftn_web.radar")

# 引用已验证的 CAT062 解析器
try:
    import sys as _sys
    _sys.path.insert(0, "/home/share/ATC_Display")
    from atc_display.cat062 import Cat062Parser as _RefParser
    _ref_parser = _RefParser()
except ImportError:
    _ref_parser = None
    logger.warning("无法导入参考CAT062解析器，雷达功能不可用")


def parse_datagram(payload: bytes) -> list[dict[str, Any]]:
    """解析完整 CAT062 UDP 数据报，返回每条记录的解析结果"""
    if _ref_parser is None:
        return []

    records: list[dict[str, Any]] = []
    try:
        tracks = _ref_parser.parse_datagram(payload)
    except Exception:
        logger.exception("参考解析器异常")
        return records

    for t in tracks:
        # 综合多个来源的呼号：I245 target_id, I390 CSN, I380 ID
        callsign = (t.target_id or t.acid or "").strip().upper()
        # 飞行程序：SID/STAR 组合
        sid = (t.sid or "").strip()
        star = (t.star or "").strip()
        flight_procedure = sid or star
        if sid and star:
            flight_procedure = f"{sid}/{star}"

        records.append({
            "callsign": callsign,
            "acid": (t.acid or "").strip(),
            "runway": (t.runway or "").strip(),
            "sid": sid,
            "star": star,
            "flight_procedure": flight_procedure,
            "ssr": (t.ssr or "").strip(),
            "track_number": t.track_number,
        })

    return records


class RadarReceiver:
    """CAT062 雷达数据接收器，监听组播 UDP 报文并更新飞行计划"""

    def __init__(
        self,
        multicast_group: str,
        port: int,
        interface_ip: str,
        on_radar_data: Callable[[dict[str, Any], str, int, datetime], None],
    ) -> None:
        self.multicast_group = multicast_group
        self.port = port
        self.interface_ip = interface_ip
        self.on_radar_data = on_radar_data
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            logger.warning("radar receiver already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="radar-receiver")
        self._thread.start()
        logger.info(
            "radar receiver started on %s:%d (interface=%s)",
            self.multicast_group, self.port, self.interface_ip,
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
        logger.info("radar receiver stopped")

    def _run(self) -> None:
        sock = self._create_socket()
        if sock is None:
            return
        self._socket = sock

        total_frames = 0
        total_parsed = 0
        while not self._stop.is_set():
            try:
                payload, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    break
                logger.exception("radar socket error")
                continue

            now = datetime.utcnow()
            total_frames += 1

            try:
                records = parse_datagram(payload)
            except Exception:
                logger.exception("CAT062 parse error from %s", addr[0])
                continue

            for parsed in records:
                if parsed.get('callsign') or parsed.get('ssr'):
                    total_parsed += 1
                    try:
                        self.on_radar_data(parsed, addr[0], addr[1], now)
                    except Exception:
                        logger.exception("radar data handler error")

            if total_frames % 100 == 0:
                logger.debug("radar: frames=%d parsed=%d", total_frames, total_parsed)

    def _create_socket(self) -> Optional[socket.socket]:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
            sock.settimeout(1.0)
            sock.bind(("0.0.0.0", self.port))

            mreq = struct.pack(
                "=4s4s",
                socket.inet_aton(self.multicast_group),
                socket.inet_aton(self.interface_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            logger.info(
                "joined radar multicast group %s on interface %s",
                self.multicast_group, self.interface_ip,
            )
            return sock
        except OSError as exc:
            logger.error("failed to create radar socket: %s", exc)
            return None
