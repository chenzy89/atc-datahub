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
class AppConfig:
    system_name: str = "ATC AFTN WebHub"
    aftn: EndpointConfig = field(default_factory=EndpointConfig)
    radar: RadarEndpointConfig = field(default_factory=RadarEndpointConfig)
    voice: VoiceEndpointConfig = field(default_factory=VoiceEndpointConfig)
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
        database=DatabaseConfig(
            path=db.get("path", "./data/aftn.db"),
        ),
        web=WebConfig(
            host=web.get("host", "0.0.0.0"),
            port=int(web.get("port", 5000)),
            debug=bool(web.get("debug", False)),
        ),
        config_file=config_file,
    )
