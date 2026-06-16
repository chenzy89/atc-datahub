"""配置管理"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EndpointConfig:
    bind_host: str = "0.0.0.0"
    port: int = 31031
    multicast_group: str | None = None
    interface_ip: str = "0.0.0.0"


@dataclass
class DatabaseConfig:
    path: str = "./data/aftn.db"


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 5000
    debug: bool = False
    map_background_color: str = "#0a1628"


@dataclass
class VoiceDataConfig:
    """语音数据配置"""
    flight_count_max: int = 18               # 通话时长统计图右Y轴架次最大值
    vad_energy_threshold: float = 0.005      # VAD (语音活动检测) 能量阈值，低于此视为静音
    vad_silence_ms: int = 1000               # 持续静音超过此值视为通话结束 (毫秒)


@dataclass
class RadarEndpointConfig:
    multicast_group: str = "228.28.28.28"
    port: int = 8107
    interface_ip: str = "0.0.0.0"
    enabled: bool = False


@dataclass
class VoiceEndpointConfig:
    multicast_group: str = "229.34.34.34"
    port: int = 34034
    interface_ip: str = "0.0.0.0"
    enabled: bool = False


@dataclass
class AsrEndpointConfig:
    multicast_group: str = "229.33.33.33"
    port: int = 33033
    interface_ip: str = "0.0.0.0"
    enabled: bool = False


@dataclass
class TrackRecordingConfig:
    """航迹保存配置"""
    enabled: bool = False
    airports: tuple = ("ZGSZ", "ZGSD", "VMMC")
    top_left_lat: float = 23.5
    top_left_lon: float = 112.0
    bottom_right_lat: float = 21.0
    bottom_right_lon: float = 115.5


@dataclass
class AppConfig:
    system_name: str = "ATC AFTN WebHub"
    aftn: EndpointConfig = field(default_factory=EndpointConfig)
    radar: RadarEndpointConfig = field(default_factory=RadarEndpointConfig)
    voice: VoiceEndpointConfig = field(default_factory=VoiceEndpointConfig)
    asr: AsrEndpointConfig = field(default_factory=AsrEndpointConfig)
    track_recording: TrackRecordingConfig = field(default_factory=TrackRecordingConfig)
    voice_data: VoiceDataConfig = field(default_factory=VoiceDataConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    web: WebConfig = field(default_factory=WebConfig)
    config_file: Path | None = None

    @property
    def db_path(self) -> Path:
        base = self.config_file.parent if self.config_file else Path.cwd()
        p = Path(self.database.path)
        if not p.is_absolute():
            p = (base / p).resolve()
        return p


def load_config(config_path: str | Path) -> AppConfig:
    path = Path(config_path).expanduser().resolve()
    raw = json.loads(path.read_text(encoding="utf-8"))
    return _build_config(raw, path)


def _build_config(raw: dict[str, Any], config_file: Path) -> AppConfig:
    net = raw.get("network", {}).get("aftn", {})
    radar_raw = raw.get("network", {}).get("radar", {})
    voice_raw = raw.get("network", {}).get("voice", {})
    asr_raw = raw.get("network", {}).get("asr", {})
    track_raw = raw.get("track_recording", {})
    voice_data_raw = raw.get("voice_data", {})
    db = raw.get("database", {})
    web = raw.get("web", {})

    return AppConfig(
        system_name=raw.get("system_name", "ATC AFTN WebHub"),
        aftn=EndpointConfig(
            bind_host=net.get("bind_host", "0.0.0.0"),
            port=int(net.get("port", 31031)),
            multicast_group=net.get("multicast_group"),
            interface_ip=net.get("interface_ip", "0.0.0.0"),
        ),
        radar=RadarEndpointConfig(
            multicast_group=radar_raw.get("multicast_group", "228.28.28.28"),
            port=int(radar_raw.get("port", 8107)),
            interface_ip=radar_raw.get("interface_ip", "0.0.0.0"),
            enabled=bool(radar_raw.get("enabled", False)),
        ),
        voice=VoiceEndpointConfig(
            multicast_group=voice_raw.get("multicast_group", "229.34.34.34"),
            port=int(voice_raw.get("port", 34034)),
            interface_ip=voice_raw.get("interface_ip", "0.0.0.0"),
            enabled=bool(voice_raw.get("enabled", False)),
        ),
        asr=AsrEndpointConfig(
            multicast_group=asr_raw.get("multicast_group", "229.33.33.33"),
            port=int(asr_raw.get("port", 33033)),
            interface_ip=asr_raw.get("interface_ip", "0.0.0.0"),
            enabled=bool(asr_raw.get("enabled", False)),
        ),
        track_recording=TrackRecordingConfig(
            enabled=bool(track_raw.get("enabled", False)),
            airports=tuple(track_raw.get("airports", ["ZGSZ", "ZGSD", "VMMC"])),
            top_left_lat=float(track_raw.get("area_top_left", {}).get("lat", 23.5)),
            top_left_lon=float(track_raw.get("area_top_left", {}).get("lon", 112.0)),
            bottom_right_lat=float(track_raw.get("area_bottom_right", {}).get("lat", 21.0)),
            bottom_right_lon=float(track_raw.get("area_bottom_right", {}).get("lon", 115.5)),
        ),
        voice_data=VoiceDataConfig(
            flight_count_max=int(voice_data_raw.get("flight_count_max", 18)),
            vad_energy_threshold=float(voice_data_raw.get("vad_energy_threshold", 0.005)),
            vad_silence_ms=int(voice_data_raw.get("vad_silence_ms", 1000)),
        ),
        database=DatabaseConfig(
            path=db.get("path", "./data/aftn.db"),
        ),
        web=WebConfig(
            host=web.get("host", "0.0.0.0"),
            port=int(web.get("port", 5000)),
            debug=bool(web.get("debug", False)),
            map_background_color=str(web.get("map_background_color", "#0a1628")),
        ),
        config_file=config_file,
    )
