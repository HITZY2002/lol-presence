"""NotificationService：Presence 事件 → QQ 群播报。

职责：
1. 订阅 PresenceService 的状态机事件
2. 按管理员设置的群播报开关过滤
3. 上线播报执行冷却去重（默认 30 分钟，群可覆盖）
4. 结算播报执行战绩退避等待（10/20/40/60s）
5. 通过 AstrBot context.send_message(umo, MessageChain) 主动发消息

约束：不伪造数据；战绩未出时只播报"打完了"。
"""

from __future__ import annotations

import asyncio
import logging
import time

from astrbot.api.event import MessageChain

from ..models.match import MatchSummary
from ..models.player import Binding
from ..models.presence import PlayerPresence, PresenceEvent
from ..storage.database import Database
from ..utils.champion import ChampionMap
from ..utils.formatter import fmt_game_end, fmt_game_start
from .match_service import MatchService
from .player_service import PlayerService

logger = logging.getLogger("lol_presence.notify")


class NotificationService:
    def __init__(
        self,
        context,
        db: Database,
        players: PlayerService,
        matches: MatchService,
        champions: ChampionMap,
        online_cooldown_minutes: int = 30,
    ):
        self.context = context
        self.db = db
        self.players = players
        self.matches = matches
        self.champions = champions
        self.default_online_cooldown = max(0, online_cooldown_minutes) * 60
        self._pending_end_tasks: dict[str, asyncio.Task] = {}
        self.sent_count = 0

    # ---------- 事件入口 ----------

    async def on_presence_event(
        self, event: PresenceEvent, old: PlayerPresence, new: PlayerPresence
    ) -> None:
        if event == PresenceEvent.PLAYER_ONLINE:
            await self._handle_online(new)
        elif event == PresenceEvent.GAME_STARTED:
            await self._handle_game_started(old, new)
        elif event == PresenceEvent.GAME_ENDED:
            await self._handle_game_ended(old, new)
        elif event == PresenceEvent.PLAYER_OFFLINE:
            if old.state.value == "IN_GAME":
                # 游戏中直接关闭客户端：仍按结算流程退避确认战绩，
                # 战绩未出现则只播报"打完了"，不伪造数据
                await self._handle_game_ended(old, new)
            return

    # ---------- 上线 ----------

    async def _handle_online(self, presence: PlayerPresence) -> None:
        binding = await self.db.get_binding_by_puuid(presence.puuid)
        if not binding:
            return
        groups = await self.db.all_broadcast_groups()
        for g in groups:
            if not g.get("online_notification"):
                continue
            group_id = str(g["group_id"])
            cooldown_min = g.get("online_cooldown_minutes", -1)
            cooldown = (
                cooldown_min * 60
                if cooldown_min and cooldown_min >= 0
                else self.default_online_cooldown
            )
            if cooldown > 0:
                last = await self.db.get_last_online_notify(presence.puuid, group_id)
                if time.time() - last < cooldown:
                    logger.info(
                        "[LOL] online notify suppressed by cooldown puuid=%s group=%s",
                        presence.puuid[:8],
                        group_id,
                    )
                    continue
            region_text = _region(binding.platform or presence.platform)
            name = binding.game_name or binding.display_name
            if region_text:
                msg = f"🟢 {name} 上号了\n\n{region_text} · {binding.display_name}"
            else:
                msg = f"🟢 {name} 上号了"
            await self._send(g["umo"], msg)
            await self.db.mark_online_notified(presence.puuid, group_id)

    # ---------- 开局 ----------

    async def _handle_game_started(
        self, old: PlayerPresence, new: PlayerPresence
    ) -> None:
        binding = await self.db.get_binding_by_puuid(new.puuid)
        if not binding:
            return
        groups = await self.db.all_broadcast_groups()
        for g in groups:
            if not g.get("game_start_notification"):
                continue
            group_id = str(g["group_id"])
            last_gid = await self.db.get_last_game_id(new.puuid, group_id)
            if new.game_id and last_gid == new.game_id:
                continue  # 同一局去重
            msg = fmt_game_start(binding, new, self.champions)
            await self._send(g["umo"], msg)
            if new.game_id:
                await self.db.mark_game_notified(new.puuid, group_id, new.game_id)

    # ---------- 结算 ----------

    async def _handle_game_ended(
        self, old: PlayerPresence, new: PlayerPresence
    ) -> None:
        binding = await self.db.get_binding_by_puuid(new.puuid)
        if not binding:
            return
        groups = [
            g
            for g in await self.db.all_broadcast_groups()
            if g.get("game_end_notification")
        ]
        if not groups:
            return
        # 每个玩家同一局只允许一个退避任务
        key = new.puuid
        prev = self._pending_end_tasks.get(key)
        if prev and not prev.done():
            return
        self._pending_end_tasks[key] = asyncio.create_task(
            self._resolve_game_end(binding, old, new, groups)
        )

    async def _resolve_game_end(
        self,
        binding: Binding,
        old: PlayerPresence,
        new: PlayerPresence,
        groups: list[dict],
    ) -> None:
        try:
            match: MatchSummary | None = None
            if self.matches.available:
                started_after = old.updated_at * 1000 if old.updated_at else None
                match = await self.matches.wait_for_latest_match(
                    new.puuid,
                    game_id_hint=old.game_id or new.game_id,
                    started_after_ms=started_after,
                )
            msg = fmt_game_end(binding, new, match, self.champions)
            for g in groups:
                group_id = str(g["group_id"])
                # 结算去重用独立的 end 键，不与开局去重互相干扰
                last_gid = await self.db.get_last_game_id(
                    new.puuid, group_id, kind="end"
                )
                gid = match.game_id if match else (new.game_id or "")
                if gid and last_gid == gid and match is not None:
                    continue
                await self._send(g["umo"], msg)
                if gid:
                    await self.db.mark_game_notified(
                        new.puuid, group_id, gid, kind="end"
                    )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("[LOL] game end notification failed")
        finally:
            self._pending_end_tasks.pop(new.puuid, None)

    # ---------- 发送 ----------

    async def _send(self, umo: str, text: str) -> None:
        try:
            chain = MessageChain(chain=[_Plain(text)])
            ok = await self.context.send_message(umo, chain)
            self.sent_count += 1
            logger.info("[LOL] notification sent umo=%s ok=%s", _mask_umo(umo), ok)
        except Exception as e:  # noqa: BLE001
            logger.warning("[LOL] send to %s failed: %s", _mask_umo(umo), e)

    async def stop(self) -> None:
        for t in self._pending_end_tasks.values():
            t.cancel()
        self._pending_end_tasks.clear()


# 延迟导入以保持模块对 AstrBot 组件的弱依赖（测试环境无 aiocqhttp）
def _Plain(text: str):
    from astrbot.api.message_components import Plain

    return Plain(text)


def _region(platform: str) -> str:
    from ..utils.formatter import platform_name

    return platform_name(platform or "")


def _mask_umo(umo: str) -> str:
    """日志中掩码群号，保留平台与会话类型。"""
    parts = umo.split(":")
    if len(parts) >= 3:
        parts[-1] = parts[-1][:3] + "***"
        return ":".join(parts)
    return umo
