"""语音数据接收器 — 组播 ADPCM 音频接收、VAD 通话时长统计、流式播放"""

from __future__ import annotations

import base64
import logging
import math
import os
import socket
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
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

# 终端代码 → 短扇区名
SECTOR_CODE_TO_SHORT: dict[str, str] = {
    "ZGJDTM01": "HN",
    "ZGJDTM02": "HE",
    "ZGJDTM03": "ARW",
    "ZGJDTM04": "AS",
    "ZGJDTM05": "AD",
    "ZGJDTM06": "ASL",
    "ZGJDTM07": "ARE",
}

# 扇区合并规则（子扇区 terminal_code → 父扇区 terminal_code）
# 子扇区 10 分钟无通话时，其航班架次合并入父扇区（去重）
SECTOR_MERGE_RULES: dict[str, str] = {
    "ZGJDTM06": "ZGJDTM04",  # ASL → AS
    "ZGJDTM01": "ZGJDTM04",  # HN → AS
    "ZGJDTM04": "ZGJDTM02",  # AS → HE
    "ZGJDTM07": "ZGJDTM03",  # ARE/AA → ARW (ARE 和 AA 共用 ZGJDTM07)
    "ZGJDTM05": "ZGJDTM03",  # AD → ARW
    "ZGJDTM03": "ZGJDTM02",  # ARW → HE
}

# 终端代码 → 内话通道号（反向查找）
CODE_TO_CHANNEL: dict[str, int] = {v: k for k, v in SECTOR_CHANNELS.items()}

# VAD (语音活动检测) 默认参数
_VAD_ENERGY_THRESHOLD_DEFAULT = 0.005  # PCM 归一化 RMS 能量阈值，低于此视为静音
_VAD_SILENCE_MS_DEFAULT = 1000        # 持续静音超过此值视为通话结束 (ms)
_VAD_NOISE_RATIO_DEFAULT = 3.0        # 能量高于噪声底噪多少倍视为语音
_VAD_MAX_BURST_SECONDS = 30.0         # 突发最长时间，超过则强制结束（防止持续载波导致无结束）

# 固定 VAD 阈值覆盖（通道ID -> 固定能量阈值）
# 当某个通道的噪声底噪恒定时使用此方式，绕过自适应噪声底噪计算。
# 能量高于此值视为语音，低于或等于视为静音。
_VAD_FIXED_THRESHOLDS: dict[int, float] = {
    50: 0.071,  # ch50: 无通话时固定噪音强度 0.071，通话时上下波动
}

# 固定阈值的容差 margin（防止 RMS 浮点计算略微高于阈值导致 VAD 永不静音）
# 实际判定为 `energy > threshold + margin`
_VAD_FIXED_THRESHOLD_MARGIN = 0.0005


@dataclass
class ChannelStatus:
    """单个通道的状态"""
    sector_code: str           # ZGJDTM01
    channel_id: int            # 内话通道号 38, 42, …
    frequency: str             # 频率如 "120.35"
    sector_name: str           # 扇区名如 HN
    last_activity: float = 0.0 # time.monotonic() 最后收到数据时间
    bytes_received: int = 0    # 累计接收字节数
    active: bool = False       # 最近 3 秒是否有数据
    vad_active: bool = False   # 最近 3 秒 VAD 是否检测到语音
    vad_energy: float = 0.0    # 当前音频能量值（归一化）
    vad_noise_floor: float = 0.0  # 当前噪声底噪值
    selected: bool = False     # 是否被选中播放

    def to_dict(self) -> dict:
        return {
            "sector_code": self.sector_code,
            "channel_id": self.channel_id,
            "frequency": self.frequency,
            "sector_name": self.sector_name,
            "last_activity": self.last_activity,
            "bytes_received": self.bytes_received,

            "active": self.active,
            "vad_active": self.vad_active,
            "vad_energy": self.vad_energy,
            "vad_noise_floor": self.vad_noise_floor,
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
        vad_energy_threshold: float = _VAD_ENERGY_THRESHOLD_DEFAULT,
        vad_silence_ms: int = _VAD_SILENCE_MS_DEFAULT,
    ):
        self.multicast_group = multicast_group
        self.port = port
        self.interface_ip = interface_ip
        self._running = False
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._db = db

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
        self._burst_voice_duration: dict[int, float] = {}  # 当前突发的有效语音秒数
        self._last_data_time: dict[int, float] = {}
        self._silence_threshold = vad_silence_ms / 1000.0  # 静音阈值 (秒)
        self._today_date = datetime.now().strftime("%Y-%m-%d")
        self._duration_lock = threading.Lock()
        self._last_db_flush_time = time.monotonic()

        # ── VAD (语音活动检测) ──
        self._vad_energy_threshold = vad_energy_threshold
        self._vad_noise_ratio = _VAD_NOISE_RATIO_DEFAULT  # 能量高于噪声底噪倍数视为语音
        self._vad_max_burst_seconds = _VAD_MAX_BURST_SECONDS  # 突发强制结束时间
        self._vad_silence_duration: dict[int, float] = {}  # channel/fs_key -> 连续静音秒数
        self._vad_noise_samples: dict[int, deque] = {}  # channel -> 最近50个能量值，用于滚动噪声底噪
        self._vad_burst_start_time: dict[int, float] = {}  # channel -> 突发开始时 clock_now
        self._vad_last_voice_time: dict[int, float] = {}  # channel -> time.monotonic() 最后检测到语音的时间
        self._vad_last_energy: dict[int, float] = {}  # channel -> 最后更新的音频能量
        self._vad_last_noise_floor: dict[int, float] = {}  # channel -> 最后更新的噪声底噪
        # 能量历史（用于前端波形图，采样于每次 get_status 调用）
        # deque(maxlen=30) ≈ 30 × 2s ≈ 1 分钟窗口
        self._vad_energy_history: dict[int, deque] = {}
        self._vad_energy_history_last_sample: dict[int, float] = {}

        # 从 DB 恢复已有数据
        self._load_from_db()

    # ── 公共属性 ──────────────────────────────────────

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
        # 保存所有通话时长到 DB，防止重启丢失 last burst 数据
        self._flush_stale_bursts()
        self._flush_durations_to_db()
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
            # 更新 VAD 活跃状态：3 秒内检测到语音则为活跃
            last_voice = self._vad_last_voice_time.get(ch_id, 0.0)
            st.vad_active = (last_voice > 0 and (now - last_voice) < 3.0)
            st.vad_energy = self._vad_last_energy.get(ch_id, 0.0)
            st.vad_noise_floor = self._vad_last_noise_floor.get(ch_id, 0.0)
            # 更新能量历史（所有通道统一每 ~1s 采样一次）
            now_ts = time.time()
            last_sample = self._vad_energy_history_last_sample.get(ch_id, now_ts)
            if ch_id not in self._vad_energy_history:
                self._vad_energy_history[ch_id] = deque(maxlen=60)
            while now_ts - last_sample >= 1.0:
                energy = self._vad_last_energy.get(ch_id, 0.0) if st.active else 0.0
                self._vad_energy_history[ch_id].append(energy)
                last_sample += 1.0
            self._vad_energy_history_last_sample[ch_id] = last_sample


            d = st.to_dict()
            d["energy_history"] = list(self._vad_energy_history.get(ch_id, []))
            result.append(d)
        self._flush_stale_bursts()
        # 每 30 秒刷一次通話时长到 DB（防止重启丢失）
        now_t = time.monotonic()
        if now_t - self._last_db_flush_time >= 30.0:
            self._flush_durations_to_db()
            self._last_db_flush_time = now_t
        return result

    def get_channel_duration(self, date_str: str, channel_id: int) -> list[float]:
        """返回指定日期指定通道的 144 个 10 分钟时段的通话秒数"""
        with self._duration_lock:
            day_data = self._duration_buckets.get(date_str, {})
            return day_data.get(channel_id, [0.0] * 144)

    def build_voice_activity_map(self, date_str: str) -> dict[str, list[bool]]:
        """构建扇区通话活动映射

        返回 {terminal_code: [144 bool]}，True 表示该 slot 有通话
        基于 voice_duration 来判断：duration > 0 = 有通话
        """
        result: dict[str, list[bool]] = {}
        for ch_id, sector_code in CHANNEL_SECTORS.items():
            durations = self.get_channel_duration(date_str, ch_id)
            terminal_code = sector_code
            result[terminal_code] = [d > 0 for d in durations]
        return result

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

        # 轮询器，用于监控 socket 是否可读
        import select as _select
        poller = _select.poll()
        poller.register(sock, _select.POLLIN)

        self._loop_iteration = 0
        self._last_drain_warning = 0.0

        while self._running:
            try:
                events = poller.poll(500)  # 500ms timeout
            except Exception:
                if self._running:
                    logger.exception("voice select error")
                continue

            if not events:
                # 心跳：定期做清理
                self._periodic_housekeeping()

                # 自愈检测：如果 socket 积压超过 100KB 且无活动
                # 尝试 drain 积压数据
                self._try_drain_buffer(sock)
                continue

            # 持续 drain socket 直到清空或超时
            drained = 0
            while self._running:
                try:
                    data, addr = sock.recvfrom(10240, socket.MSG_DONTWAIT)
                except BlockingIOError:
                    break  # 无更多数据
                except socket.timeout:
                    break
                except Exception as e:
                    if self._running:
                        logger.exception("voice receive error")
                    break

                drained += 1

                try:
                    self._process_voice_packet(data)
                except Exception:
                    logger.exception("voice packet processing error (ch=%s)",
                                     struct.unpack("<H", data[0:2])[0] & 0xFFFF if len(data) >= 2 else -1)
                self._loop_iteration += 1

    def _process_voice_packet(self, data: bytes) -> None:
        """处理一个语音数据包：解析通道、解码 PCM、跟踪时长、保存文件

        提取为独立方法，方便异常处理，避免单包异常导致整个接收线程崩溃。
        """
        if len(data) < 10:
            return

        # 解析通道号 (前 2 字节, 小端序)
        channel = struct.unpack("<H", data[0:2])[0] & 0xFFFF

        # 只关心我们监控的 7 个通道
        sector_code = CHANNEL_SECTORS.get(channel)
        if not sector_code:
            return

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

        # ── 提取 ADPCM 并提前解码 PCM（用于 VAD 能量检测） ──
        adpcm_data = data[2 : len(data) - 8]
        pcm_data = b""
        if adpcm_data:
            if channel not in self._decoders:
                self._decoders[channel] = ADPCMDecoder()
            decoder = self._decoders[channel]
            pcm_data = decoder.decode_adpcm(adpcm_data)

        # ── 通话时长统计（VAD 能量检测） ──
        self._track_duration(channel, clock_now, pcm_data)

        # 推入 PCM 流缓冲（用于浏览器 SSE 流式播放）
        if pcm_data and self._play_lock.acquire(blocking=False):
            try:
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

        # 旧版本地播放
        if pcm_data and self._pygame_ok and self._playing_channel == channel:
            self._play_pcm(pcm_data)

    def _try_drain_buffer(self, sock) -> None:
        """检测 socket 积压并尝试 drain，防止 rx_queue 填满"""
        try:
            # 尝试非阻塞读取 — 如果有数据，直接读取并丢弃
            # 即使丢弃，也更新 activity_window 确保绿灯亮
            drained = 0
            for _ in range(200):
                try:
                    data, _ = sock.recvfrom(10240, socket.MSG_DONTWAIT)
                except (BlockingIOError, socket.timeout):
                    break
                except Exception:
                    break
                drained += 1
                if len(data) < 10:
                    continue
                ch = struct.unpack("<H", data[0:2])[0] & 0xFFFF
                sc = CHANNEL_SECTORS.get(ch)
                if sc:
                    mono = time.monotonic()
                    if ch not in self._activity_window:
                        self._activity_window[ch] = []
                    self._activity_window[ch].append(mono)
                    st = self._status.get(sc)
                    if st:
                        st.last_activity = mono
                        st.bytes_received += len(data)
            if drained > 0:
                logger.info("drained %d packets from voice rx buffer", drained)
        except Exception:
            pass

    def _periodic_housekeeping(self) -> None:
        """定期维护任务（在 socket timeout 时执行）"""
        # 清理过时的突发通话
        self._flush_stale_bursts()

    @staticmethod
    def _compute_pcm_energy(pcm_data: bytes) -> float:
        """计算 PCM16 数据的 RMS 能量，归一化到 [0, 1]"""
        if not pcm_data or len(pcm_data) < 2:
            return 0.0
        # PCM16 小端，每 2 字节一个 sample
        samples = len(pcm_data) // 2
        total = 0.0
        for i in range(samples):
            off = i * 2
            sample = struct.unpack_from("<h", pcm_data, off)[0]
            total += sample * sample
        rms = math.sqrt(total / samples)
        return rms / 32767.0  # 归一化

    def _track_duration(self, channel: int, clock_now: float, pcm_data: bytes) -> None:
        """能量检测 VAD → 追踪通话突发时长，计入对应 10 分钟 bucket

        使用 PCM 能量判断语音/静音，替代原有的数据包间隔检测。
        """
        energy = self._compute_pcm_energy(pcm_data) if pcm_data else 0.0
        pcm_duration = len(pcm_data) / 16000.0 if pcm_data else 0.0  # 8kHz 16bit

        with self._duration_lock:
            # 更新日期
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

            # ── VAD 判断（自适应噪声底噪，ch50 使用固定阈值） ──
            fixed_threshold = _VAD_FIXED_THRESHOLDS.get(channel)
            if fixed_threshold is not None:
                # 固定阈值模式：加 margin 避免浮点略微高于阈值导致 VAD 永不静音
                is_voice = energy > fixed_threshold + _VAD_FIXED_THRESHOLD_MARGIN
                self._vad_last_energy[channel] = energy
                self._vad_last_noise_floor[channel] = 0.0  # 固定阈值模式下无意义
            else:
                # 自适应噪声底噪模式（原有逻辑）
                if channel not in self._vad_noise_samples:
                    self._vad_noise_samples[channel] = deque(maxlen=50)  # 约3秒窗口
                self._vad_noise_samples[channel].append(energy)
                noise_floor = min(self._vad_noise_samples[channel])
                dynamic_threshold = max(self._vad_energy_threshold, noise_floor * self._vad_noise_ratio)
                is_voice = energy > dynamic_threshold
                # 更新能量和噪声底噪（供前端显示）
                self._vad_last_energy[channel] = energy
                self._vad_last_noise_floor[channel] = noise_floor

            # 强制结束检测：突发超过最大时长
            burst_age = 0.0
            if channel in self._burst_start:
                burst_age = clock_now - self._burst_start[channel]
                if burst_age > self._vad_max_burst_seconds and \
                        self._burst_voice_duration.get(channel, 0.0) > 0.5:
                    # 超长突发：强制结束
                    burst_start = self._burst_start.pop(channel, None)
                    voice_dur = self._burst_voice_duration.pop(channel, None) or 0.0
                    if burst_start is not None and voice_dur > 0.5:
                        dt = datetime.utcfromtimestamp(burst_start)
                        slot = (dt.hour * 60 + dt.minute) // 10
                        if 0 <= slot < 144:
                            day_buckets[channel][slot] += voice_dur
                            if self._db:
                                try:
                                    db_save_today = self._today_date
                                    self._db.save_voice_duration(db_save_today, channel, slot, day_buckets[channel][slot])
                                except Exception:
                                    pass
                    if channel in self._vad_silence_duration:
                        self._vad_silence_duration[channel] = 0.0
                    # 立即开始新突发
                    self._burst_start[channel] = clock_now
                    self._burst_voice_duration[channel] = 0.0
                    return

            if is_voice:
                # 语音活动：记录 VAD 活跃时间（用于前端指示器）
                self._vad_last_voice_time[channel] = time.monotonic()
                # 重置静音计数器
                self._vad_silence_duration[channel] = 0.0
                # 如果尚无活跃突发，启动新突发
                if channel not in self._burst_start:
                    self._burst_start[channel] = clock_now
                    self._burst_voice_duration[channel] = 0.0
                # 累加有效语音时长
                self._burst_voice_duration[channel] = \
                    self._burst_voice_duration.get(channel, 0.0) + pcm_duration
            else:
                # 静音：累积静音时长
                silence = self._vad_silence_duration.get(channel, 0.0) + pcm_duration
                self._vad_silence_duration[channel] = silence

                # 如果静音超过阈值，结束当前突发
                if silence > self._silence_threshold:
                    burst_start = self._burst_start.pop(channel, None)
                    voice_dur = self._burst_voice_duration.pop(channel, None) or 0.0
                    if burst_start is not None and voice_dur > 0.5:
                        dt = datetime.utcfromtimestamp(burst_start)
                        slot = (dt.hour * 60 + dt.minute) // 10
                        if 0 <= slot < 144:
                            day_buckets[channel][slot] += voice_dur
                            # 突发结束 → 立即刷到 DB（防止重启丢失）
                            if self._db:
                                try:
                                    self._db.save_voice_duration(today, channel, slot, day_buckets[channel][slot])
                                except Exception:
                                    pass
                    # 重置静音计数器（避免反复触发）
                    self._vad_silence_duration[channel] = 0.0



    def _load_from_db(self) -> None:
        """启动时从 DB 恢复最近几天的通话时长数据"""
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
        """清理超过静默阈值的突发通话（使用 VAD 静音计数器）

        在 get_status 和定期维护时调用，作为 _track_duration 的后备保障。
        """
        changed = False
        with self._duration_lock:
            for channel in list(self._burst_start.keys()):
                # 检查 VAD 静音计数器
                silence_dur = self._vad_silence_duration.get(channel, 0.0)
                if silence_dur > self._silence_threshold:
                    burst_start = self._burst_start.pop(channel, None)
                    voice_dur = self._burst_voice_duration.pop(channel, None) or 0.0
                    if burst_start is not None and voice_dur > 0.5:
                        today = datetime.now().strftime("%Y-%m-%d")
                        day_buckets = self._duration_buckets.get(today, {})
                        if channel in day_buckets:
                            dt = datetime.utcfromtimestamp(burst_start)
                            slot = (dt.hour * 60 + dt.minute) // 10
                            if 0 <= slot < 144:
                                day_buckets[channel][slot] += voice_dur
                                if self._db:
                                    try:
                                        db_save_today = datetime.now().strftime("%Y-%m-%d")
                                        self._db.save_voice_duration(db_save_today, channel, slot, day_buckets[channel][slot])
                                    except Exception:
                                        pass
                            changed = True
                    self._vad_silence_duration[channel] = 0.0

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
