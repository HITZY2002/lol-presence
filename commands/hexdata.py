"""On-demand Hexdata card queries; no background polling or score injection."""

from __future__ import annotations

from datetime import datetime

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Plain

from ..services.hexdata_browser import HEXDATA_URL, HexdataError


async def cmd_score(plugin, event: AstrMessageEvent, args: list[str]):
    """Query self, an explicitly mentioned friend, or a complete Riot ID.

    Args:
        plugin: The running LOL plugin instance.
        event: Event to reply to, including any mention components.
        args: Arguments following the score subcommand.

    Yields:
        One card message, or one actionable error message.
    """
    service = plugin.hexdata
    if service is None:
        yield event.plain_result("查分服务尚未初始化，请稍后再试。")
        return
    if args == ["登录"]:
        if not event.is_admin():
            yield event.plain_result("只有机器人管理员可以打开本机查分登录窗口。")
            return
        try:
            await service.open_login()
        except HexdataError as exc:
            yield event.plain_result(str(exc))
            return
        yield event.plain_result(
            "已打开本机 Hexdata 专用浏览器，请手动登录，完成后再 /lol 查分。不要在群里发送密码或验证码。"
        )
        return

    at_qq = plugin._at_qq(event)
    query = " ".join(args).strip().replace("＃", "#")
    if at_qq:
        binding = await plugin.players.get_binding(at_qq)
    elif "#" in query:
        binding = None
    elif query:
        binding = await plugin.players.find_by_display_name(query)
    else:
        binding = await plugin.players.get_binding(event.get_sender_id())
    if at_qq or "#" not in query:
        if not binding:
            yield event.plain_result(
                "该用户尚未绑定 LOL 账号。可先 /lol 绑定 游戏名#标签，或直接 /lol 查分 游戏名#标签。"
            )
            return
        if not binding.game_name or not binding.tag_line:
            yield event.plain_result(
                "绑定记录缺少完整游戏 ID，请重新绑定，或直接 /lol 查分 游戏名#标签。"
            )
            return
        query = binding.display_name
    try:
        card = await service.get_card(query)
    except HexdataError as exc:
        yield event.plain_result(f"海斗查分：{exc}\n{HEXDATA_URL}")
        return
    stamp = datetime.fromtimestamp(card.created_at).strftime("%m-%d %H:%M")
    cache_note = "（缓存，非重新查询）" if card.cached else ""
    yield event.chain_result(
        [
            Plain(
                f"【{card.game_id} 海斗战绩卡】\n生成时间：{stamp}{cache_note}\n数据来源：Hexdata（hexdata.com.cn），估算非官方隐藏分。\n{HEXDATA_URL}"
            ),
            Image.fromFileSystem(str(card.path)),
        ]
    )
