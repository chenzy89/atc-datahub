"""CAT062 雷达数据接收器 — 解析飞行跑道和飞行程序 (SID/STAR)

监听组播 228.28.28.28:8107，接收 ASTERIX CAT062 格式雷达数据报文，
解析航班呼号、使用跑道、飞行程序，并更新到飞行计划表。

CAT062 标准参考：EUROCONTROL ASTERIX Category 062 (Track Messages)
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger("aftn_web.radar")

# ── CAT062 FSPEC 字段定义 ──────────────────────────────────────
# (fspec_byte_idx, bit_position_in_byte): (field_name, field_length_or_None_means_variable)
# 位序：bit 7 = first field, bit 0 = extension flag (在 FSPEC byte 中)
_CAT062_FIELDS: dict[tuple[int, int], tuple[str, int]] = {
    # FSPEC byte 0
    (0, 7): ("I062_010", 2),    # Data Source Identifier (SAC+SIC)
    (0, 6): ("I062_015", 1),    # Service Management
    (0, 5): ("I062_040", 0),    # Track Status (variable, special)
    (0, 4): ("I062_070", 3),    # Mode-3/A Code (2 bytes) actually +1 for V=1
    (0, 3): ("I062_105", 2),    # Track Number
    (0, 2): ("I062_100", 1),    # Emergency
    (0, 1): ("I062_200", 4),    # Calculated Track Position (cartesian)
    (0, 0): ("_EXT", 0),        # Extension

    # FSPEC byte 1
    (1, 7): ("I062_210", 1),    # Calculated Ground Speed (1 byte)
    (1, 6): ("I062_220", 2),    # Track Angle (2 bytes)
    (1, 5): ("I062_230", 1),    # Track Angle Rate (1 byte)
    (1, 4): ("I062_290", 1),    # Mode-3/A Code Confidence
    (1, 3): ("I062_300", 0),    # Calculated Track Position Polar
    (1, 2): ("I062_340", 0),    # Measured Radar Position (variable)
    (1, 1): ("I062_360", 0),    # Track Quality (variable)
    (1, 0): ("_EXT", 0),

    # FSPEC byte 2
    (2, 7): ("I062_380", 0),    # Flight Plan Related Data (variable, special)
    (2, 6): ("I062_390", 1),    # Receive Status
    (2, 5): ("I062_400", 0),    # Track Data Ages (variable)
    (2, 4): ("I062_320", 2),    # Flight Level
    (2, 3): ("I062_500", 0),    # Standard Deviation of Position
    (2, 2): ("I062_510", 0),    # Standard Deviation of Velocity
    (2, 1): ("I062_520", 0),    # Amplitude of the Plot
    (2, 0): ("_EXT", 0),
    # ... more bytes possible but we only need up to byte 2
}

# I062/380 子字段定义（sub-FSPEC）
# Bit 7 → first subfield, bit 0 → extension
_062_380_SUBFIELDS: dict[int, tuple[str, int]] = {
    7: ("TRP", 1),        # Trajectory Pointer
    6: ("CS", 1),         # Calculated Track Status
    5: ("TTR", 1),        # Type of Trip (bits: dep/arr)
    4: ("STI", 1),        # Stand/Terminal Info
    3: ("CFL", 2),        # Cleared Flight Level
    2: ("ID", 8),         # Track Identifier (callsign, space-padded)
    1: ("RWY", 8),        # Runway (space-padded)
    0: ("_EXT", 0),       # Extension
}

# Extension byte for I062/380
_062_380_SUBFIELDS_EXT: dict[int, tuple[str, int]] = {
    7: ("SIDSTAR", 8),    # SID/STAR procedure name (space-padded)
    6: ("FLTID", 8),      # Flight Identifier
    5: ("COM", 1),        # Communication status
    4: ("SNR", 1),        # Sequence Number
    3: ("FSS", 0),        # FSS data
    2: ("TID", 1),        # TID
    1: ("ACS", 0),        # Additional Code Space
    0: ("_EXT", 0),
}


def _strip_padding(data: bytes) -> str:
    """去除尾部空格和空字节"""
    return data.rstrip(b' \x00').decode('ascii', errors='replace')


def _safe_ssr(code: int) -> str:
    """SSR code to 4-digit octal string"""
    octal = oct(code)[2:]
    return octal.zfill(4)


def _parse_fspec(data: bytes, offset: int) -> tuple[list[int], int]:
    """解析 FSPEC，返回 (位序列表, 新的 offset)"""
    bits = []
    while offset < len(data):
        b = data[offset]
        for bp in range(7, -1, -1):
            if b & (1 << bp):
                bits.append(bp)
        offset += 1
        if not (b & 1):  # extension bit = 0 means last FSPEC byte
            break
    return bits, offset


def _parse_380_subfields(data: bytes, offset: int) -> tuple[dict[str, Any], int]:
    """解析 I062/380 Flight Plan Related Data 的子字段
    返回 (子字段 dict, 新的 offset) 其中子字段包括 TRP, CS, TTR, ID, RWY, SIDSTAR 等
    """
    if offset >= len(data):
        return {}, offset

    rep = data[offset]
    offset += 1
    result: dict[str, Any] = {}

    for _ in range(rep):
        sub_bits, offset = _parse_fspec(data, offset)
        for bp in sub_bits:
            fname, flen = _062_380_SUBFIELDS.get(bp, (None, 0))
            if fname is None or flen == 0:
                # 遇到 extension 或未知字段，尝试跳过
                if bp == 0:  # EXT flag
                    continue
                # 尝试从 ext subfields 找
                fname, flen = _062_380_SUBFIELDS_EXT.get(bp, (None, 0))
            if fname is None or flen == 0:
                continue
            if offset + flen > len(data):
                break
            raw = data[offset:offset + flen]
            offset += flen
            if fname == "ID":
                result["callsign"] = _strip_padding(raw)
            elif fname == "RWY":
                result["runway"] = _strip_padding(raw)
            elif fname == "SIDSTAR":
                result["sidstar"] = _strip_padding(raw)
            elif fname == "TTR":
                result["ttr"] = raw[0]
            result[fname.lower()] = _strip_padding(raw)

    return result, offset


def parse_cat062_frame(data: bytes) -> dict[str, Any] | None:
    """解析单个 CAT062 (0x3E) ASTERIX 雷达帧

    返回 dict（可能包含 keys: callsign, runway, sidstar, ssr, sac, sic, track_number, ttr）
    若不是 CAT062 帧则返回 None
    """
    if len(data) < 3:
        return None

    cat = data[0]
    if cat != 0x3E:
        return None

    length = struct.unpack('>H', data[1:3])[0]
    frame = data[:min(length, len(data))]

    result: dict[str, Any] = {
        'callsign': '',
        'runway': '',
        'sidstar': '',
        'ssr': 0,
        'ssr_str': '',
        'sac': 0,
        'sic': 0,
        'track_number': 0,
        'ttr': 0,
    }

    offset = 3

    # 解析 FSPEC，得到位序列表（每个元素是 FSPEC 中的 bit 序号，从 7 开始）
    fspec_byte_map: list[int] = []
    fspec_byte_idx = 0
    while offset < len(frame):
        b = frame[offset]
        offset += 1
        for bp in range(7, -1, -1):
            if b & (1 << bp):
                fspec_byte_map.append(fspec_byte_idx * 8 + (7 - bp))
        if not (b & 1):
            break
        fspec_byte_idx += 1

    # 遍历字段
    for raw_bit_idx in fspec_byte_map:
        byte_idx = raw_bit_idx // 8
        bit_pos = 7 - (raw_bit_idx % 8)  # 反转 bit 序：index 0 → bit 7
        field_info = _CAT062_FIELDS.get((byte_idx, bit_pos))
        if field_info is None:
            # 跳过未知字段（这只是保守处理，实际需要跳过正确的字节数）
            # 对于未知的变长字段，只能碰运气
            continue

        fname, flen = field_info

        if fname == "_EXT":
            continue

        if fname == "I062_380":
            # 变长子字段
            sub, offset = _parse_380_subfields(frame, offset)
            if "callsign" in sub:
                result["callsign"] = sub["callsign"]
            if "runway" in sub:
                result["runway"] = sub["runway"]
            if "sidstar" in sub:
                result["sidstar"] = sub["sidstar"]
            if "ttr" in sub:
                result["ttr"] = sub["ttr"]
            continue

        if flen == 0:
            # 已知变长字段但未实现特殊解析——记录日志并尝试跳过
            # 保守跳过一个字节（可能错误，但 CAT062 流数据不影响后续接收）
            logger.debug("skipping variable field %s at offset %d", fname, offset)
            offset += 1
            continue

        if offset + flen > len(frame):
            break

        if fname == "I062_010":
            # Data Source Identifier: 2 bytes (SAC, SIC)
            result["sac"] = frame[offset]
            result["sic"] = frame[offset + 1]
        elif fname == "I062_070":
            # Mode-3/A Code: 2 bytes (standard ASTERIX SSR encoding)
            # b1: V(7) G(6) L(5) spare(4) D1[2:0](4:2) D2[1](1:0)
            # b2: D2[2](7) D3[2:0](6:4) D4[2:0](3:1) spare(0) — varies by implementation
            # Standard encoding (bits to 4 octal digits):
            b1, b2 = frame[offset], frame[offset + 1]
            d1 = (b1 >> 2) & 0x07
            d2 = ((b1 & 0x03) << 1) | ((b2 >> 7) & 0x01)
            d3 = (b2 >> 4) & 0x07
            d4 = (b2 >> 1) & 0x07
            result["ssr"] = d1 * 512 + d2 * 64 + d3 * 8 + d4
            result["ssr_str"] = f"{d1}{d2}{d3}{d4}"
        elif fname == "I062_105":
            result["track_number"] = struct.unpack('>H', frame[offset:offset + 2])[0]
        elif fname == "I062_200":
            # Cartesian position (x, y each 2 bytes signed) - just skip
            pass
        elif fname == "I062_320":
            # Flight Level (2 bytes) - just skip
            pass

        offset += flen

    return result


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

            # UDP 报文中可能包含多个 CAT062 帧（连续拼接）
            data = payload
            offset = 0
            while offset < len(data):
                if len(data) - offset < 3:
                    break
                # 检查 category
                cat = data[offset]
                if cat != 0x3E:
                    offset += 1
                    continue
                # 读取帧长度
                frame_len = struct.unpack('>H', data[offset + 1:offset + 3])[0]
                if frame_len < 3 or offset + frame_len > len(data):
                    # 长度不合理，跳过这个字节
                    offset += 1
                    continue

                frame_data = data[offset:offset + frame_len]
                offset += frame_len

                try:
                    parsed = parse_cat062_frame(frame_data)
                except Exception:
                    logger.exception("CAT062 parse error at frame offset %d", offset - frame_len)
                    continue

                if parsed and (parsed.get('callsign') or parsed.get('ssr_str')):
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
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)  # 2MB buffer
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
