"""指令实现：绑定相关。

命令函数签名统一为
    async def handler(plugin, event, args) -> AsyncIterator[MessageEventResult]
由 main.py 的 /lol 路由调用。这样可以保持 Star 类轻量，
同时不破坏 AstrBot 对 filter 装饰器的扫描。
"""

from __future__ import annotations

import re

from astrbot.api.event import AstrMessageEvent

from ..services.player_service import BindError


def not_bound_hint() -> str:
    return "还没有绑定 LOL 账号。\n使用：/lol 绑定 游戏名#TAG\n或尝试：/lol 自动绑定"


async def cmd_bind(plugin, event: AstrMessageEvent, args: list[str]):
    if not args:
        yield event.plain_result(
            "用法：/lol 绑定 游戏名#TAG\n管理员代绑定：/lol 绑定 @群友 游戏名#TAG"
        )
        return

    target_qq = event.get_sender_id()
    query = " ".join(args)
    at_qq = plugin._at_qq(event)
    delegated = bool(at_qq and str(at_qq) != str(target_qq))
    if delegated:
        if not event.get_group_id():
            yield event.plain_result("请在群聊中使用代绑定")
            return
        if not await plugin._is_group_admin(event):
            yield event.plain_result("只有群管理员可以为其他群友绑定")
            return
        target_qq = str(at_qq)
        escaped_qq = re.escape(target_qq)
        query = re.sub(rf"\[CQ:at,qq={escaped_qq}(?:,[^\]]*)?\]", " ", query)
        query = re.sub(rf"@.*?\(\s*{escaped_qq}\s*\)", " ", query)
        query = " ".join(query.split())
        if not query:
            yield event.plain_result("用法：/lol 绑定 @群友 游戏名#TAG")
            return

    try:
        b = await plugin.players.bind(target_qq, query)
    except BindError as e:
        yield event.plain_result(str(e))
        return
    except Exception:  # noqa: BLE001
        plugin.logger.exception("[LOL] bind failed")
        yield event.plain_result("绑定失败，数据源暂时不可用，请稍后再试")
        return
    await plugin.refresh_monitored(reconcile=True)
    prefix = "✅ 已为被 @ 的群友绑定" if delegated else "✅ 绑定成功"
    status = None
    if plugin.provider:
        try:
            status = await plugin.provider.observation_status(b.puuid)
        except Exception:  # noqa: BLE001
            plugin.logger.debug("[LOL] observation check failed after bind")
    yield event.plain_result(_bind_result(prefix, b.display_name, status))


async def cmd_autobind(plugin, event: AstrMessageEvent, args: list[str]):
    try:
        b = await plugin.players.auto_bind(event.get_sender_id())
    except BindError as e:
        yield event.plain_result(str(e))
        return
    await plugin.refresh_monitored(reconcile=True)
    yield event.plain_result(_bind_result("✅ 自动绑定成功", b.display_name, None))


async def cmd_unbind(plugin, event: AstrMessageEvent, args: list[str]):
    n = await plugin.players.unbind(event.get_sender_id())
    if n:
        await plugin.refresh_monitored()
        yield event.plain_result("✅ 已解除绑定")
    else:
        yield event.plain_result("你还没有绑定 LOL 账号")


def _bind_result(prefix: str, display_name: str, status) -> str:
    lines = [f"{prefix}：{display_name}"]
    observer = status.observer_display_name if status else ""
    if status and status.observable is True:
        suffix = f"（观察号：{observer}）" if observer else ""
        lines.append(f"🟢 已检测到好友关系，监控已生效{suffix}")
    elif status and status.observable is False:
        target = f"观察号「{observer}」" if observer else "观察号"
        lines.append(f"⚠️ 绑定已保存，但{target}的好友列表中尚未检测到该账号。")
        lines.append("互加 LOL 好友后通常会立即生效，最迟在 5 分钟对账时生效。")
    else:
        lines.append("好友关系确认后，上线/开局/结算将自动播报。")
    return "\n".join(lines)
