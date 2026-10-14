"""指令实现：战绩/段位/今日。"""

from __future__ import annotations

from astrbot.api.event import AstrMessageEvent

from ..services.match_service import MatchNotReadyError
from ..utils.formatter import (
    fmt_match_history,
    fmt_match_stats,
    fmt_ranked,
    fmt_today,
)
from .bind import not_bound_hint


async def _target_binding(plugin, event: AstrMessageEvent, args: list[str]):
    """返回 (binding, 错误提示)。"""
    qq_id, hint = await plugin.resolve_target(event, args)
    if not qq_id:
        return None, hint or not_bound_hint()
    b = await plugin.players.get_binding(qq_id)
    if not b:
        return None, "该用户尚未绑定 LOL 账号"
    return b, ""


async def cmd_history(plugin, event: AstrMessageEvent, args: list[str]):
    if not plugin.matches.available:
        yield event.plain_result("战绩数据源当前不可用（需本地 LCU 观察号或 SGP）。")
        return
    b, err = await _target_binding(plugin, event, args)
    if not b:
        yield event.plain_result(err)
        return
    count = 5
    for a in args:
        if a.isdigit():
            count = max(1, min(20, int(a)))
            break
    try:
        # Fetch once for both the requested recent list and the 100-game stats.
        matches = await plugin.matches.get_history(b.puuid, count=100)
    except MatchNotReadyError as e:
        yield event.plain_result(f"战绩查询失败：{e}")
        return
    recent = fmt_match_history(b, matches, plugin.champions, count)
    stats = fmt_match_stats(b, matches, limit=100)
    yield event.plain_result(f"{recent}\n\n{stats}")


async def cmd_ranked(plugin, event: AstrMessageEvent, args: list[str]):
    if not plugin.matches.available:
        yield event.plain_result("段位数据源当前不可用。")
        return
    b, err = await _target_binding(plugin, event, args)
    if not b:
        yield event.plain_result(err)
        return
    try:
        ranked = await plugin.matches.get_ranked(b.puuid)
    except MatchNotReadyError as e:
        yield event.plain_result(f"段位查询失败：{e}")
        return
    yield event.plain_result(fmt_ranked(b, ranked))


async def cmd_today(plugin, event: AstrMessageEvent, args: list[str]):
    if not plugin.matches.available:
        yield event.plain_result("战绩数据源当前不可用。")
        return
    b, err = await _target_binding(plugin, event, args)
    if not b:
        yield event.plain_result(err)
        return
    matches = await plugin.matches.get_today_matches(b.puuid)
    yield event.plain_result(fmt_today(b, matches))
