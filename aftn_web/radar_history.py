"""航迹历史存储 — 每天一个多成员 gzip JSONL 文件

存储解析后的单条航迹点 (parsed CAT062 records)，而非 FDR 快照。
最小存储单元：时间戳 + 呼号 + 位置 + 状态信息。

保留 90 天，自动清理过期文件。

写入策略：缓冲 5 秒或 200 条后，组装完整的独立 gzip member 写入文件。
读取时跳过损坏的最后一个 member，保证 crash-safe。
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import struct
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("aftn_web.radar_history")

# 缓冲参数
_FLUSH_INTERVAL_SECONDS = 5
_FLUSH_BATCH_SIZE = 200


def _write_gzip_member(filepath: Path, data: bytes) -> bool:
    """追加一个完整的独立 gzip member 到文件
    返回 True 表示写入成功
    """
    buf = io.BytesIO()
    try:
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(data)
        member = buf.getvalue()
        with open(str(filepath), "ab") as f:
            f.write(member)
        return True
    except Exception as exc:
        logger.error("radar history: write gzip member failed: %s", exc)
        return False


def _read_gzip_members(filepath: Path) -> list[bytes]:
    """读取 gzip 多成员文件，返回所有完整 member 的解压数据
    跳过尾部不完整 member（crash-safe）
    """
    members: list[bytes] = []
    try:
        with open(str(filepath), "rb") as raw:
            data = raw.read()
    except Exception:
        return members

    offset = 0
    while offset < len(data):
        # 检查 gzip 头部魔术字
        if data[offset:offset+2] != b'\x1f\x8b':
            break
        # 定位下一个 member 的起始位置
        # gzip header: 10 bytes fixed + optional FEXTRA/NAME/COMMENT + 8 bytes CRC
        # 简化：尝试解压当前 member，如果有错误则终止
        try:
            buf = io.BytesIO(data[offset:])
            with gzip.GzipFile(fileobj=buf) as gz:
                member_data = gz.read()
            members.append(member_data)
            offset += buf.tell()
        except Exception:
            # 最后一个 member 不完整，跳过
            if members:
                logger.debug("radar history: truncated member at offset %d, skipping", offset)
            break

    return members


class RadarHistoryStore:
    """航迹历史存储，线程安全，crash-safe"""

    def __init__(self, data_dir: str | Path, retention_days: int = 90):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._today: str | None = None
        self._buffer: list[str] = []      # JSONL lines waiting to flush
        self._last_flush = time.monotonic()
        self._current_path: Path | None = None
        self._last_cleanup = time.monotonic()

    # ── 公开接口 ──────────────────────────────────────────

    def record(self, parsed: dict[str, Any], received_at: datetime) -> None:
        """记录一条解析后的航迹点（缓冲写入）"""
        now = datetime.utcnow()
        today = now.strftime("%Y%m%d")

        ts = received_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{received_at.microsecond // 1000:03d}Z"
        point = {
            "ts": ts,
            "cs": (parsed.get("callsign") or "").strip(),
            "ss": (parsed.get("ssr") or "").strip(),
            "lt": parsed.get("latitude", 0.0),
            "ln": parsed.get("longitude", 0.0),
            "fl": parsed.get("flight_level", 0.0),
            "hd": parsed.get("heading", 0.0),
            "sp": parsed.get("speed", 0.0),
            "ap": (parsed.get("adep") or "").strip().upper(),
            "ad": (parsed.get("adest") or "").strip().upper(),
            "rw": (parsed.get("runway") or "").strip(),
            "fp": (parsed.get("flight_procedure") or "").strip(),
        }

        line = json.dumps(point, ensure_ascii=False, separators=(",", ":")) + "\n"

        with self._lock:
            if today != self._today:
                self._rotate(today)

            self._buffer.append(line)

            # 达到阈值 | 超过间隔 → 刷盘
            now_ts = time.monotonic()
            if (len(self._buffer) >= _FLUSH_BATCH_SIZE
                    or now_ts - self._last_flush >= _FLUSH_INTERVAL_SECONDS):
                self._flush_nolock()

    def query(
        self,
        ts_from: str,
        ts_to: str,
        callsign: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询时间范围内的航迹点"""
        from_d = ts_from[:10].replace("-", "")
        to_d = ts_to[:10].replace("-", "")
        results: list[dict[str, Any]] = []

        files = sorted(self.data_dir.glob("radar_*.jsonl.gz"))
        for f in files:
            name = f.stem  # radar_20260526 or radar_20260502.jsonl
            date_part = name.split("_", 1)[1].split(".")[0] if "_" in name else ""
            if date_part < from_d or date_part > to_d:
                continue
            if date_part == from_d:
                self._scan_file(f, ts_from, ts_to, callsign, results)
            elif date_part == to_d:
                self._scan_file(f, ts_from, ts_to, callsign, results)
            else:
                self._scan_file(f, None, None, callsign, results)

        return results

    def get_time_range(self) -> tuple[Optional[str], Optional[str]]:
        """获取可用历史数据的时间范围"""
        files = sorted(self.data_dir.glob("radar_*.jsonl.gz"))
        if not files:
            return None, None

        first_ts: str | None = None
        last_ts: str | None = None

        # 第一个 member 的第一条有效记录
        try:
            members = _read_gzip_members(files[0])
            if members:
                for line in members[0].decode("utf-8").split("\n"):
                    line = line.strip()
                    if line:
                        try:
                            first_ts = json.loads(line).get("ts", "")
                        except json.JSONDecodeError:
                            pass
                        break
        except Exception:
            pass

        # 最后一个 member 的最后一条有效记录
        try:
            members = _read_gzip_members(files[-1])
            if members:
                last_member = members[-1]
                last_line = ""
                for ln in last_member.decode("utf-8").split("\n"):
                    ln = ln.strip()
                    if ln:
                        last_line = ln
                if last_line:
                    try:
                        last_ts = json.loads(last_line).get("ts", "")
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass

        return first_ts, last_ts

    def flush(self) -> None:
        """强制刷盘"""
        with self._lock:
            self._flush_nolock()

    def close(self) -> None:
        """关闭并刷盘"""
        with self._lock:
            self._flush_nolock()
            self._buffer = []
            self._today = None
            self._current_path = None

    # ── 内部 ──────────────────────────────────────────────

    def _rotate(self, today: str) -> None:
        """切换日期文件"""
        self._flush_nolock()
        self._current_path = self.data_dir / f"radar_{today}.jsonl.gz"
        self._today = today
        logger.info("radar history: rotated to %s", self._current_path.name)

        # 每小时检查过期清理
        now_ts = time.monotonic()
        if now_ts - self._last_cleanup > 3600:
            self._last_cleanup = now_ts
            self._cleanup_old()

    def _flush_nolock(self) -> None:
        """将缓冲写入文件（持有锁时调用）"""
        if not self._buffer or not self._current_path:
            return
        data = "".join(self._buffer).encode("utf-8")
        if _write_gzip_member(self._current_path, data):
            self._buffer = []
            self._last_flush = time.monotonic()
        else:
            logger.warning("radar history: flush failed, buffer kept (%d lines)", len(self._buffer))

    def _cleanup_old(self) -> None:
        """删除过期文件"""
        cutoff = datetime.utcnow() - timedelta(days=self.retention_days)
        cutoff_str = cutoff.strftime("%Y%m%d")
        removed = 0
        for f in self.data_dir.glob("radar_*.jsonl.gz"):
            try:
                name = f.stem
                date_part = name.split("_", 1)[1].split(".")[0] if "_" in name else ""
                if date_part < cutoff_str and date_part.isdigit():
                    f.unlink()
                    removed += 1
            except Exception:
                pass
        if removed:
            logger.info("radar history: cleaned %d old files (retention=%dd)", removed, self.retention_days)

    def _scan_file(
        self,
        path: Path,
        ts_from: str | None,
        ts_to: str | None,
        callsign: str | None,
        results: list[dict[str, Any]],
    ) -> None:
        """扫描单个文件的所有完整 member"""
        try:
            members = _read_gzip_members(path)
        except Exception as exc:
            logger.warning("radar history: scan %s failed: %s", path.name, exc)
            return

        for member_data in members:
            for line in member_data.decode("utf-8").split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    pt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = pt.get("ts", "")
                if ts_from and ts < ts_from:
                    continue
                if ts_to and ts > ts_to:
                    continue
                if callsign and pt.get("cs", "").upper() != callsign.upper():
                    continue
                results.append(pt)
