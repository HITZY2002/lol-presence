"""Card command and browser safety regression tests; no network calls."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_lol_presence.commands.hexdata import cmd_score
from astrbot_plugin_lol_presence.models.player import Binding
from astrbot_plugin_lol_presence.services.hexdata_browser import (
    HexdataBrowser,
    HexdataCard,
    HexdataError,
)


class FakeEvent:
    def __init__(self, admin=False):
        self.admin = admin

    def get_sender_id(self):
        return "10001"

    def is_admin(self):
        return self.admin

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain


def make_plugin(tmp_path, *, at=None, bound=True):
    binding = Binding("10001", "PU-A", game_name="测试 玩家", tag_line="12345")
    service = SimpleNamespace(
        get_card=AsyncMock(
            return_value=HexdataCard(
                binding.display_name, tmp_path / "card.png", time.time()
            )
        ),
        open_login=AsyncMock(),
    )
    players = SimpleNamespace(
        get_binding=AsyncMock(return_value=binding if bound else None),
        find_by_display_name=AsyncMock(return_value=binding if bound else None),
    )
    return SimpleNamespace(hexdata=service, players=players, _at_qq=lambda _: at)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args,at",
    [
        ([], None),
        (["@群友(20002)"], "20002"),
        (["测试", "玩家#12345"], None),
        (["测试 玩家"], None),
    ],
)
async def test_command_resolves_self_mention_and_direct_id(tmp_path, args, at):
    plugin = make_plugin(tmp_path, at=at)
    results = [result async for result in cmd_score(plugin, FakeEvent(), args)]
    plugin.hexdata.get_card.assert_awaited_once_with("测试 玩家#12345")
    assert len(results) == 1 and len(results[0]) == 2
    assert "数据来源：Hexdata" in results[0][0].text
    assert "https://hexdata.com.cn/matchmaking-position" in results[0][0].text
    assert "非官方隐藏分" in results[0][0].text
    if at:
        plugin.players.get_binding.assert_awaited_once_with(at)


@pytest.mark.asyncio
async def test_direct_query_needs_neither_binding_nor_lcu(tmp_path):
    plugin = make_plugin(tmp_path, bound=False)
    results = [
        result async for result in cmd_score(plugin, FakeEvent(), ["未绑定＃56789"])
    ]
    plugin.hexdata.get_card.assert_awaited_once_with("未绑定#56789")
    plugin.players.get_binding.assert_not_awaited()
    assert len(results) == 1


@pytest.mark.asyncio
async def test_missing_binding_does_not_query(tmp_path):
    plugin = make_plugin(tmp_path, bound=False)
    results = [result async for result in cmd_score(plugin, FakeEvent(), [])]
    assert "尚未绑定" in results[0]
    plugin.hexdata.get_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_binding_does_not_send_puuid(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.players.get_binding.return_value = Binding("10001", "not-a-game-name")
    results = [result async for result in cmd_score(plugin, FakeEvent(), [])]
    assert "缺少完整游戏 ID" in results[0]
    plugin.hexdata.get_card.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_login_is_bot_admin_only(tmp_path, admin):
    plugin = make_plugin(tmp_path)
    results = [result async for result in cmd_score(plugin, FakeEvent(admin), ["登录"])]
    assert len(results) == 1
    assert plugin.hexdata.open_login.await_count == int(admin)
    plugin.hexdata.get_card.assert_not_awaited()


@pytest.mark.asyncio
async def test_errors_are_one_reply_not_a_zero_score_or_image(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.hexdata.get_card.side_effect = HexdataError("登录已过期")
    results = [result async for result in cmd_score(plugin, FakeEvent(), [])]
    assert len(results) == 1 and "登录已过期" in results[0]
    assert "0 分" not in results[0]


@pytest.mark.asyncio
async def test_cached_message_is_labelled(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.hexdata.get_card.return_value = HexdataCard(
        "测试#12345", tmp_path / "card.png", time.time(), True
    )
    results = [result async for result in cmd_score(plugin, FakeEvent(), [])]
    assert "缓存，非重新查询" in results[0][0].text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["", "玩家", "#12345", "玩家#", "玩家#123#456", "玩家#12\n345", "x" * 161 + "#1"],
)
async def test_invalid_id_never_starts_browser(tmp_path, value):
    service = HexdataBrowser(tmp_path, {"enabled": True})
    service._start_browser = AsyncMock()
    with pytest.raises(HexdataError, match="完整游戏 ID"):
        await service.get_card(value)
    service._start_browser.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_feature_never_launches(tmp_path):
    service = HexdataBrowser(tmp_path, {})
    with pytest.raises(HexdataError, match="尚未启用"):
        await service.get_card("测试#12345")
    assert service._runtime is None


@pytest.mark.asyncio
async def test_headless_cannot_open_login(tmp_path):
    service = HexdataBrowser(tmp_path, {"enabled": True, "headless": True})
    with pytest.raises(HexdataError, match="关闭 headless"):
        await service.open_login()


@pytest.mark.asyncio
async def test_cache_cooldown_and_missing_cache_file(tmp_path):
    service = HexdataBrowser(tmp_path, {"enabled": True})
    path = tmp_path / "card.png"
    path.write_bytes(b"test fixture")
    service._start_browser = AsyncMock()
    service._generate_card = AsyncMock(
        return_value=HexdataCard("测试#12345", path, time.time())
    )
    await service.get_card("测试＃12345")
    assert (await service.get_card("测试#12345")).cached
    assert service._generate_card.await_count == 1
    with pytest.raises(HexdataError, match="冷却"):
        await service.get_card("另一个#12345")
    path.unlink()
    with pytest.raises(HexdataError, match="冷却"):
        await service.get_card("测试#12345")


@pytest.mark.asyncio
async def test_concurrent_query_is_rejected_and_close_cancels_active(tmp_path):
    service = HexdataBrowser(tmp_path, {"enabled": True})
    entered = asyncio.Event()

    async def blocked(_):
        entered.set()
        await asyncio.Event().wait()

    service._start_browser = AsyncMock()
    service._generate_card = blocked
    task = asyncio.create_task(service.get_card("测试#12345"))
    await entered.wait()
    with pytest.raises(HexdataError, match="其他战绩卡"):
        await service.get_card("另一个#12345")
    await service.close()
    assert task.cancelled()
    assert not service._lock.locked()
    with pytest.raises(HexdataError, match="重载"):
        await service.get_card("另一个#12345")


@pytest.mark.asyncio
async def test_failed_lookup_does_not_cache_or_leak_browser_error(tmp_path, caplog):
    service = HexdataBrowser(tmp_path, {"enabled": True})
    service._start_browser = AsyncMock()
    service._generate_card = AsyncMock(
        side_effect=RuntimeError("private-session-secret")
    )
    with pytest.raises(HexdataError) as error:
        await service.get_card("测试#12345")
    assert not service._cache
    assert "private-session-secret" not in str(error.value)
    assert "private-session-secret" not in caplog.text
