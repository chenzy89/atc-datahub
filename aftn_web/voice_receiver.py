"""语音数据接收器 — 组播 ADPCM 音频接收、播放与文件存储"""

from __future__ import annotations

import base64
import logging
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

# 尝试从 online_auio 虚拟环境导入 pygame
_VENV_PYGAME = "/home/share/online_auio/venv/lib/python3.8/site-packages"
if os.path.isdir(_VENV_PYGAME):
    sys.path.insert(0, _VENV_PYGAME)
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("aftn_web.voice")

# ADPCM 常量
T_STEP = [
    7, 8, 9, 10, 11, 12, 13, 14,
    16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66,
    73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307,
    337, 371, 408, 449, 494, 544, 598, 658,
    724, 796, 876, 963, 1060, 1166, 1282, 1411,
    1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024,
    3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484,
    7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
    32767,
]
T_INDEX = [
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
]
BLOCK_SIZE = 256

# 扇区 → 内话通道号映射
SECTOR_CHANNELS: dict[str, int] = {
    "ZGJDTM01": 38,
    "ZGJDTM02": 42,
    "ZGJDTM03": 50,
    "ZGJDTM04": 264,
    "ZGJDTM05": 46,
    "ZGJDTM06": 32,
    "ZGJDTM07": 54,
}

# 通道号 → 扇区名
CHANNEL_SECTORS: dict[int, str] = {v: k for k, v in SECTOR_CHANNELS.items()}


@dataclass
class ChannelStatus:
    """单个通道的状态"""
    sector_code: str           # ZGJDTM01
    channel_id: int            # 内话通道号 38, 42, …
    frequency: str             # 频率如 "120.35"
    sector_name: str           # 扇区名如 HN
    last_activity: float = 0.0 # time.monotonic() 最后收到数据时间
    bytes_received: int = 0    # 累计接收字节数
    bytes_saved: int = 0       # 已保存到文件的字节数
    active: bool = False       # 最近 3 秒是否有数据
    selected: bool = False     # 是否被选中播放

    def to_dict(self) -> dict:
        return {
            "sector_code": self.sector_code,
            "channel_id": self.channel_id,
            "frequency": self.frequency,
            "sector_name": self.sector_name,
            "last_activity": self.last_activity,
            "bytes_received": self.bytes_received,
            "bytes_saved": self.bytes_saved,
            "active": self.active,
            "selected": self.selected,
        }


class ADPCMDecoder:
    """IMA ADPCM 解码器 — 从 online_auio 项目移植"""

    def __init__(self):
        self.decoder_predicted = 0
        self.decoder_index = 0
        self.decoder_step = 7

    def decode_adpcm(self, adpcm_data: bytes) -> bytes:
        pcm_frames = bytearray()
        i = 0
        while i < len(adpcm_data):
            end = min(i + BLOCK_SIZE, len(adpcm_data))
            block = adpcm_data[i:end]
            if len(block) != BLOCK_SIZE:
                break
            try:
                pcm_block = self.decode_block(block)
                pcm_frames.extend(pcm_block)
            except Exception:
                pass
            i += BLOCK_SIZE
        return bytes(pcm_frames)

    def decode_block(self, block: bytes) -> bytes:
        if len(block) != BLOCK_SIZE:
            raise ValueError(f"块大小必须为256，实际{len(block)}")
        result = bytearray()
        self.decoder_predicted = struct.unpack('<h', block[0:2])[0]
        self.decoder_index = block[2] & 0xFF
        self.decoder_step = T_STEP[self.decoder_index]
        result.extend(block[0:2])
        for i in range(4, len(block)):
            original_sample = block[i] & 0xFF
            second_sample = original_sample >> 4
            first_sample = original_sample & 0x0F
            first_pcm = self.decode_sample(first_sample)
            second_pcm = self.decode_sample(second_sample)
            result.extend(struct.pack('<h', first_pcm))
            result.extend(struct.pack('<h', second_pcm))
        return bytes(result)

    def decode_sample(self, nibble: int) -> int:
        sign = nibble & 8
        delta = nibble & 7
        difference = self.decoder_step >> 3
        if delta & 4:
            difference += self.decoder_step
        if delta & 2:
            difference += self.decoder_step >> 1
        if delta & 1:
            difference += self.decoder_step >> 2
        if sign:
            difference = -difference
        self.decoder_predicted += difference
        if self.decoder_predicted > 32767:
            self.decoder_predicted = 32767
        elif self.decoder_predicted < -32768:
            self.decoder_predicted = -32768
        self.decoder_index += T_INDEX[nibble]
        self.decoder_index = max(0, min(88, self.decoder_index))
        self.decoder_step = T_STEP[self.decoder_index]
        return self.decoder_predicted


def _get_frequency(sector_code: str) -> str:
    """从 SectorInfo 配置获取频率"""
    freqs = {
        "ZGJDTM01": "120.35",
        "ZGJDTM02": "121.4",
        "ZGJDTM03": "119.9",
        "ZGJDTM04": "123.85",
        "ZGJDTM05": "119.55",
        "ZGJDTM06": "119.025",
        "ZGJDTM07": "127.95",
    }
    return freqs.get(sector_code, "")


def _get_sector_name(sector_code: str) -> str:
    names = {
        "ZGJDTM01": "HN",
        "ZGJDTM02": "HE",
        "ZGJDTM03": "ARW",
        "ZGJDTM04": "AS",
        "ZGJDTM05": "AD",
        "ZGJDTM06": "ASL",
        "ZGJDTM07": "ARE/AA",
    }
    return names.get(sector_code, "")


def make_wav(pcm16_data: bytes, sample_rate: int = 8000) -> bytes:
    """将 PCM16 数据包装为 WAV 文件"""
    num_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    data_size = len(pcm16_data)

    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, num_channels,
        sample_rate, byte_rate, block_align,
        bits_per_sample,
        b'data', data_size,
    )
    return header + pcm16_data


class VoiceReceiver:
    """语音数据接收器 — 组播接收、解码、播放、文件存储"""

    def __init__(
        self,
        multicast_group: str,
        port: int,
        interface_ip: str,
        sector_channels: dict[str, int] | None = None,
        db: Any = None,
        save_dir: str = "",
        retention_days: int = 30,
    ):
        self.multicast_group = multicast_group
        self.port = port
        self.interface_ip = interface_ip
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._db = db
        self._save_dir = save_dir
        self._retention_days = retention_days

        # 每个通道一个解码器（ADPCM 解码有状态）
        self._decoders: dict[int, ADPCMDecoder] = {}

        # 通道状态 — 按扇区代码索引
        channels = sector_channels or SECTOR_CHANNELS
        self._status: dict[str, ChannelStatus] = {}
        for sector_code, ch_id in channels.items():
            self._status[sector_code] = ChannelStatus(
                sector_code=sector_code,
                channel_id=ch_id,
                frequency=_get_frequency(sector_code),
                sector_name=_get_sector_name(sector_code),
            )

        # 被选中的播放通道 (None = 不播放)
        self._playing_channel: Optional[int] = None
        self._play_lock = threading.Lock()
        self._pygame_ok = False
        self._pygame_mixer_inited = False

        # PCM 流缓冲（用于浏览器 SSE 流式播放）
        # channel_id -> deque of (seq, pcm_bytes)
        self._pcm_buffers: dict[int, deque] = {}
        self._pcm_events: dict[int, threading.Event] = {}
        self._pcm_seq: dict[int, int] = {}

        # 最近 3 秒内各通道的活跃标记（用于 Active 判定）
        self._activity_window: dict[int, list[float]] = {}

        # ── 通话时长统计 ──
        self._duration_buckets: dict[str, dict[int, list[float]]] = {}
        self._burst_start: dict[int, float] = {}
        self._last_data_time: dict[int, float] = {}
        self._silence_threshold = 1.0  # 1 秒无数据视为一句话说完
        self._today_date = datetime.now().strftime("%Y-%m-%d")
        self._duration_lock = threading.Lock()
        self._db_flush_counter = 0

        # ── 语音文件存储 ──
        # 当前突发缓冲：channel_id -> bytearray(adpcm_data)
        self._burst_buffers: dict[int, bytearray] = {}
        # 当前突发起始时间戳：channel_id -> datetime
        self._burst_start_ts: dict[int, datetime] = {}
        # 当前突发包计数（用于生成文件名序号）
        self._burst_seq: dict[int, int] = {}
        # 每日每个通道已生成的文件数
        self._daily_file_count: dict[str, dict[int, int]] = {}
        self._file_lock = threading.Lock()
        # 最后一次清理检查的日期
        self._last_cleanup_date = ""

        # 从 DB 恢复已有数据
        self._load_from_db()

        # 迁移旧版文件名到新格式
        self._migrate_old_files()

    # ── 公共属性 ──────────────────────────────────────

    @property
    def retention_days(self) -> int:
        return self._retention_days

    # ── 生命周期 ──────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # 尝试初始化 pygame
        self._init_pygame()

        # 创建 UDP socket
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("", self.port))

        # 加入组播
        try:
            import sys
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
                "voice receiver joined %s:%d (if=%s)",
                self.multicast_group, self.port, self.interface_ip,
            )
        except Exception as e:
            logger.error("voice multicast join failed: %s", e)
            self._running = False
            return

        # 启动接收线程
        self._thread = threading.Thread(
            target=self._receive_loop, daemon=True, name="voice-receiver"
        )
        self._thread.start()
        logger.info("voice receiver started")

    def stop(self) -> None:
        self._running = False
        # 保存所有未写入的突发缓冲
        self._flush_all_bursts()
        if self._socket:
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        if self._pygame_mixer_inited:
            try:
                import pygame
                pygame.mixer.quit()
            except Exception:
                pass
        logger.info("voice receiver stopped")

    # ── 公共状态查询 ──────────────────────────────────────

    def get_status(self) -> list[dict]:
        """返回所有通道的状态列表"""
        now = time.monotonic()
        result = []
        for sector_code, st in self._status.items():
            ch_id = st.channel_id
            # 更新 active 状态：3 秒内有数据就标记为活跃
            recent = self._activity_window.get(ch_id, [])
            recent = [t for t in recent if now - t < 3.0]
            self._activity_window[ch_id] = recent
            st.active = len(recent) > 0
            st.selected = (self._playing_channel == ch_id)
            # 更新已保存字节数
            st.bytes_saved = self._get_channel_bytes_saved(ch_id)
            result.append(st.to_dict())
        self._flush_stale_bursts()
        return result

    def get_channel_duration(self, date_str: str, channel_id: int) -> list[float]:
        """返回指定日期指定通道的 144 个 10 分钟时段的通话秒数"""
        with self._duration_lock:
            day_data = self._duration_buckets.get(date_str, {})
            return day_data.get(channel_id, [0.0] * 144)

    def get_playing_channel(self) -> Optional[int]:
        return self._playing_channel

    def select_channel(self, channel_id: int) -> bool:
        """选择播放通道。ch 为 -1 表示停止播放。"""
        valid = list(SECTOR_CHANNELS.values())
        if channel_id != -1 and channel_id not in valid:
            return False

        with self._play_lock:
            if channel_id == -1:
                if self._pygame_mixer_inited:
                    try:
                        import pygame
                        pygame.mixer.stop()
                    except Exception:
                        pass
                self._playing_channel = None
                logger.info("voice playback stopped")
            else:
                self._playing_channel = channel_id
                logger.info("voice playback selected channel %d", channel_id)
        return True

    def is_running(self) -> bool:
        return self._running

    # ── 语音文件 API ──────────────────────────────────────

    def get_channel_save_dir(self, channel_id: int) -> str:
        """返回指定通道的语音文件保存目录"""
        if not self._save_dir:
            return ""
        return str(Path(self._save_dir) / str(channel_id))

    def get_save_dir(self) -> str:
        return self._save_dir

    def list_recordings(self, date_str: str, channel_id: int) -> list[dict]:
        """列出指定日期和通道的所有录音文件"""
        recordings = []
        date_dir = Path(self._save_dir) / str(channel_id) / date_str
        if not date_dir.is_dir():
            return recordings

        try:
            for fpath in sorted(date_dir.iterdir()):
                if fpath.suffix == ".adpcm" and fpath.is_file():
                    # 文件名格式: HHMMSS_NNN.adpcm (UTC时间)
                    # 或兼容旧格式: HHMMSS_fff_NNN.adpcm
                    name = fpath.stem
                    parts = name.split("_")
                    if len(parts) >= 3:
                        # 旧格式: HHMMSS_fff_NNN
                        start_ts = parts[0]
                    elif len(parts) >= 2:
                        # 新格式: HHMMSS_NNN
                        start_ts = parts[0]
                    else:
                        start_ts = "000000"
                    file_size = fpath.stat().st_size
                    # ADPCM 时长 = 文件大小 / 块大小 * 255 / 8 * 1000 ms... 近似
                    # 每个256字节块产生255个字节PCM（128个16bit样本），8000Hz
                    # 每个块时间 = 128/8000 = 16ms
                    num_blocks = file_size // BLOCK_SIZE
                    dur_seconds = round(num_blocks * 128 / 8000, 1)

                    recordings.append({
                        "filename": fpath.name,
                        "start_time": start_ts,
                        "size": file_size,
                        "size_str": self._format_size(file_size),
                        "duration": dur_seconds,
                    })
        except OSError:
            pass
        return recordings

    def list_dates(self, channel_id: int) -> list[str]:
        """列出指定通道下有哪些日期有录音文件"""
        ch_dir = Path(self._save_dir) / str(channel_id)
        if not ch_dir.is_dir():
            return []
        dates = []
        try:
            for entry in sorted(ch_dir.iterdir()):
                if entry.is_dir():
                    # 验证是 YYYY-MM-DD 格式
                    name = entry.name
                    parts = name.split("-")
                    if len(parts) == 3 and all(p.isdigit() for p in parts):
                        dates.append(name)
        except OSError:
            pass
        return dates

    def get_recording_data(self, date_str: str, channel_id: int,
                            from_time: str = "", duration_minutes: int = 10) -> bytes:
        """获取指定时间范围的录音 ADPCM 数据，解码为 WAV PCM16

        Args:
            date_str: UTC 日期 "YYYY-MM-DD"
            channel_id: 通道号
            from_time: 起始时间 (UTC)，格式 "HH:MM" 或 "HH:MM:SS" 或 "HHMMSS"
            duration_minutes: 时长（分钟），默认10分钟
        """
        recordings = self.list_recordings(date_str, channel_id)

        # 计算 UTC 时间范围
        from_key = (from_time or "").replace(":", "")[:6]
        if not from_key:
            from_key = "000000"

        # 计算结束时间
        from_h = int(from_key[0:2])
        from_m = int(from_key[2:4])
        from_s = int(from_key[4:6])
        total_sec = from_h * 3600 + from_m * 60 + from_s + duration_minutes * 60
        to_h = (total_sec // 3600) % 24
        to_m = (total_sec % 3600) // 60
        to_s = total_sec % 60
        to_key = f"{to_h:02d}{to_m:02d}{to_s:02d}"

        selected = []
        for rec in recordings:
            if rec["start_time"] < from_key:
                continue
            if rec["start_time"] > to_key:
                continue
            selected.append(rec)

        if not selected:
            return b""

        # 读取所有选中文件的 ADPCM 数据，拼接后解码为 PCM16
        all_adpcm = bytearray()
        for rec in selected:
            fpath = Path(self._save_dir) / str(channel_id) / date_str / rec["filename"]
            try:
                data = fpath.read_bytes()
                all_adpcm.extend(data)
            except OSError:
                pass

        if not all_adpcm:
            return b""

        # 解码为 PCM16 → WAV
        decoder = ADPCMDecoder()
        pcm_data = decoder.decode_adpcm(bytes(all_adpcm))
        wav_data = make_wav(pcm_data, sample_rate=8000)
        return wav_data

    def get_date_size(self, date_str: str, channel_id: int) -> int:
        """返回指定日期指定通道的录音文件总大小"""
        recordings = self.list_recordings(date_str, channel_id)
        return sum(r["size"] for r in recordings)

    def _get_channel_bytes_saved(self, channel_id: int) -> int:
        """获取指定通道累计保存的字节数"""
        if not self._save_dir:
            return 0
        total = 0
        ch_dir = Path(self._save_dir) / str(channel_id)
        if not ch_dir.is_dir():
            return 0
        try:
            # 从内存中记录的突发文件累计
            with self._file_lock:
                for date_key, count_map in self._daily_file_count.items():
                    total += sum(count_map.values()) * 1024  # approximate
        except Exception:
            pass
        return total

    def _format_size(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / 1024 / 1024:.1f} MB"

    # ── 内部方法 ──────────────────────────────────────────

    def _init_pygame(self) -> None:
        try:
            import pygame
            pygame.mixer.pre_init(8000, -16, 1, 1024)
            pygame.mixer.init()
            self._pygame_ok = True
            self._pygame_mixer_inited = True
            logger.info("pygame mixer initialized: %s", pygame.mixer.get_init())
        except Exception as e:
            logger.warning("pygame init failed (audio playback disabled): %s", e)
            self._pygame_ok = False

    def _receive_loop(self) -> None:
        sock = self._socket
        if not sock:
            return

        while self._running:
            try:
                data, addr = sock.recvfrom(10240)
            except socket.timeout:
                # 心跳：定期做清理
                self._periodic_housekeeping()
                continue
            except Exception:
                if self._running:
                    logger.exception("voice receive error")
                continue

            if len(data) < 10:
                continue

            # 解析通道号 (前 2 字节, 小端序)
            channel = struct.unpack("<H", data[0:2])[0] & 0xFFFF

            # 只关心我们监控的 7 个通道
            sector_code = CHANNEL_SECTORS.get(channel)
            if not sector_code:
                continue

            # 记录活跃时间
            clock_now = time.time()
            mono_now = time.monotonic()
            if channel not in self._activity_window:
                self._activity_window[channel] = []
            self._activity_window[channel].append(mono_now)
            if len(self._activity_window[channel]) > 500:
                self._activity_window[channel] = self._activity_window[channel][-250:]

            # 更新状态
            st = self._status.get(sector_code)
            if st:
                st.last_activity = mono_now
                st.bytes_received += len(data)

            # ── 通话时长统计 ──
            self._track_duration(channel, clock_now)

            # ── 语音文件保存 ──
            # 提取 ADPCM 数据 (跳过前 2 字节通道号, 去掉末尾 8 字节)
            adpcm_data = data[2 : len(data) - 8]
            if adpcm_data:
                self._save_adpcm_data(channel, adpcm_data, clock_now)

            # 解码 PCM
            if channel not in self._decoders:
                self._decoders[channel] = ADPCMDecoder()
            decoder = self._decoders[channel]
            pcm_data = decoder.decode_adpcm(adpcm_data)

            # 推入 PCM 流缓冲（供浏览器 SSE 使用）
            if pcm_data and self._play_lock.acquire(blocking=False):
                try:
                    if self._playing_channel == channel:
                        if channel not in self._pcm_buffers:
                            self._pcm_buffers[channel] = deque(maxlen=200)
                            self._pcm_events[channel] = threading.Event()
                            self._pcm_seq[channel] = 0
                        buf = self._pcm_buffers[channel]
                        seq = self._pcm_seq[channel]
                        self._pcm_seq[channel] = seq + 1
                        buf.append((seq, pcm_data))
                        self._pcm_events[channel].set()
                finally:
                    self._play_lock.release()

            # （旧版）服务端本地播放 — 已废弃，改为浏览器 SSE 流式播放
            if pcm_data and self._pygame_ok and self._playing_channel == channel:
                self._play_pcm(pcm_data)

    def _periodic_housekeeping(self) -> None:
        """定期维护任务（在 socket timeout 时执行）"""
        # 清理过时的突发通话
        self._flush_stale_bursts()
        # 检查是否需要清理过期文件（每日一次）
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._last_cleanup_date:
            self._last_cleanup_date = today
            self._cleanup_old_files()

    def _track_duration(self, channel: int, clock_now: float) -> None:
        """追踪通话突发时长，计入对应 10 分钟 bucket"""
        with self._duration_lock:
            last_data = self._last_data_time.get(channel, 0.0)
            self._last_data_time[channel] = clock_now

            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._today_date:
                self._today_date = today

            date_key = today
            if date_key not in self._duration_buckets:
                new_day: dict[int, list[float]] = {}
                for ch in SECTOR_CHANNELS.values():
                    new_day[ch] = [0.0] * 144
                self._duration_buckets[date_key] = new_day
            day_buckets = self._duration_buckets[date_key]
            if channel not in day_buckets:
                day_buckets[channel] = [0.0] * 144

            if last_data > 0 and (clock_now - last_data) > self._silence_threshold:
                burst_start = self._burst_start.pop(channel, None)
                if burst_start is not None:
                    duration = last_data - burst_start
                    if duration > 0.5:
                        dt = datetime.utcfromtimestamp(burst_start)
                        slot = (dt.hour * 60 + dt.minute) // 10
                        if 0 <= slot < 144:
                            day_buckets[channel][slot] += duration

            if channel not in self._burst_start:
                self._burst_start[channel] = clock_now

    def _save_adpcm_data(self, channel: int, adpcm_data: bytes, clock_now: float) -> None:
        """保存 ADPCM 数据到文件（按突发分组）"""
        if not self._save_dir:
            return

        with self._file_lock:
            last_data = self._last_data_time.get(channel, 0.0)

            # 检查是否是新突发（上次数据超过静默阈值）
            if last_data > 0 and (clock_now - last_data) > self._silence_threshold:
                # 结束当前突发 → 写入文件
                self._flush_burst(channel)
                # 如果没有活跃的突发缓冲，启动新突发
                if channel not in self._burst_buffers:
                    self._burst_buffers[channel] = bytearray()
                    self._burst_start_ts[channel] = datetime.utcfromtimestamp(clock_now)
                    self._burst_seq[channel] = 0

            # 确保突发缓冲存在
            if channel not in self._burst_buffers:
                self._burst_buffers[channel] = bytearray()
                self._burst_start_ts[channel] = datetime.utcfromtimestamp(clock_now)
                self._burst_seq[channel] = 0

            # 追加数据到当前突发缓冲
            self._burst_buffers[channel].extend(adpcm_data)

            # 上限 2MB（约 4 分钟连续语音），静默检测 + 大小双重保障
            if len(self._burst_buffers[channel]) > 2 * 1024 * 1024:
                self._flush_burst(channel)
                self._burst_buffers[channel] = bytearray()
                self._burst_start_ts[channel] = datetime.utcfromtimestamp(clock_now)
                self._burst_seq[channel] = 0

    def _flush_burst(self, channel: int) -> None:
        """将指定通道的当前突发写入文件"""
        buf = self._burst_buffers.get(channel)
        if not buf or len(buf) == 0:
            return

        start_ts = self._burst_start_ts.get(channel)
        seq = self._burst_seq.get(channel, 0)

        if not start_ts:
            return

        date_dir = start_ts.strftime("%Y-%m-%d")
        # 文件名: HHMMSS_NNN.adpcm (UTC)
        ts_str = start_ts.strftime("%H%M%S")
        filename = f"{ts_str}_{seq:03d}.adpcm"

        save_path = Path(self._save_dir) / str(channel) / date_dir
        try:
            save_path.mkdir(parents=True, exist_ok=True)
            (save_path / filename).write_bytes(bytes(buf))
            logger.debug("voice file saved: %s (ch=%d, size=%d)",
                         save_path / filename, channel, len(buf))

            # 更新文件计数
            if date_dir not in self._daily_file_count:
                self._daily_file_count[date_dir] = {}
            if channel not in self._daily_file_count[date_dir]:
                self._daily_file_count[date_dir][channel] = 0
            self._daily_file_count[date_dir][channel] += 1
        except OSError as e:
            logger.error("failed to save voice file %s: %s",
                         save_path / filename, e)

        # 清空缓冲，递增序号
        self._burst_buffers.pop(channel, None)
        self._burst_seq[channel] = seq + 1

    def _flush_all_bursts(self) -> None:
        """停止时写入所有未存盘的突发"""
        with self._file_lock:
            for ch in list(self._burst_buffers.keys()):
                self._flush_burst(ch)

    def _cleanup_old_files(self) -> None:
        """删除超过保留天数的语音文件"""
        if self._retention_days <= 0 or not self._save_dir:
            return

        cutoff = datetime.now() - timedelta(days=self._retention_days)
        cutoff_str = cutoff.strftime("%Y-%m-%d")
        logger.info("voice cleanup: deleting files before %s", cutoff_str)

        base_dir = Path(self._save_dir)
        if not base_dir.is_dir():
            return

        deleted = 0
        freed_bytes = 0
        try:
            for ch_dir in base_dir.iterdir():
                if not ch_dir.is_dir():
                    continue
                for date_dir in ch_dir.iterdir():
                    if not date_dir.is_dir():
                        continue
                    date_name = date_dir.name
                    # 检查日期是否在保留期内
                    if date_name < cutoff_str:
                        for f in date_dir.glob("*.adpcm"):
                            freed_bytes += f.stat().st_size
                            f.unlink()
                            deleted += 1
                        # 删除空目录
                        try:
                            date_dir.rmdir()
                        except OSError:
                            pass
        except OSError as e:
            logger.error("voice cleanup error: %s", e)

        if deleted > 0:
            logger.info("voice cleanup: deleted %d files, freed %s",
                        deleted, self._format_size(freed_bytes))

    def _load_from_db(self) -> None:
        """启动时从 DB 恢复昨天的通话时长数据"""
        db = self._db
        if not db:
            return
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        for date_key in (today, yesterday):
            try:
                day_data: dict[int, list[float]] = {}
                for ch in SECTOR_CHANNELS.values():
                    day_data[ch] = db.load_voice_durations(date_key, ch)
                with self._duration_lock:
                    self._duration_buckets[date_key] = day_data
                logger.info("voice durations loaded from DB for %s", date_key)
            except Exception:
                pass

    def _migrate_old_files(self) -> None:
        """迁移旧版文件名 (HHMMSS_fff_NNN.adpcm) 到新格式 (HHMMSS_NNN.adpcm)，并将时间从北京转为UTC"""
        if not self._save_dir:
            return
        base = Path(self._save_dir)
        if not base.is_dir():
            return

        migrated = 0
        for ch_dir in sorted(base.iterdir()):
            if not ch_dir.is_dir():
                continue
            for date_dir in sorted(ch_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                for fpath in sorted(date_dir.glob("*.adpcm")):
                    name = fpath.stem
                    parts = name.split("_")
                    if len(parts) < 3:
                        continue  # 已经是新格式 HHMMSS_NNN 或非标准
                    # 旧格式: HHMMSS_fff_NNN.adpcm (北京时间)
                    local_hms = parts[0]
                    seq_str = parts[2] if len(parts) >= 3 else parts[1]
                    if len(local_hms) != 6 or not local_hms.isdigit():
                        continue
                    # 解析旧目录名作为本地日期
                    local_date_str = date_dir.name
                    try:
                        local_dt = datetime.strptime(local_date_str + "_" + local_hms, "%Y-%m-%d_%H%M%S")
                    except ValueError:
                        continue
                    # 北京时间 = UTC+8
                    from datetime import timedelta as _td
                    utc_dt = local_dt - _td(hours=8)
                    utc_date_str = utc_dt.strftime("%Y-%m-%d")
                    utc_hms = utc_dt.strftime("%H%M%S")
                    # 新文件名: HHMMSS_NNN.adpcm
                    new_name = f"{utc_hms}_{seq_str}.adpcm"
                    new_dir = Path(self._save_dir) / str(ch_dir.name) / utc_date_str
                    new_path = new_dir / new_name
                    if new_path.exists():
                        logger.warning("migration target exists, skipping: %s", new_path)
                        continue
                    try:
                        new_dir.mkdir(parents=True, exist_ok=True)
                        fpath.rename(new_path)
                        migrated += 1
                    except OSError as e:
                        logger.error("migration error: %s -> %s: %s", fpath, new_path, e)
                # 尝试删除旧日期目录（如果空了）
                try:
                    remaining = list(date_dir.iterdir())
                    if not remaining:
                        date_dir.rmdir()
                except OSError:
                    pass
        if migrated > 0:
            logger.info("voice file migration: renamed %d old files to UTC naming", migrated)

    def _flush_durations_to_db(self) -> None:
        """将内存中的语音时长数据刷到 DB"""
        db = self._db
        if not db:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        with self._duration_lock:
            day_data = self._duration_buckets.get(today, {})
            for channel, buckets in day_data.items():
                for slot, dur in enumerate(buckets):
                    if dur > 0:
                        try:
                            db.save_voice_duration(today, channel, slot, dur)
                        except Exception:
                            pass

    def _flush_stale_bursts(self) -> None:
        """清理超过静默阈值的突发通话（在 get_status 调用时执行）"""
        now = time.time()
        changed = False
        with self._duration_lock:
            for channel in list(self._burst_start.keys()):
                last_data = self._last_data_time.get(channel, 0.0)
                if last_data > 0 and (now - last_data) > self._silence_threshold:
                    burst_start = self._burst_start.pop(channel, None)
                    if burst_start is not None:
                        duration = last_data - burst_start
                        if duration > 0.5:
                            today = datetime.now().strftime("%Y-%m-%d")
                            day_buckets = self._duration_buckets.get(today, {})
                            if channel in day_buckets:
                                dt = datetime.utcfromtimestamp(burst_start)
                                slot = (dt.hour * 60 + dt.minute) // 10
                                if 0 <= slot < 144:
                                    day_buckets[channel][slot] += duration
                            changed = True
        # 如果清理了突发，顺便刷到 DB
        if changed:
            self._db_flush_counter += 1
            if self._db_flush_counter >= 10:
                self._flush_durations_to_db()
                self._db_flush_counter = 0

    # ── 浏览器 SSE 流式播放 ──────────────────────────

    def wait_pcm_data(self, channel: int, timeout: float = 0.5) -> list[tuple[int, bytes]] | None:
        """等待并返回指定通道的新 PCM 数据块列表。
        返回 [(seq, pcm_bytes), ...] 或超时返回 None。
        """
        ev = self._pcm_events.get(channel)
        buf = self._pcm_buffers.get(channel)
        if ev is None or buf is None:
            ev = threading.Event()
            self._pcm_events[channel] = ev
            buf = deque(maxlen=200)
            self._pcm_buffers[channel] = buf

        if not buf:
            ev.clear()
            ev.wait(timeout=timeout)

        if not buf:
            return None
        items = list(buf)
        buf.clear()
        return items

    def clear_pcm_buffer(self, channel: int) -> None:
        """清空指定通道的 PCM 缓冲（切换通道时调用）"""
        buf = self._pcm_buffers.get(channel)
        if buf:
            buf.clear()
        if channel in self._pcm_events:
            self._pcm_events[channel].set()

    # ── 旧版服务器本地播放 ──

    def _play_pcm(self, pcm_data: bytes) -> None:
        """通过 pygame 播放 PCM16 数据"""
        if not self._pygame_ok:
            return
        try:
            import pygame
            sound = pygame.mixer.Sound(buffer=pcm_data)
            sound.set_volume(1.0)
            pygame.mixer.find_channel(True).play(sound)
        except Exception as e:
            logger.debug("voice play error: %s", e)
