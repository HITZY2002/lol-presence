"""统一 Presence 状态模型。

核心原则：
- 状态必须来自数据源的真实字段，禁止由战绩/段位推断伪造。
- 数据源只能给出 offline/online/inGame 粗粒度状态时，
  统一映射为 OFFLINE / ONLINE / IN_GAME，不得伪造 LOBBY 等中间态。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class PresenceState(str, Enum):
    OFFLINE = "OFFLINE"
    ONLINE = "ONLINE"  # 客户端在线（含 lobby，视数据源粒度）
    LOBBY = "LOBBY"  # 仅高粒度数据源使用
    MATCHMAKING = "MATCHMAKING"  # 仅高粒度数据源使用
    CHAMP_SELECT = "CHAMP_SELECT"  # 仅高粒度数据源使用
    IN_GAME = "IN_GAME"
    UNKNOWN = "UNKNOWN"


class PresenceEvent(str, Enum):
    PLAYER_ONLINE = "PLAYER_ONLINE"  # OFFLINE -> 任意非离线态
    PLAYER_OFFLINE = "PLAYER_OFFLINE"  # 任意非离线态 -> OFFLINE
    GAME_STARTED = "GAME_STARTED"  # -> IN_GAME
    GAME_ENDED = "GAME_ENDED"  # IN_GAME -> 非游戏态
    PRESENCE_CHANGED = "PRESENCE_CHANGED"  # 其他中间态变化（一般不播报）


# LCU lol.gameStatus 等原始值 -> 统一状态。
# 未知值一律落 UNKNOWN，绝不猜测。
GAME_STATUS_MAP: dict[str, PresenceState] = {
    "outOfGame": PresenceState.ONLINE,
    "inQueue": PresenceState.MATCHMAKING,
    "lobby": PresenceState.LOBBY,
    "matchmaking": PresenceState.MATCHMAKING,
    "readyCheck": PresenceState.MATCHMAKING,
    "champSelect": PresenceState.CHAMP_SELECT,
    "inGame": PresenceState.IN_GAME,
    # LCU chat presence 还可能出现：
    "mobileLobby": PresenceState.LOBBY,
    "awake": PresenceState.ONLINE,
    # 粗粒度（WeGame 类）：
    "online": PresenceState.ONLINE,
    "offline": PresenceState.OFFLINE,
    "inGameLegacy": PresenceState.IN_GAME,
}

# availability 原始值
AVAILABILITY_MAP: dict[str, str] = {
    "offline": "offline",
    "away": "away",
    "chat": "available",
    "dnd": "busy",
    "mobile": "mobile",
    "online": "available",
}


def map_game_status(raw: str | None) -> PresenceState:
    if not raw:
        return PresenceState.UNKNOWN
    return GAME_STATUS_MAP.get(raw, PresenceState.UNKNOWN)


@dataclass
class PlayerPresence:
    """单个玩家的实时 Presence 快照。"""

    puuid: str
    state: PresenceState = PresenceState.UNKNOWN
    qq_id: str = ""
    availability: str = ""  # 数据源原始 availability
    raw_game_status: str = ""  # 数据源原始 gameStatus，便于排查
    champion_id: int | None = None
    queue_id: int | None = None
    game_id: str | None = None
    game_mode: str = ""
    game_queue_type: str = ""
    ranked_tier: str = ""
    ranked_division: str = ""
    game_name: str = ""
    tag_line: str = ""
    platform: str = ""
    updated_at: float = field(default_factory=time.time)

    def is_online_like(self) -> bool:
        return self.state not in (PresenceState.OFFLINE, PresenceState.UNKNOWN)

    def to_dict(self) -> dict:
        d = {
            "puuid": self.puuid,
            "state": self.state.value,
            "qq_id": self.qq_id,
            "availability": self.availability,
            "raw_game_status": self.raw_game_status,
            "champion_id": self.champion_id,
            "queue_id": self.queue_id,
            "game_id": self.game_id,
            "game_mode": self.game_mode,
            "game_queue_type": self.game_queue_type,
            "ranked_tier": self.ranked_tier,
            "ranked_division": self.ranked_division,
            "game_name": self.game_name,
            "tag_line": self.tag_line,
            "platform": self.platform,
            "updated_at": self.updated_at,
        }
        return d


def classify_transition(old: PresenceState, new: PresenceState) -> PresenceEvent | None:
    """状态机：由 (old, new) 推导应产生的事件。

    只有真正的 transition 才返回事件；old == new 返回 None（不播报）。
    UNKNOWN 参与判断但只用于初始化场景，由上层 service 控制。
    """
    if old == new:
        return None

    old_online = old not in (PresenceState.OFFLINE, PresenceState.UNKNOWN)
    new_online = new not in (PresenceState.OFFLINE, PresenceState.UNKNOWN)

    # UNKNOWN means the source cannot observe the account. Entering or leaving
    # UNKNOWN is not a real player transition and must never produce a notice.
    if old == PresenceState.UNKNOWN or new == PresenceState.UNKNOWN:
        return None

    # 上线：OFFLINE -> 任意在线态
    if not old_online and new_online:
        return PresenceEvent.PLAYER_ONLINE

    # 下线：在线 -> OFFLINE
    if old_online and not new_online:
        return PresenceEvent.PLAYER_OFFLINE

    # 开局：非游戏 -> IN_GAME
    if new == PresenceState.IN_GAME and old != PresenceState.IN_GAME:
        return PresenceEvent.GAME_STARTED

    # 结束：IN_GAME -> 非游戏在线态
    if old == PresenceState.IN_GAME and new != PresenceState.IN_GAME:
        return PresenceEvent.GAME_ENDED

    # 其他中间态变化
    return PresenceEvent.PRESENCE_CHANGED
