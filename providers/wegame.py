"""腾讯 WeGame 云端数据源。

【2026-08 调研结论，必须先读 docs/research.md】

1. Presence（他人实时在线/游戏中）：
   不存在可用的纯云端接口。
   - 旧 TGP `get_battle_topbar_info` 响应中的 `online_status` 字段（2022）
     是云端在线状态的书面证据，但其凭据体系（掌上 WeGame App tgp_ticket）
     已随 App 停运（2022-09-08）断链 → **legacy / unusable**。
   - 掌上英雄联盟 App 的好友在线接口无公开逆向复现 → **无法验证**。
   因此本类 capabilities.presence = False，auto 模式会自动跳过。

2. 战绩（pallas 接口，2026-06 由 Kroomax/LOLHistory 实测可用）：
   `www.wegame.com.cn/api/v1/wegame.pallas.game.LolBattle/GetBattleList`
   仅能查 Cookie 属主自己（puin 绑定），查不了任意绑定群友，
   对本插件"按 puuid 查任意群友"的场景没有增益 → 不默认接入。

3. QQ → LOL 角色映射（tcall basic_info，按 qquin+area_id 查任意 QQ）：
   生产 JS（wegame.gtimg.com frame.*.js，2026-08-29 抓取）确认前端
   以 `[2,{qquin,area_id}]` 调用 `api.pallas.tgp.qq.com/core/tcall`
   获取任意玩家 basic_info（name/icon），需助手页 Cookie 环境。
   置信度：中（接口存在性高，脱机可调用性未验证）。
   本类实现 `resolve_player(qq_id)` 尝试该链路；成功即可支持
   `/lol 自动绑定`。默认关闭，配置 wegame.cookie 后启用。

本类是"将来云端能力恢复时的替换点"：业务层只依赖 Provider 抽象，
届时补齐 presence 实现即可切换，无需改动播报逻辑。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiohttp

from ..models.presence import PlayerPresence, PresenceState
from .base import BaseProvider, ProviderCapabilities, _sanitize

logger = logging.getLogger("lol_presence.wegame")

TCALL_URL = "https://www.wegame.com.cn/api/pallas/core/tcall"

UA_WEGAME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass
class WegameCookies:
    """WeGame 助手页 Cookie（用户从浏览器抓取，独立保存，日志脱敏）。"""

    cookie: str = ""  # 完整 Cookie 头
    area_id: str = "1"  # 大区 ID（1=艾欧尼亚）


class WegamePresenceProvider(BaseProvider):
    name = "wegame"
    capabilities = ProviderCapabilities(
        qq_to_lol_binding=True,  # 仅当配置 Cookie 后经 tcall 可用
        summoner_lookup=False,
        presence=False,  # 调研结论：云端无 Presence 接口
        presence_granularity="none",
        match_history=False,  # pallas 仅限 Cookie 属主自己
        ranked=False,
        needs_watch_account=False,
        needs_local_client=False,
    )

    def __init__(self, cookies: WegameCookies, poll_interval: float = 45.0):
        super().__init__(poll_interval=poll_interval)
        self.cookies = cookies

    @classmethod
    def from_config(cls, cfg: dict) -> "WegamePresenceProvider | None":
        cookie = str(cfg.get("cookie") or "").strip()
        if not cookie:
            return None
        return cls(WegameCookies(cookie=cookie, area_id=str(cfg.get("area_id") or "1")))

    async def health_check(self) -> bool:
        # 云端无 Presence 能力：对播报场景永远"不可用"（auto 会跳过）。
        # 返回 False 是如实的架构表达，不是故障。
        return False

    async def snapshot(self) -> list[PlayerPresence]:
        return []

    async def resolve_player(
        self, query: str, qq_id: str = ""
    ) -> PlayerPresence | None:
        """QQ → LOL 角色（tcall basic_info，实验性）。

        成功返回仅含 puuid/game_name 的占位 presence（UNKNOWN 状态，
        不代表在线状态）。失败返回 None 并由上层提示手动绑定。
        """
        if not qq_id:
            return None
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={
                    "User-Agent": UA_WEGAME,
                    "Cookie": self.cookies.cookie,
                    "Referer": "https://www.wegame.com.cn/helper/lol/index.html",
                },
            ) as session:
                async with session.post(
                    TCALL_URL,
                    data={
                        "p": '[2,{"qquin":"%s","area_id":"%s"}]'
                        % (qq_id, self.cookies.area_id),
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json(content_type=None)
        except Exception as e:  # noqa: BLE001
            logger.debug("[LOL] wegame tcall failed: %s", _sanitize(e))
            return None
        info = _extract_basic_info(data)
        if not info:
            return None
        return PlayerPresence(
            puuid=str(info.get("puuid") or ""),
            state=PresenceState.UNKNOWN,
            game_name=str(info.get("name") or ""),
            platform=str(self.cookies.area_id),
        )

    async def start(self) -> None:
        await super().start()
        logger.info(
            "[LOL] wegame provider started（无 Presence 能力，仅实验性 QQ 解析）"
        )


def _extract_basic_info(data) -> dict | None:
    """tcall 响应结构无公开文档，宽松抽取 basic_info/name 字段。"""
    if not isinstance(data, dict):
        return None
    body = data.get("body") or data.get("data") or {}
    if isinstance(body, dict):
        basic = body.get("basic_info") or body.get("basicInfo")
        if isinstance(basic, dict):
            return basic
        if body.get("name"):
            return body
    return None
