"""指令实现：状态查询（在线/谁在玩/状态/车队）。"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..utils.champion import queue_name
from ..utils.formatter import (
    fmt_online_list,
    fmt_online_me,
    fmt_party_list,
)
from .bind import not_bound_hint


async def cmd_me(plugin, event: AstrMessageEvent, args: list[str]):
    b = await plugin.players.get_binding(event.get_sender_id())
    if not b:
        yield event.plain_result(not_bound_hint())
        return
    p = plugin.presence.get(b.puuid)
    yield event.plain_result(fmt_online_me(b, p, plugin.champions))


async def cmd_online(plugin, event: AstrMessageEvent, args: list[str]):
    if not event.get_group_id():
        yield event.plain_result("请在群聊中使用 /lol 在线")
        return
    bindings = await plugin.players.bindings_of_qq_ids(
        await plugin.group_members(event)
    )
    if not bindings:
        yield event.plain_result("本群还没有人绑定 LOL 账号，发送 /lol 绑定 游戏名#TAG")
        return
    states = plugin.presence.states_for([b.puuid for b in bindings])
    yield event.plain_result(fmt_online_list(bindings, states, plugin.champions))


async def cmd_whos_playing(plugin, event: AstrMessageEvent, args: list[str]):
    if not event.get_group_id():
        yield event.plain_result("请在群聊中使用 /lol 谁在玩")
        return
    bindings = await plugin.players.bindings_of_qq_ids(
        await plugin.group_members(event)
    )
    playing = plugin.presence.in_game_in([b.puuid for b in bindings])
    if not playing:
        yield event.plain_result("当前没有群友正在游戏中。")
        return
    by_puuid = {b.puuid: b for b in bindings}
    lines = ["【正在游戏中】", ""]
    for p in playing:
        b = by_puuid.get(p.puuid)
        name = (
            b.display_name
            if b and b.game_name
            else (
                f"{p.game_name}#{p.tag_line}"
                if p.tag_line
                else (p.game_name or p.puuid[:8])
            )
        )
        champ = plugin.champions.display(p.champion_id)
        desc = " · ".join(x for x in (champ, queue_name(p.queue_id)) if x)
        lines.append(f"🎮 {name}{('：' + desc) if desc else ''}")
    yield event.plain_result("\n".join(lines))


async def cmd_status(plugin, event: AstrMessageEvent, args: list[str]):
    qq_id, hint = await plugin.resolve_target(event, args)
    if not qq_id:
        yield event.plain_result(hint or not_bound_hint())
        return
    b = await plugin.players.get_binding(qq_id)
    if not b:
        yield event.plain_result("该用户尚未绑定 LOL 账号")
        return
    p = plugin.presence.get(b.puuid)
    yield event.plain_result(fmt_online_me(b, p, plugin.champions))


async def cmd_party(plugin, event: AstrMessageEvent, args: list[str]):
    if not event.get_group_id():
        yield event.plain_result("请在群聊中使用 /lol 车队")
        return
    bindings = await plugin.players.bindings_of_qq_ids(
        await plugin.group_members(event)
    )
    puuids = [b.puuid for b in bindings]
    yield event.plain_result(
        fmt_party_list(bindings, plugin.presence.states_for(puuids))
    )
