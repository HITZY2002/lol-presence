"""集成测试：使用真实 AstrBot 包（非 stub）跑插件完整生命周期。

验证点：
- register 装饰器与真实框架兼容
- initialize() 在真实 Context + mock 配置下完成 Provider 组装
- /lol 指令路由与参数解析
- Presence 事件 -> 通知 -> context.send_message 全链路
- terminate() 干净退出

注意：与单元测试（astrbot stub）不能在同一 Python 进程内共存，
请用 run_tests.sh 分开运行。
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest


class FakeConfig(dict):
    def save_config(self):
        pass


def make_config(**overrides):
    cfg = FakeConfig(
        {
            "presence_provider": {
                "mode": "mock",
                "poll_interval": 5,
                "reconcile_interval": 60,
                "lcu": {},
                "wegame": {},
            },
            "match_provider": {"mode": "auto", "server_id": "hn1"},
            "notification": {
                "online_notification": True,
                "game_start_notification": True,
                "game_end_notification": True,
                "online_cooldown_minutes": 30,
            },
            "champion_cdn": "",
            "debug": False,
        }
    )
    cfg.update(overrides)
    return cfg


class FakeEvent:
    """最小 AstrMessageEvent 替身：携带消息链与发送者信息。"""

    def __init__(self, sender_qq="10001", group_id="12345", text="/lol", at_qq=None):
        from astrbot.api.message_components import At, Plain

        self.unified_msg_origin_value = f"qq:GroupMessage:{group_id}"
        self._sender = sender_qq
        self._group = group_id
        self._text = text
        self._at_qq = at_qq
        comps = [Plain(text)]
        if at_qq:
            comps.insert(0, At(qq=at_qq))
        self._comps = comps

    @property
    def message_str(self):
        # 真实 aiocqhttp 适配器会把 At 段渲染成 "@昵称(qq)" 追加进纯文本
        if self._at_qq:
            return f"{self._text} @user({self._at_qq})"
        return self._text

    @property
    def unified_msg_origin(self):
        return self.unified_msg_origin_value

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return self._group

    def is_admin(self):
        return True  # 测试视为管理员

    def get_messages(self):
        return self._comps

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        from astrbot.api.event import MessageEventResult

        result = MessageEventResult()
        result.chain = chain
        return result

    async def get_group(self):
        class G:
            group_owner = "10001"
            group_admins = []
            members = {self._sender: object(), "20002": object(), "30003": object()}

        return G()


async def drain(gen):
    out = []
    async for item in gen:
        out.append(item)
    return out


def _make_context_and_plugin():
    from astrbot.api.star import StarTools, Context
    from astrbot_plugin_lol_presence.main import LolPresencePlugin, PLUGIN_NAME

    tmp = tempfile.mkdtemp(prefix="lol_integration_")
    real_data_dir = Path(tmp) / PLUGIN_NAME
    real_data_dir.mkdir(parents=True, exist_ok=True)
    StarTools.get_data_dir = classmethod(lambda cls, name=None: real_data_dir)

    class SendingContext(Context):
        def __init__(self):
            self.sent = []

        async def send_message(self, session, message_chain):
            self.sent.append(
                (session, list(getattr(message_chain, "chain", message_chain)))
            )
            return True

    context = SendingContext()
    plugin = LolPresencePlugin(context, make_config())
    return context, plugin


@pytest.mark.integration
@pytest.mark.asyncio
async def test_full_lifecycle_with_real_astrbot():
    """首启静默 -> 管理开启播报 -> 绑定 -> 上线通知 -> 冷却 -> 查询指令。"""
    context, plugin = _make_context_and_plugin()
    await plugin.initialize()

    assert plugin.provider is not None and plugin.provider.name == "mock"
    assert plugin.presence.initialized is False  # 等待首批快照封口

    provider = plugin.provider

    # ---- 首启静默（任务第二十七节）----
    from astrbot_plugin_lol_presence.models.presence import PresenceState

    provider.set_state("PU-A", PresenceState.ONLINE, game_name="A", tag_line="1111")
    provider.set_state("PU-B", PresenceState.OFFLINE, game_name="B", tag_line="2222")
    await plugin._on_provider_snapshot(await provider.snapshot())
    assert plugin.presence.initialized is True
    assert context.sent == []  # 启动快照 0 通知

    # ---- 管理员开启播报（真实管理流程）----
    results = await drain(plugin.lol_command(FakeEvent(text="/lol 播报 on")))
    assert results and "开启" in results[0][1]

    # ---- 绑定 B ----
    from astrbot_plugin_lol_presence.models.player import Binding

    await plugin.db.upsert_binding(
        Binding(
            qq_id="20002",
            puuid="PU-B",
            game_name="B",
            tag_line="2222",
            platform="ionia",
        )
    )
    plugin.sync_monitored()
    await asyncio.sleep(0.05)

    # ---- B 上线：1 条上线通知 ----
    await plugin._on_provider_snapshot(
        [provider.set_state("PU-B", PresenceState.ONLINE)]
    )
    await asyncio.sleep(0.05)
    online_msgs = [s for s in context.sent if "上号了" in s[1][0].text]
    assert len(online_msgs) == 1
    assert "艾欧尼亚" in online_msgs[0][1][0].text
    assert online_msgs[0][0] == "qq:GroupMessage:12345"

    # ---- 冷却期内再上线：不重复 ----
    await plugin._on_provider_snapshot(
        [provider.set_state("PU-B", PresenceState.OFFLINE)]
    )
    await plugin._on_provider_snapshot(
        [provider.set_state("PU-B", PresenceState.ONLINE)]
    )
    await asyncio.sleep(0.05)
    assert len([s for s in context.sent if "上号了" in s[1][0].text]) == 1

    # ---- 指令：/lol 在线 ----
    results = await drain(plugin.lol_command(FakeEvent(text="/lol 在线")))
    assert results and "【LOL 群友状态】" in results[0][1]

    # ---- 指令：/lol 状态 @20002 ----
    results = await drain(
        plugin.lol_command(FakeEvent(text="/lol 状态", at_qq="20002"))
    )
    assert results and ("在线" in results[0][1] or "离线" in results[0][1])

    # ---- 指令：/lol 车队 ----
    results = await drain(plugin.lol_command(FakeEvent(text="/lol 车队")))
    assert results and "能开黑" in results[0][1]

    # ---- 指令：/lol 帮助 ----
    results = await drain(plugin.lol_command(FakeEvent(text="/lol 帮助")))
    assert results and "绑定" in results[0][1]

    await plugin.terminate()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hexdata_routing_with_real_message_components(tmp_path):
    """Return one text-and-image result without sending unsolicited broadcasts."""
    import time
    from unittest.mock import AsyncMock

    from astrbot.api.message_components import Image, Plain
    from astrbot_plugin_lol_presence.models.player import Binding
    from astrbot_plugin_lol_presence.services.hexdata_browser import HexdataCard

    context, plugin = _make_context_and_plugin()
    await plugin.initialize()
    try:
        await plugin.db.upsert_binding(
            Binding("20002", "PU-B", game_name="测试 玩家", tag_line="12345")
        )
        plugin.hexdata.get_card = AsyncMock(
            return_value=HexdataCard(
                "测试 玩家#12345", tmp_path / "card.png", time.time()
            )
        )
        for alias in ("查分", "海斗分", "战绩卡"):
            results = await drain(
                plugin.lol_command(FakeEvent(text=f"/lol {alias}", at_qq="20002"))
            )
            assert len(results) == 1
            assert len(results[0].chain) == 2
            assert isinstance(results[0].chain[0], Plain)
            assert isinstance(results[0].chain[1], Image)
            assert results[0].chain[1].file.startswith("file:///")
        assert plugin.hexdata.get_card.await_count == 3
        assert context.sent == []
    finally:
        await plugin.terminate()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_game_flow_end_to_end():
    """开局播报 -> 结算退避 -> KDA 播报全链路。"""
    context, plugin = _make_context_and_plugin()
    await plugin.initialize()
    provider = plugin.provider

    from astrbot_plugin_lol_presence.models.presence import PresenceState
    from astrbot_plugin_lol_presence.models.player import Binding

    provider.set_state("PU-A", PresenceState.ONLINE)
    await plugin._on_provider_snapshot(await provider.snapshot())  # 封口
    # 管理员开启播报 + 绑定
    results = await drain(plugin.lol_command(FakeEvent(text="/lol 播报 on")))
    assert results and "开启" in results[0][1]
    await plugin.db.upsert_binding(
        Binding(
            qq_id="30003",
            puuid="PU-A",
            game_name="甲",
            tag_line="3333",
            platform="ionia",
        )
    )
    plugin.sync_monitored()
    await asyncio.sleep(0.05)

    # 上线 -> 开局 -> 结算全链路
    await plugin._on_provider_snapshot(
        [
            provider.set_state(
                "PU-A",
                PresenceState.IN_GAME,
                champion_id=275,
                queue_id=420,
                game_id="G1",
            )
        ]
    )
    await asyncio.sleep(0.05)
    texts = [s[1][0].text for s in context.sent]
    # 封口时已在线 => ONLINE→IN_GAME 只产生开局事件（无上线事件）
    assert any("开了" in t for t in texts)
    assert any("单双排" in t for t in texts)

    # 注入战绩后结算（mock presence 无战绩源，手动注册 mock 战绩源）
    from astrbot_plugin_lol_presence.providers.mock import (
        MockMatchProvider,
        make_finished_match,
    )

    mock_matches = MockMatchProvider()
    plugin.matches.register_provider(mock_matches)
    mock_matches.add_match("PU-A", make_finished_match("G1", "PU-A"))
    from astrbot_plugin_lol_presence.services.match_service import GAME_END_BACKOFF

    real = list(GAME_END_BACKOFF)
    GAME_END_BACKOFF[:] = [1, 1, 1, 1]
    try:
        await plugin._on_provider_snapshot(
            [provider.set_state("PU-A", PresenceState.ONLINE)]
        )
        for _ in range(200):
            if not plugin.notifier._pending_end_tasks:
                break
            await asyncio.sleep(0.05)
    finally:
        GAME_END_BACKOFF[:] = real

    texts = [s[1][0].text for s in context.sent]
    assert any("打完了" in t for t in texts)
    assert any("8/6/12" in t for t in texts)

    await plugin.terminate()
