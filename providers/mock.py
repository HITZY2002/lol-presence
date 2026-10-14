"""测试用 Mock 数据源。

不依赖任何真实 LOL 环境，通过 `provider.emit(...)` 或 `set_state(...)`
驱动状态机测试。设计目标（见任务要求第二十九/三十节）：
- 首次启动静默（initial snapshot 不通知）
- offline→online 只触发一次 PLAYER_ONLINE
- online→online 不通知
- online→inGame 触发 GAME_STARTED
- inGame→online 触发 GAME_ENDED
- 断线重连后状态未变化则 0 通知
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..models.presence import PlayerPresence, PresenceState
from .base import BaseProvider, MatchProvider, ProviderCapabilities
from ..models.match import MatchParticipant, MatchSummary, RankedInfo

logger = logging.getLogger("lol_presence.mock")


class MockPresenceProvider(BaseProvider):
    name = "mock"
    capabilities = ProviderCapabilities(
        presence=True,
        presence_granularity="coarse",
    )

    def __init__(self, poll_interval: float = 5.0):
        super().__init__(poll_interval=poll_interval)
        self._store: dict[str, PlayerPresence] = {}

    async def start(self) -> None:
        await super().start()
        self.status.connected = True
        logger.info("[LOL] presence provider=mock")

    async def stop(self) -> None:
        self.status.connected = False
        await super().stop()

    async def snapshot(self) -> list[PlayerPresence]:
        return [
            p
            for p in self._store.values()
            if not self._monitored or p.puuid in self._monitored
        ]

    async def resolve_player(
        self, query: str, qq_id: str = ""
    ) -> PlayerPresence | None:
        for p in self._store.values():
            if p.game_name and p.game_name.lower() == query.lower().split("#")[0]:
                return p
        return PlayerPresence(
            puuid=f"mock:{query}", state=PresenceState.UNKNOWN, game_name=query
        )

    async def health_check(self) -> bool:
        return True

    # ----- 测试驱动接口 -----

    def set_state(self, puuid: str, state: PresenceState, **kwargs) -> PlayerPresence:
        p = PlayerPresence(puuid=puuid, state=state, **kwargs)
        self._store[puuid] = p
        return p

    async def emit(self, presence: PlayerPresence) -> None:
        """外部注入一次 presence 更新（等价真实数据源上抛）。"""
        self._store[presence.puuid] = presence
        await self._emit([presence])


class MockMatchProvider(MatchProvider):
    name = "mock-match"

    def __init__(self):
        self._history: dict[str, list[MatchSummary]] = {}
        self._ranked: dict[str, list[RankedInfo]] = {}
        self.fail = False

    async def get_match_history(
        self, puuid: str, start: int = 0, count: int = 10
    ) -> list[MatchSummary]:
        if self.fail:
            raise RuntimeError("mock match provider failure")
        return (self._history.get(puuid) or [])[start : start + count]

    async def get_match_detail(self, puuid: str, game_id: str) -> MatchSummary | None:
        for m in self._history.get(puuid, []):
            if m.game_id == game_id:
                return m
        return None

    async def get_ranked(self, puuid: str) -> list[RankedInfo]:
        return self._ranked.get(puuid, [])

    async def health_check(self) -> bool:
        return not self.fail

    # ----- 测试数据注入 -----

    def add_match(self, puuid: str, summary: MatchSummary) -> None:
        self._history.setdefault(puuid, []).insert(0, summary)

    def set_ranked(self, puuid: str, ranked: list[RankedInfo]) -> None:
        self._ranked[puuid] = ranked


def make_finished_match(
    game_id: str,
    puuid: str,
    champion_id: int = 275,
    kills: int = 8,
    deaths: int = 6,
    assists: int = 12,
    win: bool = True,
    duration: int = 32 * 60 + 14,
    queue_id: int = 420,
    ended_ms: int | None = None,
) -> MatchSummary:
    """构造一局已结束的比赛（默认对应任务示例：亚索 8/6/12 胜利 32:14）。"""
    ended_ms = ended_ms if ended_ms is not None else int(time.time() * 1000)
    return MatchSummary(
        game_id=game_id,
        queue_id=queue_id,
        game_mode="CLASSIC",
        game_duration_seconds=duration,
        game_end_time_ms=ended_ms,
        participants=[
            MatchParticipant(
                puuid=puuid,
                champion_id=champion_id,
                kills=kills,
                deaths=deaths,
                assists=assists,
                win=win,
                team_id=100,
                is_user=True,
            )
        ],
    )
