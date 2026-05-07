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
class AppConfig:
    system_name: str = "ATC AFTN WebHub"
    aftn: EndpointConfig = field(default_factory=EndpointConfig)
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
