"""Provider 抽象层。

架构约束：
1. 业务层（services/）只依赖这里定义的抽象接口，不得直接 import lcu/wegame 实现。
2. Provider 不允许直接往 QQ 发消息；只能通过回调上抛 PlayerPresence 快照，
   由 PresenceService -> Event -> NotificationService 链路处理。
3. 数据源只能给粗粒度状态时，必须映射为 OFFLINE/ONLINE/IN_GAME 三态，
   禁止伪造 LOBBY / MATCHMAKING / CHAMP_SELECT。
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..models.presence import PlayerPresence

logger = logging.getLogger("lol_presence.provider")

# Provider 上抛快照的回调：参数为本次有变化的 presence 列表
PresenceCallback = Callable[[list[PlayerPresence]], Awaitable[None]]

__all__ = [
    "BaseProvider",
    "MatchProvider",
    "ObservationStatus",
    "PresenceCallback",
    "ProviderCapabilities",
    "ProviderStatus",
    "_sanitize",
]


@dataclass(frozen=True)
class ObservationStatus:
    """Whether a watch account can currently observe one player."""

    observable: bool | None = None
    observer_game_name: str = ""
    observer_tag_line: str = ""

    @property
    def observer_display_name(self) -> str:
        if self.observer_tag_line:
            return f"{self.observer_game_name}#{self.observer_tag_line}"
        return self.observer_game_name


@dataclass
class ProviderCapabilities:
    """声明该 Provider 的数据能力，供能力矩阵与 auto 模式决策使用。"""

    qq_to_lol_binding: bool = False  # 能用 QQ 号直接解析 LOL 角色
    summoner_lookup: bool = False  # 按 gameName#TAG / puuid 查角色
    presence: bool = False  # 实时在线/游戏中状态
    presence_granularity: str = "none"  # none / coarse(3态) / fine(含lobby等)
    match_history: bool = False
    ranked: bool = False
    needs_watch_account: bool = False  # 是否需要长期登录的观察号
    needs_local_client: bool = False  # 是否需要本机运行 League Client


@dataclass
class ProviderStatus:
    """运行状态（供 /lol debug 与自动降级逻辑使用）。"""

    name: str = "base"
    connected: bool = False
    last_error: str = ""
    reconnects: int = 0
    last_snapshot_at: float = 0.0


class BaseProvider(ABC):
    """所有数据源的基类。

    子类实现 start/stop，并在有数据时调用 `self._emit()`。
    对轮询型数据源，提供 `poll_loop()` 辅助协程（自动指数退避）。
    """

    name = "base"
    capabilities = ProviderCapabilities()

    def __init__(self, poll_interval: float = 45.0):
        self.poll_interval = poll_interval
        self.status = ProviderStatus(name=self.name)
        self._callbacks: list[PresenceCallback] = []
        self._monitored: set[str] = set()  # puuid 集合
        self._running = False
        self._tasks: list[asyncio.Task] = []

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    @property
    def running(self) -> bool:
        return self._running

    # ---------- 订阅 ----------

    def set_monitored(self, puuids: set[str]) -> None:
        """绑定关系变化时由上层调用，Provider 只关注这些 puuid。"""
        self._monitored = set(puuids)

    @property
    def monitored(self) -> set[str]:
        return self._monitored

    # ---------- 回调 ----------

    def add_callback(self, cb: PresenceCallback) -> None:
        self._callbacks.append(cb)

    async def _emit(self, updates: list[PlayerPresence]) -> None:
        for cb in self._callbacks:
            try:
                await cb(updates)
            except Exception:  # noqa: BLE001 单个回调异常不阻断其他
                logger.exception("[LOL] presence callback error")

    # ---------- 必须实现的能力 ----------

    @abstractmethod
    async def snapshot(self) -> list[PlayerPresence]:
        """全量拉取当前监控玩家的 Presence。用于启动初始化与周期对账。"""

    @abstractmethod
    async def resolve_player(
        self, query: str, qq_id: str = ""
    ) -> PlayerPresence | None:
        """解析一个玩家（gameName#TAG 或 QQ 号），返回带基础信息的 presence。
        无法解析时返回 None。"""

    async def health_check(self) -> bool:
        """数据源当前是否可用。auto 模式据此选择/降级 Provider。"""
        return False

    async def observation_status(self, puuid: str) -> ObservationStatus:
        """Return watch-account visibility when the provider supports it."""
        return ObservationStatus()

    # ---------- 轮询辅助 ----------

    async def poll_loop(self, interval: float | None = None) -> None:
        """通用轮询循环：snapshot -> emit，异常时指数退避（1s..60s）。"""
        interval = interval or self.poll_interval
        backoff = 1.0
        while self._running:
            try:
                updates = await self.snapshot()
                if updates:
                    await self._emit(updates)
                backoff = 1.0
                self.status.connected = True
                self.status.last_snapshot_at = asyncio.get_event_loop().time()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status.last_error = str(e)
                self.status.connected = False
                self.status.reconnects += 1
                logger.warning(
                    "[LOL] provider %s snapshot failed: %s, retry in %.0fs",
                    self.name,
                    _sanitize(e),
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            await asyncio.sleep(interval)


class MatchProvider(ABC):
    """战绩/段位数据源接口（SGP 或 LCU 实现）。

    与 Presence 解耦：战绩可以来自云端 SGP，Presence 可以来自本地 LCU。
    """

    name = "base-match"

    @abstractmethod
    async def get_match_history(
        self, puuid: str, start: int = 0, count: int = 10
    ) -> list:
        """返回 list[MatchSummary]（models.match），按时间倒序。查询失败抛异常。"""

    @abstractmethod
    async def get_match_detail(self, puuid: str, game_id: str):
        """返回 MatchSummary 单局详情。"""

    @abstractmethod
    async def get_ranked(self, puuid: str) -> list:
        """返回 list[RankedInfo]。"""

    async def health_check(self) -> bool:
        return False


def _sanitize(err: object) -> str:
    """日志脱敏：剔除疑似 token/密码片段。"""
    text = str(err)
    for kw in ("password", "token", "ticket", "skey", "p_skey", "authorization"):
        if kw in text.lower():
            return text[:32] + "...(redacted)"
    return text[:200]
