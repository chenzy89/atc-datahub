"""CAT062 雷达数据接收器 — 解析飞行跑道和飞行程序 (SID/STAR)

监听组播 228.28.28.28:8107，接收 ASTERIX CAT062 格式雷达数据报文，
解析航班呼号、使用跑道、飞行程序，并更新到飞行计划表。

解析逻辑参考: ATC_Display/atc_display/cat062.py (工作验证版)
CAT062 标准参考：EUROCONTROL ASTERIX Category 062 (Track Messages)
"""

from __future__ import annotations

import logging
import math
import socket
import struct
import threading
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger("aftn_web.radar")


class _Cursor:
    """字节流读取游标"""
    def __init__(self, data: bytes, start: int, end: int):
        self.data = data
        self.idx = start
        self.end = end

    def remaining(self) -> int:
        return self.end - self.idx

    def skip(self, size: int) -> None:
        if self.idx + size > self.end:
            raise ValueError(f"数据不足: 需要 {size} 字节, 剩余 {self.remaining()} 字节")
        self.idx += size

    def read(self, size: int) -> bytes:
        if self.idx + size > self.end:
            raise ValueError(f"数据不足: 需要 {size} 字节, 剩余 {self.remaining()} 字节")
        chunk = self.data[self.idx:self.idx + size]
        self.idx += size
        return chunk

    def read_u8(self) -> int:
        v = self.data[self.idx]
        self.idx += 1
        return v

    def read_u16(self) -> int:
        return int.from_bytes(self.read(2), "big", signed=False)

    def read_i16(self) -> int:
        return int.from_bytes(self.read(2), "big", signed=True)

    def read_i32(self) -> int:
        return int.from_bytes(self.read(4), "big", signed=True)


def _decode_ia5_callsign(payload: bytes) -> str:
    """从 6 字节 IA5 编码解码航班呼号"""
    if len(payload) != 6:
        return ""
    codes = [
        (payload[0] & 0xFC) >> 2,
        ((payload[0] & 0x03) << 4) | ((payload[1] & 0xF0) >> 4),
        ((payload[1] & 0x0F) << 2) | ((payload[2] & 0xC0) >> 6),
        payload[2] & 0x3F,
        (payload[3] & 0xFC) >> 2,
        ((payload[3] & 0x03) << 4) | ((payload[4] & 0xF0) >> 4),
        ((payload[4] & 0x0F) << 2) | ((payload[5] & 0xC0) >> 6),
        payload[5] & 0x3F,
    ]

    def _to_ascii(v: int) -> str:
        if v == 0:
            return " "
        if v <= 26:
            return chr(v + 64)
        return chr(v)

    return "".join(_to_ascii(c) for c in codes)


def _read_ssr(cursor: _Cursor) -> str:
    """读取 4 位八进制 SSR 应答机编码"""
    b1 = cursor.read_u8() & 0x0F
    b2 = cursor.read_u8()
    value = b1 * 256 + b2
    digits = []
    for _ in range(4):
        digits.append(str(value & 0x07))
        value >>= 3
    return "".join(reversed(digits))


def _parse_fspecs(cursor: _Cursor) -> list:
    """读取 FSPEC，返回每个字节的值列表（最多 5 字节）"""
    fspecs = []
    while True:
        v = cursor.read_u8()
        fspecs.append(v)
        if v & 0x01 == 0:  # extension bit = 0 → last FSPEC byte
            break
        if len(fspecs) >= 5:
            break
    return fspecs


def _parse_380(cursor: _Cursor) -> dict:
    """解析 I062/380 Flight Plan Related Data，返回 callsign 等"""
    result: dict = {}
    octets = []
    while True:
        octet = cursor.read_u8()
        octets.append(octet)
        if octet & 0x01 == 0:
            break
        if len(octets) >= 4:
            break

    def _has(byte_idx: int, mask: int) -> bool:
        return len(octets) > byte_idx and bool(octets[byte_idx] & mask)

    # First FSPEC byte (main aircraft parameters)
    if _has(0, 0x80): cursor.skip(3)  # ADR - Target Address
    if _has(0, 0x40):  # ID - Target Identification (callsign, 6 bytes IA5)
        result["callsign"] = _decode_ia5_callsign(cursor.read(6)).strip()
    if _has(0, 0x20): cursor.skip(2)  # MHG - Magnetic Heading
    if _has(0, 0x10): cursor.skip(2)  # IAS - Indicated Airspeed
    if _has(0, 0x08): cursor.skip(2)  # TAS - True Airspeed
    if _has(0, 0x04): cursor.skip(2)  # SAL - Selected Altitude
    if _has(0, 0x02): cursor.skip(2)  # FSS - Final State Select Altitude

    # Second FSPEC byte
    if _has(1, 0x80): cursor.skip(1)  # TIS - Track Intent Status
    if _has(1, 0x40):  # TID - Track Intent Data
        rep = cursor.read_u8()
        cursor.skip(15 * rep)
    if _has(1, 0x20): cursor.skip(2)  # COM - Communication
    if _has(1, 0x10): cursor.skip(2)  # SAB - Selected Altitude Blank
    if _has(1, 0x08): cursor.skip(7)  # ACS - Aircraft Characteristics
    if _has(1, 0x04): cursor.skip(2)  # BVR - Barometric Vertical Rate
    if _has(1, 0x02): cursor.skip(2)  # GVR - Geometric Vertical Rate

    # Third/forth FSPEC bytes (skipped, none relevant)
    for i in range(2, len(octets)):
        if octets[i] & 0x80: cursor.skip(2)  # RAN
        if octets[i] & 0x40: cursor.skip(2)  # TAR
        if octets[i] & 0x20: cursor.skip(2)  # TAN
        if octets[i] & 0x10: cursor.skip(2)  # GSP
        if octets[i] & 0x08: cursor.skip(1)  # VUN
        if octets[i] & 0x04: cursor.skip(8)  # MET
        if octets[i] & 0x02: cursor.skip(1)  # EMC

    return result


def _parse_390(cursor: _Cursor) -> dict:
    """解析 I062/390 Flight Plan Info，返回 runway, sid, star 等"""
    result: dict = {}
    octets = []
    while True:
        octet = cursor.read_u8()
        octets.append(octet)
        if octet & 0x01 == 0:
            break
        if len(octets) >= 3:
            break

    def _has(byte_idx: int, mask: int) -> bool:
        return len(octets) > byte_idx and bool(octets[byte_idx] & mask)

    # First byte
    if _has(0, 0x80): cursor.skip(2)  # TAG
    if _has(0, 0x40):  # CSN - Callsign (7 bytes)
        result["acid"] = cursor.read(7).decode("utf-8", errors="ignore").strip()
    if _has(0, 0x20): cursor.skip(4)  # IFI
    if _has(0, 0x10): cursor.skip(1)  # FCT
    if _has(0, 0x08): cursor.skip(4)  # TAC - Aircraft Type
    if _has(0, 0x04): cursor.skip(1)  # WTC
    if _has(0, 0x02): cursor.skip(4)  # DEP - Departure Airport

    # Second byte
    if _has(1, 0x80): cursor.skip(4)  # DST - Destination Airport
    if _has(1, 0x40):  # RDS - Runway Designator (3 bytes)
        result["runway"] = cursor.read(3).decode("utf-8", errors="ignore").strip()
    if _has(1, 0x20): cursor.skip(2)  # CFL
    if _has(1, 0x10): cursor.skip(2)  # CTL
    if _has(1, 0x08):  # TOD
        rep = cursor.read_u8()
        cursor.skip(rep * 4)
    if _has(1, 0x04): cursor.skip(6)  # AST
    if _has(1, 0x02): cursor.skip(1)  # STS

    # Third byte
    if _has(2, 0x80):  # STD - SID (7 bytes)
        result["sid"] = cursor.read(7).decode("utf-8", errors="ignore").strip()
    if _has(2, 0x40):  # STA - STAR (7 bytes)
        result["star"] = cursor.read(7).decode("utf-8", errors="ignore").strip()
    if _has(2, 0x20): cursor.skip(2)  # PEM
    if _has(2, 0x10): cursor.skip(7)  # PEC

    return result


def _parse_one_record(data: bytes, start: int, end: int) -> tuple[dict[str, Any], int]:
    """解析 CAT062 报文中的单条记录，返回 (解析结果, 下一个记录的起始位置)"""
    result: dict[str, Any] = {
        'callsign': '',
        'acid': '',
        'runway': '',
        'sid': '',
        'star': '',
        'ssr': '',
        'track_number': -1,
        'sac': 0,
        'sic': 0,
        'received_at': datetime.utcnow(),
    }

    cursor = _Cursor(data, start, end)

    try:
        fspecs = _parse_fspecs(cursor)
    except ValueError:
        return result, cursor.idx

    if not fspecs:
        return result

    fs1 = fspecs[0]
    fs2 = fspecs[1] if len(fspecs) > 1 else 0
    fs3 = fspecs[2] if len(fspecs) > 2 else 0
    fs4 = fspecs[3] if len(fspecs) > 3 else 0

    # FSPEC1 bits
    bit010  = (fs1 & 0x80) >> 7   # SAC/SIC (2 bytes)
    # bit015 = (fs1 & 0x20) >> 5   # Service Identifier (1 byte) — skip
    bit070  = (fs1 & 0x10) >> 4   # Time of Track (3 bytes, seconds/128)
    bit105  = (fs1 & 0x08) >> 3   # Lat/Lon (4+4 bytes)
    bit100  = (fs1 & 0x04) >> 2   # Cartesian position (6 bytes)
    bit185  = (fs1 & 0x02) >> 1   # Velocity (4 bytes)

    # FSPEC2 bits
    bit210  = (fs2 & 0x80) >> 7   # Calculated Ground Speed (2 bytes)
    bit060  = (fs2 & 0x40) >> 6   # SSR Mode-3/A Code (2 bytes)
    bit245  = (fs2 & 0x20) >> 5   # Target ID / callsign (1+6 bytes IA5)
    bit380  = (fs2 & 0x10) >> 4   # I062/380 Flight Plan Related Data
    bit040  = (fs2 & 0x08) >> 3   # Track Number (2 bytes)
    bit080  = (fs2 & 0x04) >> 2   # Flight Plan Correlated
    bit290  = (fs2 & 0x02) >> 1   # Mode-3/A Code Confidence

    # FSPEC3 bits
    bit200  = (fs3 & 0x80) >> 7   # Calculated Position (1 byte)
    bit295  = (fs3 & 0x40) >> 6   # Mode-3/A / Mode-C Reported
    bit136  = (fs3 & 0x20) >> 5   # Flight Level (2 bytes, *25*0.3048)
    bit130  = (fs3 & 0x10) >> 4   # Position WGS-84 (2 bytes)
    bit135  = (fs3 & 0x08) >> 3   # Geometric Altitude / QNH (2 bytes)
    bit220  = (fs3 & 0x04) >> 2   # Track Angle (2 bytes)
    bit390  = (fs3 & 0x02) >> 1   # I062/390 Flight Plan Info (runway, SID/STAR)

    # FSPEC4 bits
    # bit270 = (fs4 & 0x80) >> 7
    # bit300 = (fs4 & 0x40) >> 6
    # bit110 = (fs4 & 0x20) >> 5
    # bit120 = (fs4 & 0x10) >> 4
    # bit510 = (fs4 & 0x08) >> 3
    # bit500 = (fs4 & 0x04) >> 2
    # bit340 = (fs4 & 0x02) >> 1

    try:
        # I010: SAC/SIC
        if bit010:
            sacsic = cursor.read(2)
            result["sac"] = sacsic[0]
            result["sic"] = sacsic[1]

        # I015: skip
        if fs1 & 0x20:
            cursor.skip(1)

        # I070: Time of Track
        if bit070:
            cursor.skip(3)

        # I105: Lat/Lon
        if bit105:
            cursor.skip(8)  # 4+4 bytes

        # I100: Cartesian coordinates
        if bit100:
            cursor.skip(6)

        # I185: Velocity (vx_i16 + vy_i16 = 4 bytes)
        if bit185:
            cursor.skip(4)

        # I210: Ground Speed
        if bit210:
            cursor.skip(2)

        # I060: SSR
        if bit060:
            result["ssr"] = _read_ssr(cursor)

        # I245: Target ID (callsign from SSR)
        if bit245:
            cursor.skip(1)  # SPI flag
            callsign_raw = cursor.read(6)
            cs = _decode_ia5_callsign(callsign_raw).strip()
            if cs:
                result["callsign"] = cs

        # I380: Flight Plan Related Data (callsign from tracked target)
        if bit380:
            sub = _parse_380(cursor)
            if sub.get("callsign") and not result["callsign"]:
                result["callsign"] = sub["callsign"]

        # I040: Track Number
        if bit040:
            result["track_number"] = cursor.read_u16()

        # I080: Flight Plan Correlated
        if bit080:
            # Parse sub-FSPEC for correlated flag
            fx1 = cursor.read_u8()
            if fx1 & 0x01:
                second = cursor.read_u8()
                # remaining subfields not needed
                if second & 0x01:
                    third = cursor.read_u8()
                    if third & 0x01:
                        cursor.skip(1)

        # I290: Mode-3/A Confidence
        if bit290:
            # Parse sub-FSPEC, skip all
            octet1 = cursor.read_u8()
            fx1 = octet1 & 0x01
            if fx1:
                octet2 = cursor.read_u8()
                if octet2 & 0x80: cursor.skip(1)
                if octet2 & 0x40: cursor.skip(1)
                if octet2 & 0x20: cursor.skip(1)
            for mask, size in [(0x80, 1), (0x40, 1), (0x20, 1), (0x10, 1), (0x08, 2), (0x04, 1), (0x02, 1)]:
                if octet1 & mask:
                    cursor.skip(size)

        # I200: Calculated Position
        if bit200:
            cursor.skip(1)

        # I295: Mode-3/A / Mode-C
        if bit295:
            # Sub-FSPEC skip
            while True:
                octet = cursor.read_u8()
                if octet & 0x80: cursor.skip(1)
                if octet & 0x40: cursor.skip(1)
                if octet & 0x20: cursor.skip(1)
                if octet & 0x10: cursor.skip(1)
                if octet & 0x08: cursor.skip(1)
                if octet & 0x04: cursor.skip(1)
                if octet & 0x02: cursor.skip(1)
                if octet & 0x01 == 0:
                    break

        # I136: Flight Level
        if bit136:
            cursor.skip(2)

        # I130: Position WGS-84
        if bit130:
            cursor.skip(2)

        # I135: Geometric Altitude (QNH)
        if bit135:
            cursor.skip(2)

        # I220: Track Angle
        if bit220:
            cursor.skip(2)

        # I390: Flight Plan Info (runway, SID, STAR!)
        if bit390:
            sub = _parse_390(cursor)
            if sub.get("runway"):
                result["runway"] = sub["runway"]
            if sub.get("sid"):
                result["sid"] = sub["sid"]
            if sub.get("star"):
                result["star"] = sub["star"]
            if sub.get("acid") and not result["callsign"]:
                result["callsign"] = sub["acid"]

        # Skip remaining FSPEC4 fields (not relevant)
        # (We don't parse them since we got everything we need)

    except (ValueError, IndexError) as e:
        logger.debug("CAT062 parse field error: %s", e)

    return result, cursor.idx


def parse_datagram(payload: bytes) -> list[dict[str, Any]]:
    """解析完整 CAT062 UDP 数据报，可能包含多条记录

    返回每条记录的解析结果列表
    """
    if len(payload) < 3:
        return []
    cat = payload[0]
    if cat != 0x3E:
        return []
    declared_length = int.from_bytes(payload[1:3], "big", signed=False)
    total_length = min(len(payload), declared_length) if declared_length >= 3 else len(payload)

    records: list[dict[str, Any]] = []
    index = 3
    while index < total_length:
        try:
            record, next_index = _parse_one_record(payload, index, total_length)
        except (ValueError, IndexError):
            break
        if next_index <= index:
            break
        index = next_index
        if record.get("callsign") or record.get("ssr") or record.get("track_number", -1) >= 0:
            records.append(record)
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

            # 使用 parse_datagram 解析完整 UDP 报文中所有 CAT062 记录
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
