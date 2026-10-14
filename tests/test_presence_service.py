"""PresenceService 状态机行为测试（任务第二十七/二十八节场景）。"""

import pytest
import pytest_asyncio
from astrbot_plugin_lol_presence.models.presence import (
    PlayerPresence,
    PresenceEvent,
    PresenceState,
)
from astrbot_plugin_lol_presence.services.presence_service import PresenceService
from astrbot_plugin_lol_presence.storage.database import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(tmp_path)
    await database.init()
    yield database


@pytest.fixture
def events():
    collected = []

    async def collect(event, old, new):
        collected.append((event, old.state, new.state))

    return collected, collect


@pytest.mark.asyncio
async def test_startup_snapshot_is_silent(db, events):
    """启动时 A online B online C offline => 0 notifications。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()

    svc.subscribe(collect)
    snapshot = [
        PlayerPresence(puuid="A", state=PresenceState.ONLINE),
        PlayerPresence(puuid="B", state=PresenceState.ONLINE),
        PlayerPresence(puuid="C", state=PresenceState.OFFLINE),
    ]
    n = await svc.on_snapshot(snapshot)
    assert n == 0
    assert collected == []
    assert svc.initialized is True


@pytest.mark.asyncio
async def test_first_online_after_init_notifies_once(db, events):
    """初始化后 C offline -> online 只产生 1 个通知事件。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    await svc.on_snapshot(
        [
            PlayerPresence(puuid="C", state=PresenceState.OFFLINE),
        ]
    )
    n = await svc.on_snapshot([PlayerPresence(puuid="C", state=PresenceState.ONLINE)])
    assert n == 1
    assert collected == [
        (PresenceEvent.PLAYER_ONLINE, PresenceState.OFFLINE, PresenceState.ONLINE)
    ]


@pytest.mark.asyncio
async def test_duplicate_online_no_spam(db, events):
    """online -> online 不通知；断线抖动的重复上线由通知层冷却裁剪。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.OFFLINE)])
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])
    # 网络抖动 offline -> online 快速来回
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.OFFLINE)])
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])

    online_events = [e for e in collected if e[0] == PresenceEvent.PLAYER_ONLINE]
    assert len(online_events) == 2  # 首次上线 + 抖动恢复（冷却由通知层裁剪）
    # 相同状态 online -> online 不产生事件
    assert len(collected) == 3  # ONLINE, OFFLINE, ONLINE


@pytest.mark.asyncio
async def test_game_flow(db, events):
    """offline -> online -> inGame -> online 全链路。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    # 首批快照封口（静默）
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.OFFLINE)])
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])
    await svc.on_snapshot(
        [
            PlayerPresence(
                puuid="A",
                state=PresenceState.IN_GAME,
                champion_id=275,
                queue_id=420,
                game_id="12345",
            )
        ]
    )
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])

    kinds = [e[0] for e in collected]
    assert kinds == [
        PresenceEvent.PLAYER_ONLINE,
        PresenceEvent.GAME_STARTED,
        PresenceEvent.GAME_ENDED,
    ]


@pytest.mark.asyncio
async def test_reconnect_full_snapshot_no_resend(db, events):
    """断线重连：mark_reconnected 后全量快照若状态无变化 => 0 通知。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    await svc.on_snapshot(
        [PlayerPresence(puuid="A", state=PresenceState.OFFLINE)]
    )  # 封口
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])
    assert len([e for e in collected if e[0] == PresenceEvent.PLAYER_ONLINE]) == 1

    # 模拟断线重连
    svc.mark_reconnected()
    n = await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])
    assert n == 0

    # 重连封口后真实变化仍然通知
    n = await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.OFFLINE)])
    assert n == 1


@pytest.mark.asyncio
async def test_new_puuid_after_init_silent_first_seen(db, events):
    """初始化后第一次见到的新玩家不误报上线（如刚完成绑定）。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    await svc.on_snapshot([PlayerPresence(puuid="NEW", state=PresenceState.ONLINE)])
    assert collected == []


@pytest.mark.asyncio
async def test_offline_to_ingame_dual_event(db, events):
    """离线直接进游戏：上线 + 开局两个事件。"""
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.OFFLINE)])
    n = await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.IN_GAME)])
    assert n == 2
    kinds = [e[0] for e in collected]
    assert kinds == [PresenceEvent.PLAYER_ONLINE, PresenceEvent.GAME_STARTED]


@pytest.mark.asyncio
async def test_unobservable_state_never_fakes_offline_or_online(db, events):
    collected, collect = events
    svc = PresenceService(db)
    await svc.initialize()
    svc.subscribe(collect)

    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.UNKNOWN)])
    await svc.on_snapshot([PlayerPresence(puuid="A", state=PresenceState.ONLINE)])

    assert collected == []
    assert svc.get("A").state == PresenceState.ONLINE
