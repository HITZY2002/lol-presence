"""通知层测试：冷却、去重、结算退避。"""

import asyncio

import pytest
import pytest_asyncio
from astrbot_plugin_lol_presence.models.player import Binding
from astrbot_plugin_lol_presence.models.presence import (
    PlayerPresence,
    PresenceEvent,
    PresenceState,
)
from astrbot_plugin_lol_presence.providers.mock import (
    MockMatchProvider,
    make_finished_match,
)
from astrbot_plugin_lol_presence.services.match_service import MatchService
from astrbot_plugin_lol_presence.services.notification_service import (
    NotificationService,
)
from astrbot_plugin_lol_presence.services.player_service import PlayerService
from astrbot_plugin_lol_presence.storage.database import Database
from astrbot_plugin_lol_presence.utils.champion import ChampionMap

from astrbot.api.star import Context  # stub


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path)
    await database.init()
    yield database


@pytest.fixture
def context():
    return Context()


async def _setup_group(db, context, group_id="12345", cooldown=30):
    await db.upsert_group(group_id, f"qq:GroupMessage:{group_id}")
    await db.set_group_flag(group_id, "enabled", 1)
    return NotificationService(
        context,
        db,
        PlayerService(db),
        MatchService(db),
        ChampionMap(),
        online_cooldown_minutes=cooldown,
    )


def _presence(puuid, state, **kw):
    return PlayerPresence(puuid=puuid, state=state, **kw)


@pytest.mark.asyncio
async def test_online_then_cooldown_suppresses(db, context, tmp_path):
    """上线通知一次；冷却期内再次上线不重复。"""
    notifier = await _setup_group(db, context, cooldown=30)
    await db.upsert_binding(
        Binding(
            qq_id="u1", puuid="P1", game_name="刘某", tag_line="1234", platform="ionia"
        )
    )

    await notifier.on_presence_event(
        PresenceEvent.PLAYER_ONLINE,
        _presence("P1", PresenceState.OFFLINE),
        _presence("P1", PresenceState.ONLINE),
    )
    await notifier.on_presence_event(
        PresenceEvent.PLAYER_ONLINE,
        _presence("P1", PresenceState.ONLINE),
        _presence("P1", PresenceState.ONLINE),
    )
    # 抖动：offline -> online（冷却期内）
    await notifier.on_presence_event(
        PresenceEvent.PLAYER_ONLINE,
        _presence("P1", PresenceState.OFFLINE),
        _presence("P1", PresenceState.ONLINE),
    )
    sent = [c for c in context.sent if "上号" in c[1][0].text]
    assert len(sent) == 1
    assert "刘某" in sent[0][1][0].text
    assert "艾欧尼亚" in sent[0][1][0].text


@pytest.mark.asyncio
async def test_legacy_user_opt_out_flag_does_not_suppress_default_broadcast(
    db, context
):
    """The removed personal switch must not override admin group settings."""
    notifier = await _setup_group(db, context)
    b = Binding(qq_id="u1", puuid="P1", game_name="张某", tag_line="0001")
    await db.upsert_binding(b)
    b.broadcast_enabled = False
    await db.upsert_binding(b)

    await notifier.on_presence_event(
        PresenceEvent.PLAYER_ONLINE,
        _presence("P1", PresenceState.OFFLINE),
        _presence("P1", PresenceState.ONLINE),
    )
    assert len(context.sent) == 1
    assert "上号" in context.sent[0][1][0].text


@pytest.mark.asyncio
async def test_group_flags(db, context):
    """群关闭上线播报后不发送。"""
    notifier = await _setup_group(db, context)
    await db.upsert_binding(
        Binding(qq_id="u1", puuid="P1", game_name="王某", tag_line="0002")
    )
    await db.set_group_flag("12345", "online_notification", 0)

    await notifier.on_presence_event(
        PresenceEvent.PLAYER_ONLINE,
        _presence("P1", PresenceState.OFFLINE),
        _presence("P1", PresenceState.ONLINE),
    )
    assert context.sent == []


@pytest.mark.asyncio
async def test_game_started_dedup(db, context):
    """同一局 game_id 不重复播报开局。"""
    notifier = await _setup_group(db, context)
    await db.upsert_binding(
        Binding(qq_id="u1", puuid="P1", game_name="李某", tag_line="0003")
    )

    new = _presence(
        "P1", PresenceState.IN_GAME, game_id="G1", queue_id=420, champion_id=275
    )
    await notifier.on_presence_event(
        PresenceEvent.GAME_STARTED, _presence("P1", PresenceState.ONLINE), new
    )
    await notifier.on_presence_event(
        PresenceEvent.GAME_STARTED, _presence("P1", PresenceState.IN_GAME), new
    )
    texts = [c[1][0].text for c in context.sent]
    assert len(texts) == 1
    assert "开了" in texts[0]


@pytest.mark.asyncio
async def test_game_end_backoff_finds_match(db, context):
    """结算后战绩延迟出现：退避等待后播报 KDA。"""
    notifier = await _setup_group(db, context)
    await db.upsert_binding(
        Binding(qq_id="u1", puuid="P1", game_name="赵某", tag_line="0004")
    )

    mock_matches = MockMatchProvider()
    svc = MatchService(db)
    svc.register_provider(mock_matches)
    notifier.matches = svc

    old = _presence("P1", PresenceState.IN_GAME, game_id="G99", updated_at=1000.0)
    new = _presence("P1", PresenceState.ONLINE)

    async def add_late():
        await asyncio.sleep(0.05)
        mock_matches.add_match("P1", make_finished_match("G99", "P1"))

    task = asyncio.create_task(add_late())
    # 用短退避避免测试过慢
    from astrbot_plugin_lol_presence.services.match_service import GAME_END_BACKOFF

    real = list(GAME_END_BACKOFF)
    GAME_END_BACKOFF[:] = [1, 1, 1, 1]
    try:
        await notifier.on_presence_event(PresenceEvent.GAME_ENDED, old, new)
        # 等待后台任务完成
        for _ in range(100):
            if not notifier._pending_end_tasks:
                break
            await asyncio.sleep(0.05)
    finally:
        GAME_END_BACKOFF[:] = real
    await task

    texts = [c[1][0].text for c in context.sent]
    assert any("打完了" in t for t in texts)
    assert any("8/6/12" in t for t in texts)
    assert any("32:14" in t for t in texts)


@pytest.mark.asyncio
async def test_game_end_without_match_data(db, context):
    """战绩一直未出：只播报打完了，不编造 KDA。"""
    notifier = await _setup_group(db, context)
    await db.upsert_binding(
        Binding(qq_id="u1", puuid="P1", game_name="孙某", tag_line="0005")
    )

    notifier.matches = MatchService(db)  # 无 provider => available=False
    old = _presence("P1", PresenceState.IN_GAME, updated_at=1000.0)
    new = _presence("P1", PresenceState.ONLINE)

    from astrbot_plugin_lol_presence.services.match_service import GAME_END_BACKOFF

    real = list(GAME_END_BACKOFF)
    GAME_END_BACKOFF[:] = [1]
    try:
        await notifier.on_presence_event(PresenceEvent.GAME_ENDED, old, new)
        for _ in range(100):
            if not notifier._pending_end_tasks:
                break
            await asyncio.sleep(0.05)
    finally:
        GAME_END_BACKOFF[:] = real

    texts = [c[1][0].text for c in context.sent]
    assert len(texts) == 1
    assert "打完了" in texts[0]
    assert "8/" not in texts[0]
