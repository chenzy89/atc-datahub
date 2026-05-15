"""移交点（Handover Point）解析器

从 TransPtKeyFix.txt 读取移交点 ↔ 航路关键点映射表，
通过航路内容匹配找出对应的移交点。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_MAPPING_FILE = Path(__file__).resolve().parent.parent / "config" / "TransPtKeyFix.txt"


class HandoverResolver:
    """航路 → 移交点解析器"""

    def __init__(self, mapping_path: str | Path | None = None) -> None:
        """
        mapping_path: TransPtKeyFix.txt 路径，默认从 config/ 下读取
        """
        path = Path(mapping_path) if mapping_path else _MAPPING_FILE
        self._rules: list[tuple[str, frozenset[str]]] = []  # (handover_pt, key_words)
        self._load(path)

    def _load(self, path: Path) -> None:
        """加载映射文件，按 key 长度降序排列（长 key 优先匹配）"""
        raw: list[tuple[str, frozenset[str]]] = []
        if not path.exists():
            return  # 文件不存在，不报错
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            handover_pt = parts[0].strip().upper()
            key_words = frozenset(parts[1].strip().upper().split())
            if handover_pt and key_words:
                raw.append((handover_pt, key_words))
        # 按 key 单词数降序（最具体规则优先匹配）
        raw.sort(key=lambda x: -len(x[1]))
        self._rules = raw

    def resolve(self, route: str | None) -> str:
        """根据航路返回移交点，未匹配则返回空字符串"""
        if not route:
            return ""
        route_words = set(route.upper().split())
        for handover_pt, key_words in self._rules:
            if key_words.issubset(route_words):
                return handover_pt
        return ""

    def resolve_with_route_key(self, route: str | None) -> tuple[str, str]:
        """返回 (移交点, 匹配的航路关键点)，方便调试"""
        if not route:
            return ("", "")
        route_words = set(route.upper().split())
        for handover_pt, key_words in self._rules:
            if key_words.issubset(route_words):
                return (handover_pt, " ".join(sorted(key_words)))
        return ("", "")

    @property
    def rule_count(self) -> int:
        return len(self._rules)


# 单例，应用启动时创建
_resolver: Optional[HandoverResolver] = None


def get_resolver() -> HandoverResolver:
    global _resolver
    if _resolver is None:
        _resolver = HandoverResolver()
    return _resolver


def reload_resolver() -> None:
    """重新加载映射表（配置文件更新后调用）"""
    global _resolver
    _resolver = HandoverResolver()
