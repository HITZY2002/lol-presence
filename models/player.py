"""玩家绑定数据模型。

绑定主键为 (qq_id, puuid)。game_name / tag_line 仅作展示缓存，
Riot ID 可修改，禁止作为持久主键使用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Binding:
    qq_id: str
    puuid: str
    game_name: str = ""
    tag_line: str = ""
    platform: str = ""  # 国服大区，如 ionia / betrayal(峡谷之巅)
    region: str = "cn"
    enabled: bool = True
    broadcast_enabled: bool = True
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def display_name(self) -> str:
        """展示名：gameName#TAG，缺 TAG 时只显示 gameName。"""
        if self.tag_line:
            return f"{self.game_name}#{self.tag_line}"
        return self.game_name or self.puuid[:8]

    def to_dict(self) -> dict:
        return {
            "qq_id": self.qq_id,
            "puuid": self.puuid,
            "game_name": self.game_name,
            "tag_line": self.tag_line,
            "platform": self.platform,
            "region": self.region,
            "enabled": int(self.enabled),
            "broadcast_enabled": int(self.broadcast_enabled),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row) -> "Binding":
        d = dict(row)
        return cls(
            qq_id=str(d["qq_id"]),
            puuid=d["puuid"],
            game_name=d.get("game_name") or "",
            tag_line=d.get("tag_line") or "",
            platform=d.get("platform") or "",
            region=d.get("region") or "cn",
            enabled=bool(d.get("enabled", 1)),
            broadcast_enabled=bool(d.get("broadcast_enabled", 1)),
            created_at=d.get("created_at") or 0.0,
            updated_at=d.get("updated_at") or 0.0,
        )
