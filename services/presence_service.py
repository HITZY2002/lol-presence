"""PresenceService：状态机核心。

职责：
1. 接收任意 Provider 上抛的 PlayerPresence 快照
2. 与上次状态比对，推导 transition 事件
3. 初始快照静默（不得因插件启动/重连刷屏）
4. 状态持久化（重启后恢复 old_state）
5. 广播事件给订阅者（NotificationService）

事件链路：Provider -> PresenceService.on_snapshot() -> Event -> subscribers
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..models.presence import (
    PlayerPresence,
    PresenceEvent,
    PresenceState,
    classify_transition,
)
from ..storage.database import Database

logger = logging.getLogger("lol_presence.service")

EventCallback = Callable[
    [PresenceEvent, PlayerPresence, PlayerPresence], Awaitable[None]
]


@dataclass
class TransitionEvent:
    event: PresenceEvent
    old: PlayerPresence
    new: PlayerPresence


class PresenceService:
    def __init__(self, db: Database, initialization_grace: float = 15.0):
        self.db = db
        self._state: dict[str, PlayerPresence] = {}  # puuid -> 最近 presence
        self._callbacks: list[EventCallback] = []
        # 封口标志：False 时快照只登记不广播。
        # 启动后第一批快照、以及断线重连后的第一批快照都必须静默封口，
        # 这是"首启不刷屏"的硬要求（任务第九/二十七/二十八节）。
        self._sealed = False
        self._grace = initialization_grace
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        """从数据库恢复上次状态作为 old_state（不产生任何事件）。

        注意：initialize 后仍处于未封口状态，等待 Provider 的第一批
        全量快照完成封口。"""
        self._state = await self.db.load_all_presence()
        self._sealed = False
        logger.info(
            "[LOL] presence service initialized with %d cached states", len(self._state)
        )

    @property
    def initialized(self) -> bool:
        """是否已完成首批快照封口（之后的变化才允许播报）。"""
        return self._sealed

    def mark_reconnected(self) -> None:
        """断线重连后调用：接下来一轮 snapshot 作为 initial snapshot，
        只更新 old_state 不发事件。"""
        self._sealed = False

    # ---------- 订阅 ----------

    def subscribe(self, cb: EventCallback) -> None:
        self._callbacks.append(cb)

    # ---------- 核心入口 ----------

    async def on_snapshot(self, updates: list[PlayerPresence]) -> int:
        """处理一批 presence 更新，返回产生的事件数。

        未封口时（启动首批 / 重连首批）只登记状态，不广播。
        """
        events = 0
        async with self._lock:
            initializing = not self._sealed
            for new in updates:
                old = self._state.get(new.puuid)
                self._state[new.puuid] = new
                # Preserve update ordering. Fire-and-forget writes can commit an
                # older state after a newer WebSocket event during bursts.
                await self.db.save_presence(new)

                if initializing:
                    continue

                if old is None:
                    # 初始化后出现的新 puuid：第一次见到只登记，不通知
                    # （避免绑定瞬间把"已在在线"误播为上线）
                    continue

                event = classify_transition(old.state, new.state)
                if event is None:
                    continue

                if event == PresenceEvent.PRESENCE_CHANGED:
                    # 中间态变化仅登记，不播报（可扩展：群内提示匹配中）
                    continue

                events += 1
                await self._dispatch(event, old, new)

                # 边界：直接从离线进入游戏（上线时已在队列中）
                # => 同时产生上线 + 开局两个事件
                if (
                    event == PresenceEvent.PLAYER_ONLINE
                    and new.state == PresenceState.IN_GAME
                ):
                    events += 1
                    await self._dispatch(PresenceEvent.GAME_STARTED, old, new)

            if initializing:
                # 本批为 initial snapshot：全部登记完成后恢复事件产生。
                # 启动/断线重连场景：状态未变化 => 0 事件；真实下线/上线照样会有事件。
                self._sealed = True
                logger.info(
                    "[LOL] presence snapshot sealed (%d states)", len(self._state)
                )
        return events

    async def _dispatch(
        self, event: PresenceEvent, old: PlayerPresence, new: PlayerPresence
    ) -> None:
        logger.info(
            "[LOL] player state changed puuid=%s %s→%s event=%s",
            new.puuid[:8],
            old.state.value,
            new.state.value,
            event.value,
        )
        for cb in self._callbacks:
            try:
                await cb(event, old, new)
            except Exception:  # noqa: BLE001
                logger.exception("[LOL] event callback error")

    # ---------- 查询接口 ----------

    def get(self, puuid: str) -> PlayerPresence | None:
        return self._state.get(puuid)

    def all_states(self) -> dict[str, PlayerPresence]:
        return dict(self._state)

    def states_for(self, puuids: list[str]) -> dict[str, PlayerPresence]:
        keys = set(puuids)
        return {k: v for k, v in self._state.items() if k in keys}

    def online_in(self, puuids: list[str]) -> list[PlayerPresence]:
        got = self.states_for(puuids)
        return [p for p in got.values() if p.is_online_like()]

    def in_game_in(self, puuids: list[str]) -> list[PlayerPresence]:
        got = self.states_for(puuids)
        return [p for p in got.values() if p.state == PresenceState.IN_GAME]

    def party_in(self, puuids: list[str]) -> list[PlayerPresence]:
        """可开黑：ONLINE / LOBBY，排除 IN_GAME / OFFLINE。"""
        got = self.states_for(puuids)
        return [
            p
            for p in got.values()
            if p.state in (PresenceState.ONLINE, PresenceState.LOBBY)
        ]

    @property
    def is_initializing(self) -> bool:
        return not self.initialized
