"""战绩与段位数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MatchParticipant:
    puuid: str
    champion_id: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    win: bool = False
    team_id: int = 0
    is_user: bool = False

    @property
    def kda(self) -> str:
        return f"{self.kills}/{self.deaths}/{self.assists}"


@dataclass
class MatchSummary:
    """单局概要（国服字段名可能与外服不同，由 provider 负责归一化）。"""

    game_id: str
    queue_id: int = 0
    game_mode: str = ""
    map_id: int = 0
    game_duration_seconds: int = 0
    game_creation_ms: int = 0
    game_end_time_ms: int = 0
    participants: list[MatchParticipant] = field(default_factory=list)

    def participant_of(self, puuid: str) -> MatchParticipant | None:
        for p in self.participants:
            if p.puuid == puuid:
                return p
        return None

    @property
    def duration_text(self) -> str:
        s = self.game_duration_seconds
        if s <= 0:
            return "--:--"
        return f"{s // 60:02d}:{s % 60:02d}"

    def win_of(self, puuid: str) -> bool | None:
        p = self.participant_of(puuid)
        return p.win if p else None

    def kda_of(self, puuid: str) -> str:
        p = self.participant_of(puuid)
        return p.kda if p else "-/-/-"

    def champion_of(self, puuid: str) -> int:
        p = self.participant_of(puuid)
        return p.champion_id if p else 0


@dataclass
class RankedInfo:
    """排位段位信息。"""

    puuid: str
    queue_type: str = ""  # RANKED_SOLO_5x5 / RANKED_FLEX_SR
    tier: str = ""  # CHALLENGER..IRON
    division: str = ""  # I..IV
    league_points: int = 0
    wins: int = 0
    losses: int = 0
    hot_streak: bool = False

    @property
    def display(self) -> str:
        """中文段位展示。"""
        tier_map = {
            "IRON": "坚韧黑铁",
            "BRONZE": "英勇黄铜",
            "SILVER": "不屈白银",
            "GOLD": "荣耀黄金",
            "PLATINUM": "华贵铂金",
            "EMERALD": "璀璨钻石",
            "DIAMOND": "璀璨钻石",
            "MASTER": "傲世宗师",
            "GRANDMASTER": "傲世宗师",
            "CHALLENGER": "最强王者",
        }
        cn = tier_map.get(self.tier.upper(), self.tier)
        # MASTER 及以上无小分区
        if self.tier.upper() in ("MASTER", "GRANDMASTER", "CHALLENGER"):
            return f"{cn} · {self.league_points}LP"
        div = self.division.replace("I", "Ⅰ").replace("V", "Ⅴ") if self.division else ""
        base = f"{cn}{div}" if cn else "暂无段位"
        return f"{base} · {self.league_points}LP" if self.league_points else base
