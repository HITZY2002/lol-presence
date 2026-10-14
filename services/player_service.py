"""PlayerService：QQ↔LOL 绑定与玩家解析。"""

from __future__ import annotations

import logging

from ..models.player import Binding
from ..providers.base import BaseProvider
from ..storage.database import Database

logger = logging.getLogger("lol_presence.player")


class BindError(Exception):
    pass


class PlayerService:
    def __init__(self, db: Database):
        self.db = db
        # (priority, provider)，用于角色解析的 provider，按优先级排序
        self._providers: list[tuple[int, BaseProvider]] = []

    def register_provider(self, provider: BaseProvider, priority: int = 0) -> None:
        # A recovered local client gets a fresh provider instance and fresh
        # credentials. Drop the stale instance so lookups never use it again.
        self._providers = [
            item for item in self._providers if item[1].name != provider.name
        ]
        self._providers.append((priority, provider))
        self._providers.sort(key=lambda x: x[0], reverse=True)

    # ---------- 绑定 ----------

    async def bind(self, qq_id: str, query: str) -> Binding:
        """绑定：query 为 游戏名#TAG 或 召唤师名。经 provider 解析出 puuid。"""
        presence = await self._resolve(query, qq_id)
        if presence is None or not presence.puuid:
            raise BindError(
                f"无法找到角色「{query}」，请确认游戏名拼写（格式：游戏名#TAG）"
            )

        existing = await self.db.get_binding_by_puuid(presence.puuid)
        if existing and existing.qq_id != str(qq_id):
            raise BindError("该 LOL 角色已被其他 QQ 绑定")

        b = Binding(
            qq_id=str(qq_id),
            puuid=presence.puuid,
            game_name=presence.game_name,
            tag_line=presence.tag_line,
            platform=presence.platform,
        )
        await self.db.upsert_binding(b)
        logger.info("[LOL] bound qq=%s puuid=%s", b.qq_id, b.puuid[:8])
        return b

    async def auto_bind(self, qq_id: str) -> Binding:
        """QQ 自动解析绑定。仅当存在 QQ→LOL 解析能力的 provider 时可用。"""
        for _, p in self._providers:
            if p.capabilities.qq_to_lol_binding:
                presence = await p.resolve_player("", qq_id=str(qq_id))
                if presence and presence.puuid:
                    b = Binding(
                        qq_id=str(qq_id),
                        puuid=presence.puuid,
                        game_name=presence.game_name,
                        tag_line=presence.tag_line,
                        platform=presence.platform,
                    )
                    await self.db.upsert_binding(b)
                    logger.info("[LOL] auto-bound qq=%s puuid=%s", b.qq_id, b.puuid[:8])
                    return b
        raise BindError(
            "无法通过QQ自动解析英雄联盟角色，请使用：\n/lol 绑定 游戏名#TAG"
        )

    async def unbind(self, qq_id: str) -> int:
        n = await self.db.delete_binding(qq_id)
        logger.info("[LOL] unbind qq=%s removed=%d", qq_id, n)
        return n

    async def refresh_binding_names(self) -> None:
        """改名同步：绑定仍以 puuid 生效，展示名从 provider 刷新。"""
        bindings = await self.db.all_bindings()
        for b in bindings:
            for _, p in self._providers:
                try:
                    presence = await p.resolve_player(b.game_name, qq_id=b.qq_id)
                except Exception:  # noqa: BLE001
                    continue
                if presence and presence.puuid == b.puuid:
                    new_name = presence.game_name or b.game_name
                    new_tag = presence.tag_line or b.tag_line
                    if (new_name, new_tag) != (b.game_name, b.tag_line):
                        b.game_name, b.tag_line = new_name, new_tag
                        await self.db.upsert_binding(b)
                        logger.info(
                            "[LOL] binding renamed puuid=%s → %s",
                            b.puuid[:8],
                            b.display_name,
                        )
                    break

    # ---------- 查询 ----------

    async def get_binding(self, qq_id: str) -> Binding | None:
        return await self.db.get_binding(qq_id)

    async def get_binding_of_at_user(self, at_qq: str) -> Binding | None:
        return await self.db.get_binding(at_qq)

    async def find_by_display_name(self, name: str) -> Binding | None:
        if "#" in name:
            game_name, tag = name.split("#", 1)
        else:
            game_name, tag = name, ""
        return await self.db.find_binding_by_name(game_name, tag)

    async def all_bindings(self) -> list[Binding]:
        return await self.db.all_bindings()

    async def bindings_of_qq_ids(self, qq_ids: list[str]) -> list[Binding]:
        return await self.db.bindings_of_group(qq_ids)

    # ---------- 内部 ----------

    async def _resolve(self, query: str, qq_id: str):
        last_err: Exception | None = None
        for _, p in self._providers:
            try:
                presence = await p.resolve_player(query, qq_id=qq_id)
                if presence and presence.puuid:
                    return presence
            except Exception as e:  # noqa: BLE001
                last_err = e
                continue
        if last_err:
            logger.warning("[LOL] resolve %s failed: %s", query[:32], last_err)
        return None
