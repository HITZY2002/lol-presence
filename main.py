"""AstrBot 插件：英雄联盟国服上线提醒 (LOL Presence)

第一优先级：群成员上号 / 开局 / 结算自动播报。
架构：Provider(数据源) -> PresenceService(状态机) -> Event -> NotificationService -> AstrBot。

业务层不直接依赖具体数据源；云端 Provider 可用时优先云端，
否则回退本地 LCU 观察号（数据能力矩阵见 docs/research.md）。
"""

from __future__ import annotations

import asyncio
import os
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, AtAll
from astrbot.api.star import Context, Star, StarTools, register

from . import commands
from .providers import build_providers
from .services.match_service import MatchService
from .services.notification_service import NotificationService
from .services.player_service import PlayerService
from .services.presence_service import PresenceService
from .storage.database import Database
from .utils.champion import ChampionMap
from .utils.formatter import fmt_help

PLUGIN_NAME = "astrbot_plugin_lol_presence"


@register("astrbot_plugin_lol_presence", "HITZY2002", "英雄联盟国服上线提醒", "v1.0.0")
class LolPresencePlugin(Star):
    """英雄联盟国服上线提醒：上号/开局/结算自动播报 + /lol 查询指令。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.log = logger

        self.db: Database | None = None
        self.champions = ChampionMap(self._cfg("champion_cdn", default="") or None)
        self.players: PlayerService | None = None
        self.matches: MatchService | None = None
        self.presence: PresenceService | None = None
        self.notifier: NotificationService | None = None
        self.provider = None
        self.zhangmeng_provider = None
        self.zhangmeng_webhook = None

        self._tasks: list[asyncio.Task] = []
        self._started_at = time.time()
        self._presence_mode = "auto"
        self._presence_poll_interval = 45
        self._presence_reconcile_interval = 300
        self._presence_cfg: dict = {}

    # ---------------- 配置 ----------------

    def _cfg(self, *path: str, default=None):
        node = self.config
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.db = Database(data_dir)
        await self.db.init()

        self.players = PlayerService(self.db)
        self.matches = MatchService(self.db)
        self.presence = PresenceService(self.db)

        online_cooldown = int(
            self._cfg("notification", "online_cooldown_minutes", default=30) or 30
        )
        self.notifier = NotificationService(
            self.context,
            self.db,
            self.players,
            self.matches,
            self.champions,
            online_cooldown_minutes=online_cooldown,
        )
        self.presence.subscribe(self.notifier.on_presence_event)
        # Restore cached state before a provider can emit its first snapshot.
        # Otherwise a fast WebSocket connection can race with initialize().
        await self.presence.initialize()

        self._presence_mode = str(
            self._cfg("presence_provider", "mode", default="auto") or "auto"
        )
        self._presence_poll_interval = max(
            10,
            int(self._cfg("presence_provider", "poll_interval", default=45) or 45),
        )
        self._presence_reconcile_interval = max(
            60,
            int(
                self._cfg("presence_provider", "reconcile_interval", default=300) or 300
            ),
        )
        self._presence_cfg = self._cfg("presence_provider", default={}) or {}
        self.provider = await build_providers(
            mode=self._presence_mode,
            poll_interval=self._presence_poll_interval,
            reconcile_interval=self._presence_reconcile_interval,
            cfg=self._presence_cfg,
        )
        if self.provider is None:
            logger.warning(
                "[LOL] 无可用 Presence 数据源，自动播报停用；绑定与缓存查询仍可用"
            )
        else:
            self.provider.add_callback(self._on_provider_snapshot)
            self.players.register_provider(self.provider, priority=0)
            bindings = await self.players.all_bindings()
            self.provider.set_monitored({b.puuid for b in bindings})
            try:
                await self.provider.start()
            except Exception as e:  # noqa: BLE001
                logger.warning("[LOL] provider first start failed: %s", e)
        self._tasks.append(
            asyncio.create_task(
                self._provider_supervisor(), name="lol-provider-supervisor"
            )
        )

        self._setup_match_providers()

        # 资源守护：观察号机可选开启（自动关闭客户端 UI 省 ~70% 资源）
        self._setup_resource_saver()

        # 掌盟通知 Provider（增量接入，不影响现有 LCU/SGP）
        self._setup_zhangmeng()

        self._tasks.append(
            asyncio.create_task(self.champions.ensure_loaded(), name="lol-champions")
        )
        logger.info(
            "[LOL] plugin initialized provider=%s match_available=%s zhangmeng=%s",
            getattr(self.provider, "name", "none"),
            self.matches.available,
            "enabled" if self.zhangmeng_provider else "disabled",
        )

    async def terminate(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=3.0)
        self._tasks.clear()
        if self.provider:
            await self.provider.stop()
        if self.zhangmeng_provider:
            await self.zhangmeng_provider.stop()
        if self.notifier:
            await self.notifier.stop()
        logger.info("[LOL] plugin terminated")

    # ---------------- Provider 管理 ----------------

    async def _on_provider_snapshot(self, updates) -> None:
        if self.db:
            for update in updates:
                if not update.game_name:
                    continue
                binding = await self.db.get_binding_by_puuid(update.puuid)
                if not binding:
                    continue
                identity = (
                    update.game_name or binding.game_name,
                    update.tag_line or binding.tag_line,
                    update.platform or binding.platform,
                )
                if identity != (
                    binding.game_name,
                    binding.tag_line,
                    binding.platform,
                ):
                    binding.game_name, binding.tag_line, binding.platform = identity
                    await self.db.upsert_binding(binding)
                    logger.info(
                        "[LOL] binding identity refreshed puuid=%s name=%s",
                        binding.puuid[:8],
                        binding.display_name,
                    )
        if self.presence:
            await self.presence.on_snapshot(updates)

    async def _provider_supervisor(self) -> None:
        """Rediscover providers after the local League client stops or restarts."""
        while True:
            provider = self.provider
            healthy = False
            if provider is not None and provider.running:
                try:
                    healthy = await provider.health_check()
                except Exception:  # noqa: BLE001
                    healthy = False

            if not healthy:
                if provider is not None:
                    logger.info(
                        "[LOL] provider %s unavailable, rediscovering", provider.name
                    )
                    try:
                        await provider.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    self.provider = None

                try:
                    candidate = await build_providers(
                        mode=self._presence_mode,
                        poll_interval=self._presence_poll_interval,
                        reconcile_interval=self._presence_reconcile_interval,
                        cfg=self._presence_cfg,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("[LOL] provider rediscovery failed: %s", e)
                    candidate = None

                if candidate is not None:
                    candidate.add_callback(self._on_provider_snapshot)
                    self.players.register_provider(candidate, priority=0)
                    bindings = await self.players.all_bindings()
                    candidate.set_monitored({b.puuid for b in bindings})
                    if self.presence:
                        # Seal the first snapshot after recovery to avoid replay.
                        # This must happen before start() can emit anything.
                        self.presence.mark_reconnected()
                    try:
                        await candidate.start()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[LOL] recovered provider start failed: %s", e)
                        await candidate.stop()
                    else:
                        self.provider = candidate
                        self._setup_match_providers()
                        logger.info("[LOL] provider recovered: %s", candidate.name)
            await asyncio.sleep(60)

    def sync_monitored(self) -> None:
        """把当前所有绑定 puuid 同步给 Provider（后台执行）。"""
        self._tasks.append(asyncio.create_task(self._sync_monitored_async()))

    async def _sync_monitored_async(self) -> None:
        await self.refresh_monitored()

    async def refresh_monitored(self, reconcile: bool = False) -> None:
        """Synchronize bindings and optionally refresh query state immediately."""
        if not (self.provider and self.players):
            return
        bindings = await self.players.all_bindings()
        self.provider.set_monitored({b.puuid for b in bindings})
        if reconcile:
            try:
                updates = await self.provider.snapshot()
                if updates:
                    await self._on_provider_snapshot(updates)
            except Exception as e:  # noqa: BLE001
                logger.warning("[LOL] immediate presence refresh failed: %s", e)

    def _setup_match_providers(self) -> None:
        mode = str(self._cfg("match_provider", "mode", default="auto") or "auto")
        if mode == "off":
            return
        from .providers.lcu import LcuMatchProvider, LcuPresenceProvider
        from .providers.sgp import SgpProvider

        if isinstance(self.provider, LcuPresenceProvider):
            # SGP 优先（战绩/段位可靠性高，token 来自本客户端），
            # LCU 旧接口作为兜底。
            try:
                server_id = str(
                    self._cfg("match_provider", "server_id", default="hn1") or "hn1"
                )
                sgp = SgpProvider(self.provider.client, server_id=server_id)
                self.matches.register_provider(sgp)
                logger.info("[LOL] match provider=sgp server=%s", server_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("[LOL] sgp provider init failed: %s", e)
            self.matches.register_provider(LcuMatchProvider(self.provider.client))
            logger.info("[LOL] match provider fallback=lcu")
        elif mode == "sgp":
            logger.warning(
                "[LOL] SGP 需要 LCU 观察号提供 token；当前无 LCU，战绩功能不可用"
            )

    def _setup_resource_saver(self) -> None:
        from .providers.lcu import LcuPresenceProvider

        rs_cfg = self._cfg("resource_saver", default={}) or {}
        if not rs_cfg.get("enabled"):
            return
        if not isinstance(self.provider, LcuPresenceProvider):
            logger.info("[LOL] resource saver 仅在本地 LCU 模式下可用，已跳过")
            return
        from .services.resource_saver import ResourceSaver

        saver = ResourceSaver(
            mode=str(rs_cfg.get("mode") or "kill_ux"),
            delay_seconds=max(10.0, float(rs_cfg.get("delay_seconds") or 90)),
            check_interval=max(30.0, float(rs_cfg.get("check_interval") or 120)),
            set_priority=bool(rs_cfg.get("set_priority", True)),
            get_client=lambda: (
                self.provider.client
                if isinstance(self.provider, LcuPresenceProvider)
                and self.provider.running
                else None
            ),
        )
        self._tasks.append(
            asyncio.create_task(saver.run_loop(), name="lol-resource-saver")
        )
        logger.info("[LOL] resource saver enabled mode=%s", saver.mode)

    # ---------------- 掌盟通知 ----------------

    def _setup_zhangmeng(self) -> None:
        """初始化掌盟通知 Provider 和 Webhook 路由（增量接入，不影响现有 LCU/SGP）。"""
        zm_cfg = self._cfg("zhangmeng", default={}) or {}
        if not zm_cfg.get("enabled", False):
            logger.info(
                "[LOL] zhangmeng notify provider disabled (config zhangmeng.enabled=false)"
            )
            return

        try:
            from .providers.zhangmeng_notify import ZhangMengNotifyProvider
            from .webhook.zhangmeng import ZhangMengWebhookHandler
        except ImportError as e:
            logger.warning("[LOL] zhangmeng module import failed: %s", e)
            return

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        patterns_path = zm_cfg.get("patterns_path") or os.path.join(
            plugin_dir, "data", "zhangmeng_patterns.json"
        )
        log_dir = zm_cfg.get("log_dir") or os.path.join(plugin_dir, "logs")
        secret = str(zm_cfg.get("secret", "") or "")
        default_cooldown = int(zm_cfg.get("default_cooldown_minutes", 10) or 10)

        self.zhangmeng_provider = ZhangMengNotifyProvider(
            db=self.db,
            patterns_path=patterns_path,
            default_cooldown_minutes=default_cooldown,
        )
        self.zhangmeng_provider.add_callback(self._on_provider_snapshot)
        # 掌盟 Provider 是被动推送模式，不需要轮询，但需要 start() 设置 running 状态
        asyncio.create_task(self.zhangmeng_provider.start())

        self.zhangmeng_webhook = ZhangMengWebhookHandler(
            provider=self.zhangmeng_provider,
            secret=secret,
            log_dir=log_dir,
        )

        # 注册 Webhook 路由：POST /api/plug/astrbot_plugin_lol_presence/zhangmeng/notify
        async def zhangmeng_notify_view():
            return await self.zhangmeng_webhook.handle()

        try:
            self.context.register_web_api(
                route="zhangmeng/notify",
                view_handler=zhangmeng_notify_view,
                methods=["POST"],
                desc="掌盟通知 Webhook（SmsForwarder → AstrBot）",
            )
            logger.info(
                "[LOL] zhangmeng webhook registered: POST /api/plug/%s/zhangmeng/notify (secret=%s)",
                PLUGIN_NAME,
                "set" if secret else "none",
            )
        except Exception as e:
            logger.warning("[LOL] zhangmeng webhook register failed: %s", e)

    # ---------------- 指令 ----------------

    @filter.command("lol", alias={"LOL", "LoL"})
    async def lol_command(self, event: AstrMessageEvent):
        """/lol 指令组。子指令见 /lol 帮助"""
        args = self._parse_args(event)
        sub = args[0] if args else ""

        # 群内自动登记 umo（后续主动播报的基础）
        group_id = event.get_group_id()
        if group_id and self.db is not None:
            await self.db.upsert_group(group_id, event.unified_msg_origin)

        routing = {
            "": commands.status.cmd_me,
            "我的": commands.status.cmd_me,
            "在线": commands.status.cmd_online,
            "谁在玩": commands.status.cmd_whos_playing,
            "状态": commands.status.cmd_status,
            "车队": commands.status.cmd_party,
            "战绩": commands.match.cmd_history,
            "最近": commands.match.cmd_history,
            "段位": commands.match.cmd_ranked,
            "今日": commands.match.cmd_today,
            "绑定": commands.bind.cmd_bind,
            "自动绑定": commands.bind.cmd_autobind,
            "解绑": commands.bind.cmd_unbind,
            "帮助": self._cmd_help,
            "help": self._cmd_help,
            "播报": self._cmd_group_switch,
            "上线播报": self._cmd_group_switch,
            "开局播报": self._cmd_group_switch,
            "结算播报": self._cmd_group_switch,
        }
        handler = routing.get(sub)
        if handler is None:
            yield event.plain_result(fmt_help())
            return
        async for res in handler(self, event, args[1:]):
            yield res

    # ----- 供 commands/ 模块使用的辅助 -----

    @property
    def logger(self):
        return logger

    @staticmethod
    def _parse_args(event: AstrMessageEvent) -> list[str]:
        """拆出 lol 之后的参数。"""
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            text = text[1:]
        for prefix in ("lol", "LOL", "LoL"):
            if text.lower().startswith(prefix.lower()):
                text = text[len(prefix) :]
                break
        return [a for a in text.split() if a.strip()]

    @staticmethod
    def _at_qq(event: AstrMessageEvent) -> str | None:
        for comp in event.get_messages():
            if isinstance(comp, At) and not isinstance(comp, AtAll):
                return str(comp.qq)
        return None

    async def resolve_target(
        self, event: AstrMessageEvent, args: list[str]
    ) -> tuple[str | None, str]:
        """查询目标：优先 @，其次绑定名，最后自己。返回 (qq_id, 错误提示)。"""
        at = self._at_qq(event)
        if at:
            return at, ""
        name_args = [a for a in args if not a.isdigit()]
        if name_args:
            b = await self.players.find_by_display_name(name_args[0])
            if b:
                return b.qq_id, ""
            return None, f"未找到「{name_args[0]}」的绑定，请让对方先 /lol 绑定"
        return event.get_sender_id(), ""

    async def group_members(self, event: AstrMessageEvent) -> list[str]:
        """群成员 QQ 列表；失败时回退为[发送者]。"""
        try:
            group = await event.get_group()
            if group and getattr(group, "members", None):
                members = group.members
                if isinstance(members, dict):
                    return [str(k) for k in members.keys()]
                return [
                    str(getattr(m, "id", getattr(m, "user_id", m))) for m in members
                ]
        except Exception:  # noqa: BLE001
            pass
        sender = event.get_sender_id()
        return [sender] if sender else []

    # ----- 帮助与群开关 -----

    async def _cmd_help(self, _plugin, event: AstrMessageEvent, args: list[str]):
        """统一签名 (plugin, event, args)，与 commands/ 模块一致。"""
        yield event.plain_result(fmt_help())

    _FLAG_MAP = {
        "播报": "enabled",
        "上线播报": "online_notification",
        "开局播报": "game_start_notification",
        "结算播报": "game_end_notification",
    }

    async def _is_group_admin(self, event: AstrMessageEvent) -> bool:
        """AstrBot 管理员 或 QQ 群主/群管理。"""
        if event.is_admin():
            return True
        gid = event.get_group_id()
        if not gid:
            return False
        try:
            group = await event.get_group()
            if group:
                sid = event.get_sender_id()
                owner = str(getattr(group, "group_owner", "") or "")
                admins = {str(x) for x in (getattr(group, "group_admins", None) or [])}
                return sid == owner or sid in admins
        except Exception:  # noqa: BLE001
            pass
        return False

    async def _cmd_group_switch(self, plugin, event: AstrMessageEvent, args: list[str]):
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用")
            return
        sub = self._parse_args(event)[0]
        column = self._FLAG_MAP.get(sub, "enabled")
        if not await self._is_group_admin(event):
            yield event.plain_result("该指令需要群管理员权限")
            return
        if not args or args[0].lower() not in ("on", "off", "开", "关"):
            yield event.plain_result(f"用法：/lol {sub} on|off")
            return
        value = 1 if args[0].lower() in ("on", "开") else 0
        await plugin.db.upsert_group(group_id, event.unified_msg_origin)
        await plugin.db.set_group_flag(group_id, column, value)
        yield event.plain_result(f"✅ 已{'开启' if value else '关闭'}本群的「{sub}」")
