"""MatchService：战绩与段位查询。

数据源优先级由上层注入（SGP > LCU）。所有查询结果缓存进 SQLite，
供"游戏结束播报"的退避重试与 /lol 今日 使用。
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..models.match import MatchSummary, RankedInfo
from ..providers.base import MatchProvider
from ..storage.database import Database

logger = logging.getLogger("lol_presence.match")

# 游戏结束后战绩延迟出现的退避序列（秒）
GAME_END_BACKOFF = [10, 20, 40, 60]


class MatchNotReadyError(Exception):
    pass


class MatchService:
    def __init__(self, db: Database):
        self.db = db
        self._providers: list[MatchProvider] = []
        self._cache_ttl = 60.0
        self._mem_cache: dict[str, tuple[float, list[MatchSummary]]] = {}

    def register_provider(self, provider: MatchProvider) -> None:
        # Reconnecting LCU/SGP providers carry new session credentials.
        self._providers = [p for p in self._providers if p.name != provider.name]
        self._providers.append(provider)

    @property
    def available(self) -> bool:
        return bool(self._providers)

    # ---------- 查询 ----------

    async def get_history(
        self, puuid: str, count: int = 10, fresh: bool = False
    ) -> list[MatchSummary]:
        if not self._providers:
            raise MatchNotReadyError("战绩数据源不可用")
        cache_key = f"{puuid}:{count}"
        cached = self._mem_cache.get(cache_key)
        now = time.time()
        if not fresh and cached and now - cached[0] < self._cache_ttl:
            return cached[1]
        last_err: Exception | None = None
        for p in self._providers:
            try:
                matches = await p.get_match_history(puuid, start=0, count=count)
                for m in matches:
                    await self.db.save_match_raw(
                        m.game_id, puuid, {"game_id": m.game_id}, m.game_end_time_ms
                    )
                self._mem_cache[cache_key] = (now, matches)
                return matches
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("[LOL] match history via %s failed: %s", p.name, e)
        raise MatchNotReadyError(str(last_err or "no provider"))

    async def get_ranked(self, puuid: str) -> list[RankedInfo]:
        if not self._providers:
            raise MatchNotReadyError("段位数据源不可用")
        for p in self._providers:
            try:
                return await p.get_ranked(puuid)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[LOL] ranked via %s failed: %s", p.name, e)
        raise MatchNotReadyError("all ranked providers failed")

    # ---------- 结算播报的等待查询 ----------

    async def wait_for_latest_match(
        self,
        puuid: str,
        game_id_hint: str | None,
        started_after_ms: int | None = None,
        backoff: list[int] | None = None,
    ) -> MatchSummary | None:
        """游戏结束后按指数退避等待最新战绩出现。

        - game_id_hint: presence 里带出的 gameId（如有），命中直接返回
        - started_after_ms: 只接受结束时间晚于该时间戳(毫秒)的对局
        - 全部退避尝试后仍未出现：返回 None（上层只播报"打完了"）
        """
        for i, delay in enumerate(backoff or GAME_END_BACKOFF):
            if i > 0:
                await asyncio.sleep(delay)
            try:
                # fresh=True：结算等待路径不能读旧缓存，否则空列表会被放大
                matches = await self.get_history(puuid, count=5, fresh=True)
            except MatchNotReadyError:
                continue
            for m in matches:
                if game_id_hint and str(m.game_id) == str(game_id_hint):
                    return m
                if (
                    started_after_ms
                    and m.game_end_time_ms
                    and m.game_end_time_ms < started_after_ms
                ):
                    continue
                # 没有提示 id 时，取最新一局
                return m
        return None

    async def get_today_matches(
        self, puuid: str, count: int = 30
    ) -> list[MatchSummary]:
        from datetime import datetime

        try:
            matches = await self.get_history(puuid, count=count)
        except MatchNotReadyError:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        return [
            m
            for m in matches
            if m.game_end_time_ms
            and datetime.fromtimestamp(m.game_end_time_ms / 1000).strftime("%Y-%m-%d")
            == today
        ]
