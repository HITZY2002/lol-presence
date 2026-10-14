"""消息格式化。原则：没有可靠数据就少显示，绝不显示猜测值。"""

from __future__ import annotations

from datetime import datetime

from ..models.match import MatchSummary, RankedInfo
from ..models.player import Binding
from ..models.presence import PlayerPresence, PresenceState
from .champion import ChampionMap, queue_name, shows_champion

# 国服大区展示名
PLATFORM_NAMES = {
    "ionia": "艾欧尼亚",
    "betrayal": "峡谷之巅",
    "tft": "云顶之弈",
    "karma": "卡拉曼达",
    "hauling": "豪尔沃",
    "nawm": "纳威",
    "bravery": "布拉弗",
    "dragon": "巨龙之巢",
    "brotherhood": "守望之海",
    "nimbus": "霆虫",
    "wrath": "雷瑟守备",
    "talan": "塔兰",
    "asol": "铸星龙王",
    "sol": "巨神峰",
    "bandel": "班德尔城",
    "frostguard": "凛冬之怒",
    "nofear": "无畏先锋",
    "stoneplate": "石阵",
    "monolith": "巨石",
    "spire": "水晶之痕",
    "varus": "惩罪之箭",
    "shurima": "恕瑞玛",
    "zaun": "祖安",
    "bilgewater": "比尔吉沃特",
    "noxus": "诺克萨斯",
    "ornn": "山隐之焰",
    "kingsoul": "王者之魂",
    "dawn": "黎明",
    "dusk": "暮色",
}


def platform_name(platform: str | None) -> str:
    if not platform:
        return ""
    return PLATFORM_NAMES.get(platform.lower(), platform)


def short_puuid(puuid: str, keep: int = 8) -> str:
    return puuid[:keep] + "..." if puuid else ""


def _display(binding: Binding | None, presence: PlayerPresence | None) -> str:
    name = (binding.display_name if binding and binding.game_name else "") or (
        f"{presence.game_name}#{presence.tag_line}"
        if presence and presence.tag_line
        else (presence.game_name if presence else "")
    )
    puuid = binding.puuid if binding else (presence.puuid if presence else "")
    return name or short_puuid(puuid)


def _state_line(presence: PlayerPresence, champions: ChampionMap) -> str:
    if presence.state == PresenceState.IN_GAME:
        parts = []
        champ = (
            champions.display(presence.champion_id)
            if shows_champion(presence.queue_id)
            else None
        )
        if champ:
            parts.append(champ)
        q = queue_name(presence.queue_id)
        parts.append(q)
        return "🎮 " + " · ".join(parts) + " · 游戏中"
    if presence.state == PresenceState.CHAMP_SELECT:
        return "⏳ 英雄选择中"
    if presence.state == PresenceState.MATCHMAKING:
        return "🔍 匹配中"
    if presence.state == PresenceState.LOBBY:
        return "🏠 在房间"
    if presence.state == PresenceState.ONLINE:
        return "🟢 在线 · 可组队"
    return "⚫ 离线"


def fmt_online_list(
    bindings: list[Binding],
    presence_map: dict[str, PlayerPresence],
    champions: ChampionMap,
    max_show: int = 15,
) -> str:
    online_items: list[tuple[str, str]] = []
    not_friends: list[str] = []
    unavailable: list[str] = []
    offline = 0
    for b in bindings:
        p = presence_map.get(b.puuid)
        if not p or p.state == PresenceState.UNKNOWN:
            target = _display(b, p)
            if p and p.raw_game_status == "not_friend":
                not_friends.append(target)
            else:
                unavailable.append(target)
            continue
        if p.state == PresenceState.OFFLINE:
            offline += 1
            continue
        online_items.append((_display(b, p), _state_line(p, champions)))

    if not online_items and not not_friends and not unavailable:
        return "【LOL 群友状态】\n\n当前没有绑定的群友在线。"

    lines = ["【LOL 群友状态】", ""]
    if not online_items:
        lines.extend(["当前没有可确认在线的群友。", ""])
    for name, line in online_items[:max_show]:
        lines.append(f"{name}")
        lines.append(line)
        lines.append("")
    if len(online_items) > max_show:
        lines.append(f"… 以及 {len(online_items) - max_show} 人")
        lines.append("")
    if offline:
        lines.append(f"⚫ 其他 {offline} 人离线")
    if not_friends:
        if offline:
            lines.append("")
        lines.append(f"⚠️ 观察号未加好友 {len(not_friends)} 人（添加后自动接管监控）：")
        lines.extend(f"- {name}" for name in not_friends[:max_show])
        if len(not_friends) > max_show:
            lines.append(f"… 以及 {len(not_friends) - max_show} 人")
    if unavailable:
        if offline or not_friends:
            lines.append("")
        lines.append(f"⚠️ 状态暂不可用 {len(unavailable)} 人（客户端未返回状态）：")
        lines.extend(f"- {name}" for name in unavailable[:max_show])
        if len(unavailable) > max_show:
            lines.append(f"… 以及 {len(unavailable) - max_show} 人")
    return "\n".join(lines).strip()


def fmt_online_me(
    binding: Binding,
    presence: PlayerPresence | None,
    champions: ChampionMap,
) -> str:
    if not presence or presence.state == PresenceState.UNKNOWN:
        if presence and presence.raw_game_status == "not_friend":
            return (
                f"【{_display(binding, presence)}】\n"
                "⚠️ 观察号尚未添加该账号为好友\n"
                "添加好友后将自动接管监控，无需重新绑定"
            )
        return (
            f"【{_display(binding, presence)}】\n⚠️ 状态暂不可用：客户端当前未返回状态"
        )
    if presence.state == PresenceState.OFFLINE:
        return f"【{_display(binding, presence)}】\n⚫ 当前离线"
    region = platform_name(binding.platform or presence.platform)
    prefix = f"{region} · " if region else ""
    return (
        f"【{prefix}{_display(binding, presence)}】\n{_state_line(presence, champions)}"
    )


def fmt_party_list(
    bindings: list[Binding],
    presence_map: dict[str, PlayerPresence],
) -> str:
    party: list[str] = []
    for b in bindings:
        p = presence_map.get(b.puuid)
        if not p:
            continue
        if p.state in (PresenceState.ONLINE, PresenceState.LOBBY):
            party.append(_display(b, p))
    if not party:
        return "【现在能开黑】\n\n暂时没有在线未开局的群友。"
    lines = ["【现在能开黑】", ""]
    lines.extend(party)
    lines.append("")
    lines.append(f"共 {len(party)} 人在线未开。")
    return "\n".join(lines)


def fmt_game_start(
    binding: Binding,
    presence: PlayerPresence,
    champions: ChampionMap,
) -> str:
    name = _display(binding, presence)
    champ = (
        champions.display(presence.champion_id)
        if shows_champion(presence.queue_id)
        else None
    )
    q = queue_name(presence.queue_id)
    if champ:
        return f"🎮 {name} 开了\n\n{champ} · {q}"
    return f"🎮 {name} 开了一把{q}"


def _result_icon(win: bool | None) -> str:
    if win is True:
        return "🏆 胜利"
    if win is False:
        return "💀 失败"
    return ""


def fmt_game_end(
    binding: Binding,
    presence: PlayerPresence,
    match: MatchSummary | None,
    champions: ChampionMap,
) -> str:
    name = _display(binding, presence)
    if not match:
        # 战绩未及时出现，只播报打完，不编数据
        return f"🏁 {name} 打完了\n\n（战绩暂未出，稍后可用 /lol 战绩 @{binding.game_name} 查询）"
    champ = champions.display(match.champion_of(binding.puuid))
    lines = [f"🏁 {name} 打完了", ""]
    if champ:
        lines.append(champ)
    lines.append(match.kda_of(binding.puuid))
    win = match.win_of(binding.puuid)
    res = _result_icon(win)
    if res:
        lines.append(res)
    lines.append(match.duration_text)
    return "\n".join(lines)


def fmt_match_history(
    binding: Binding,
    matches: list[MatchSummary],
    champions: ChampionMap,
    count: int = 10,
) -> str:
    if not matches:
        return "没有查询到最近战绩。"
    region = platform_name(binding.platform)
    prefix = f"{region} · " if region else ""
    lines = [f"【{prefix}{_display(binding, None)} 最近战绩】", ""]
    for i, m in enumerate(matches[:count], 1):
        champ = champions.display(m.champion_of(binding.puuid)) or "未知英雄"
        win = m.win_of(binding.puuid)
        icon = "✔" if win else "✘"
        when = (
            datetime.fromtimestamp(m.game_end_time_ms / 1000).strftime("%m-%d")
            if m.game_end_time_ms
            else ""
        )
        lines.append(
            f"{i}. {icon} {champ} {m.kda_of(binding.puuid)} · {queue_name(m.queue_id)} · {m.duration_text} {when}"
        )
    return "\n".join(lines)


def fmt_match_stats(
    binding: Binding,
    matches: list[MatchSummary],
    limit: int = 100,
) -> str:
    """Format aggregate win rate and KDA from up to ``limit`` recent games."""
    participants = [
        participant
        for match in matches[:limit]
        if (participant := match.participant_of(binding.puuid)) is not None
    ]
    title = f"【{_display(binding, None)} 近{limit}场统计】"
    if not participants:
        return f"{title}\n\n暂无足够的对局数据。"

    games = len(participants)
    wins = sum(1 for participant in participants if participant.win)
    kills = sum(participant.kills for participant in participants)
    deaths = sum(participant.deaths for participant in participants)
    assists = sum(participant.assists for participant in participants)
    win_rate = wins / games * 100
    kda = (kills + assists) / deaths if deaths else None
    kda_text = f"{kda:.2f}" if kda is not None else "∞"
    return "\n".join(
        [
            title,
            "",
            f"有效对局：{games} 场",
            f"胜率：{win_rate:.1f}%（{wins}胜{games - wins}负）",
            f"场均 K/D/A：{kills / games:.2f} / {deaths / games:.2f} / {assists / games:.2f}",
            f"平均 KDA：{kda_text}",
        ]
    )


def fmt_today(
    binding: Binding,
    matches: list[MatchSummary],
) -> str:
    """今日战绩：按本地日历日过滤。"""
    today = datetime.now().strftime("%Y-%m-%d")
    today_matches = [
        m
        for m in matches
        if m.game_end_time_ms
        and datetime.fromtimestamp(m.game_end_time_ms / 1000).strftime("%Y-%m-%d")
        == today
    ]
    if not today_matches:
        return f"【{_display(binding, None)} 今日战绩】\n\n今天还没有完成的对局。"
    wins = sum(1 for m in today_matches if m.win_of(binding.puuid))
    total = len(today_matches)
    kills = sum(
        m.participant_of(binding.puuid).kills
        for m in today_matches
        if m.participant_of(binding.puuid)
    )
    deaths = sum(
        m.participant_of(binding.puuid).deaths
        for m in today_matches
        if m.participant_of(binding.puuid)
    )
    assists = sum(
        m.participant_of(binding.puuid).assists
        for m in today_matches
        if m.participant_of(binding.puuid)
    )
    lines = [
        f"【{_display(binding, None)} 今日战绩】",
        "",
        f"共 {total} 局 · {wins} 胜 {total - wins} 负",
        f"总 KDA {kills}/{deaths}/{assists}",
    ]
    return "\n".join(lines)


def fmt_ranked(binding: Binding, ranked_list: list[RankedInfo]) -> str:
    region = platform_name(binding.platform)
    prefix = f"{region} · " if region else ""
    if not ranked_list:
        return f"【{prefix}{_display(binding, None)} 段位】\n\n暂无排位数据。"
    lines = [f"【{prefix}{_display(binding, None)} 段位】", ""]
    for r in ranked_list:
        tag = "单双排" if "SOLO" in r.queue_type else "灵活排位"
        lines.append(f"{tag}：{r.display}（{r.wins}胜{r.losses}负）")
    return "\n".join(lines)


def fmt_help() -> str:
    return """【LOL 上线提醒】指令

/lol                  查询自己的状态
/lol 我的              查询自己的状态
/lol 在线              本群群友在线状态
/lol 谁在玩            正在游戏中的群友
/lol 车队              在线可开黑的群友
/lol 状态 @某人        查询某人状态
/lol 战绩 @某人        最近战绩及近100场统计
/lol 最近 10           最近N局
/lol 段位 @某人        排位段位
/lol 今日              今日战绩汇总
/lol 绑定 游戏名#TAG   绑定 LOL 账号
/lol 绑定 @群友 游戏名#TAG  管理员代绑定
/lol 自动绑定          QQ 自动解析绑定（若支持）
/lol 解绑              解除绑定
/lol 播报 on/off       群播报开关(管理员)
/lol 上线播报 on/off   (管理员)
/lol 开局播报 on/off   (管理员)
/lol 结算播报 on/off   (管理员)
/lol 帮助              本帮助"""
