"""ASR 语音识别结果接收器 — 组播 JSON 文本接收与存储"""

from __future__ import annotations

import json
import logging
import socket
import sys
import threading
import time
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("aftn_web.asr")


class AsrReceiver:
    """ASR 语音识别文本接收器 — 接收组播 JSON 并存入数据库"""

    def __init__(
        self,
        multicast_group: str,
        port: int,
        interface_ip: str,
        db: Any = None,
    ):
        self.multicast_group = multicast_group
        self.port = port
        self.interface_ip = interface_ip
        self._db = db
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

        # 内存缓存：每个扇区最新的 ASR 文本
        # sector -> dict of latest ASR data
        self._latest_asr: dict[str, dict] = {}
        self._asr_lock = threading.Lock()

        # 统计
        self._total_received = 0
        self._total_parsed = 0
        self._total_errors = 0

        # 启动时回填已有记录的 wavbegintime
        if self._db:
            try:
                self._db.backfill_asr_wavbegintime()
            except Exception:
                logger.exception("ASR wavbegintime backfill error")

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # 创建 UDP socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)  # 1MB 缓冲

        try:
            self._socket.bind(("", self.port))
        except OSError as e:
            logger.error("ASR bind port %d failed: %s", self.port, e)
            self._running = False
            return

        # 加入组播
        try:
            group_bytes = socket.inet_aton(self.multicast_group)
            if self.interface_ip and self.interface_ip != "0.0.0.0":
                if sys.platform == "linux":
                    self._socket.setsockopt(
                        socket.IPPROTO_IP,
                        socket.IP_MULTICAST_IF,
                        socket.inet_aton(self.interface_ip),
                    )
                mreq = group_bytes + socket.inet_aton(self.interface_ip)
            else:
                mreq = group_bytes + socket.inet_aton("0.0.0.0")
            self._socket.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq
            )
            self._socket.settimeout(0.5)
            logger.info(
                "ASR receiver joined %s:%d (if=%s)",
                self.multicast_group, self.port, self.interface_ip,
            )
        except Exception as e:
            logger.error("ASR multicast join failed: %s", e)
            self._running = False
            return

        # 启动接收线程
        self._thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="asr-receiver"
        )
        self._thread.start()
        logger.info("ASR receiver started")

    def stop(self) -> None:
        self._running = False
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        logger.info("ASR receiver stopped (total: recv=%d, parsed=%d, errors=%d)",
                     self._total_received, self._total_parsed, self._total_errors)

    # ── 公共状态查询 ──────────────────────────────────

    def get_latest_asr(self, sector: str | None = None) -> dict | None:
        """获取指定扇区的最新 ASR 文本，或不指定扇区时返回全部"""
        with self._asr_lock:
            if sector:
                return self._latest_asr.get(sector)
            return dict(self._latest_asr)

    def get_latest_asr_all(self) -> dict[str, dict]:
        """返回所有扇区的最新 ASR 文本"""
        with self._asr_lock:
            return dict(self._latest_asr)

    def get_stats(self) -> dict:
        return {
            "total_received": self._total_received,
            "total_parsed": self._total_parsed,
            "total_errors": self._total_errors,
        }

    # ── 内部方法 ──────────────────────────────────────────

    def _receive_loop(self) -> None:
        import select as _select

        sock = self._socket
        if not sock:
            return

        poller = _select.poll()
        poller.register(sock, _select.POLLIN)

        while self._running:
            try:
                events = poller.poll(500)
            except Exception:
                if self._running:
                    logger.exception("ASR select error")
                continue

            if not events:
                continue

            try:
                data, addr = sock.recvfrom(65535)
            except Exception:
                continue

            self._total_received += 1

            try:
                # 尝试解析 JSON
                text = data.decode("utf-8", errors="replace").strip()
                if not text:
                    continue

                # 兼容纯 JSON 和带多余字符的情况
                # 找到第一个 { 和最后一个 }
                first_brace = text.find("{")
                last_brace = text.rfind("}")
                if first_brace >= 0 and last_brace > first_brace:
                    text = text[first_brace:last_brace + 1]

                payload = json.loads(text)
                self._process_asr_payload(payload)
            except json.JSONDecodeError:
                self._total_errors += 1
                logger.debug("ASR JSON parse error from %s: %s",
                             addr, data[:200])
            except Exception:
                self._total_errors += 1
                logger.exception("ASR processing error from %s", addr)

    @staticmethod
    def _extract_wavbegintime(processedCommand: str) -> str:
        """从 processedCommand 文本中提取语音开始时间

        预期格式（在文本开头）:
          [HH:mm:ss] 或 [HH:mm] → 如 [10:14:23]文字
          HH:mm:ss → 纯时间开头
        取当天 UTC 日期拼成 YYYY-MM-DD HH:mm:ss
        """
        if not processedCommand:
            return ""
        import re

        # 格式1: [HH:mm:ss] 或 [HH:mm]
        m = re.match(r'^\[(\d{2}:\d{2}(?::\d{2})?)\]', processedCommand)
        if m:
            time_part = m.group(1)
            if len(time_part) == 5:
                time_part += ":00"
            from datetime import datetime as _dt
            now = _dt.utcnow()
            return f"{now.strftime('%Y-%m-%d')} {time_part}"

        # 格式2: HH:mm:ss 开头（无括号）
        m = re.match(r'^(\d{2}:\d{2}:\d{2})\s', processedCommand)
        if m:
            from datetime import datetime as _dt
            now = _dt.utcnow()
            return f"{now.strftime('%Y-%m-%d')} {m.group(1)}"

        # 格式3: 完整日期时间 YYYY-MM-DD HH:mm:ss 开头
        m = re.match(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', processedCommand)
        if m:
            return m.group(1)

        return ""

    def _process_asr_payload(self, payload: dict) -> None:
        """处理单条 ASR 文本

        预期字段：wavbegintime, processedCommand, callsign, sector, speaker, duration, wavfilepath
        """
        wavbegintime = str(payload.get("wavbegintime") or "")
        processedCommand = str(payload.get("processedCommand") or "")
        callsign = str(payload.get("callsign") or "").upper()
        sector = str(payload.get("sector") or "").upper()
        speaker = str(payload.get("speaker") or "")
        duration = float(payload.get("duration") or 0)
        wavfilepath = str(payload.get("wavfilepath") or "")

        if not sector:
            logger.debug("ASR: 缺少 sector 字段，忽略")
            return

        # 如果 JSON 中的 wavbegintime 为空，尝试从 processedCommand 文本中提取
        if not wavbegintime:
            wavbegintime = self._extract_wavbegintime(processedCommand)

        self._total_parsed += 1

        # ── 更新内存缓存 ──
        entry = {
            "wavbegintime": wavbegintime,
            "processedCommand": processedCommand,
            "callsign": callsign,
            "sector": sector,
            "speaker": speaker,
            "duration": duration,
            "wavfilepath": wavfilepath,
        }
        with self._asr_lock:
            self._latest_asr[sector] = entry

        # ── 存入数据库 ──
        if self._db:
            try:
                self._db.save_asr_text(
                    wavbegintime=wavbegintime,
                    processedCommand=processedCommand,
                    callsign=callsign,
                    sector=sector,
                    speaker=speaker,
                    duration=duration,
                    wavfilepath=wavfilepath,
                )
            except Exception:
                logger.exception("ASR DB 存储错误")

        # 日志采样
        if self._total_parsed <= 5 or self._total_parsed % 100 == 0:
            logger.info(
                "[ASR] %s/%s: %s <- %s (total: recv=%d, parsed=%d)",
                sector, callsign,
                processedCommand[:60],
                wavbegintime,
                self._total_received, self._total_parsed,
            )
