"""LCU 好友 presence 映射与消息格式化测试。"""

import json

import pytest
from astrbot_plugin_lol_presence.models.match import MatchParticipant, MatchSummary
from astrbot_plugin_lol_presence.models.player import Binding
from astrbot_plugin_lol_presence.models.presence import (
    PlayerPresence,
    PresenceState,
)
from astrbot_plugin_lol_presence.providers.lcu import (
    LcuCredentials,
    LcuPresenceProvider,
    _presence_from_friend,
)
from astrbot_plugin_lol_presence.utils.champion import ChampionMap, queue_name
from astrbot_plugin_lol_presence.utils.formatter import (
    fmt_game_end,
    fmt_game_start,
    fmt_match_stats,
    fmt_online_list,
    fmt_party_list,
)


def _friend(**kw):
    base = {
        "puuid": "PUUID1",
        "gameName": "刘某",
        "gameTag": "1234",
        "availability": "chat",
        "productName": "league_of_legends",
        "lol": {"gameStatus": "outOfGame"},
    }
    base.update(kw)
    return base


def test_friend_online():
    p = _presence_from_friend(_friend())
    assert p.state == PresenceState.ONLINE
    assert p.puuid == "PUUID1"
    assert p.game_name == "刘某"


def test_friend_chat_jid_falls_back_to_puuid():
    puuid = "87f222fe-b9ac-5b9b-9a50-020c4fa27550"
    p = _presence_from_friend(_friend(puuid="", id=f"{puuid}@pvp.net"))
    assert p.puuid == puuid


def test_friend_offline_beats_stale_fields():
    """availability=offline 时即使残留 inGame 字段也是 OFFLINE。"""
    p = _presence_from_friend(
        _friend(
            availability="offline",
            lol={"gameStatus": "inGame", "championId": 275},
        )
    )
    assert p.state == PresenceState.OFFLINE


def test_friend_ingame_fields():
    p = _presence_from_friend(
        _friend(
            lol={
                "gameStatus": "inGame",
                "championId": 275,
                "queueId": 420,
                "gameId": 991,
            }
        )
    )
    assert p.state == PresenceState.IN_GAME
    assert p.champion_id == 275
    assert p.queue_id == 420
    assert p.game_id == "991"


def test_friend_unknown_status_is_online_not_unknown():
    """在线但出现未知 gameStatus：保守按 ONLINE，不进 UNKNOWN。"""
    p = _presence_from_friend(_friend(lol={"gameStatus": "someNewStatus"}))
    assert p.state == PresenceState.ONLINE


def test_friend_missing_all_presence_fields_is_unknown():
    p = _presence_from_friend(_friend(availability="", productName="", lol={}))
    assert p.state == PresenceState.UNKNOWN


def test_other_riot_product_is_not_league_online():
    p = _presence_from_friend(
        _friend(
            availability="chat",
            productName="valorant",
            lol={},
        )
    )
    assert p.state == PresenceState.OFFLINE


def test_champ_select():
    p = _presence_from_friend(_friend(lol={"gameStatus": "champSelect"}))
    assert p.state == PresenceState.CHAMP_SELECT


def test_fmt_online_list_mixed():
    champs = ChampionMap.__new__(ChampionMap)  # 跳过网络加载
    champs._id_to_name = {275: "疾风剑豪·亚索"}
    champs._id_to_alias = {}
    b1 = Binding(
        qq_id="1", puuid="P1", game_name="刘某", tag_line="1234", platform="ionia"
    )
    b2 = Binding(qq_id="2", puuid="P2", game_name="张某", tag_line="5678")
    b3 = Binding(qq_id="3", puuid="P3", game_name="李某", tag_line="0000")
    presence = {
        "P1": PlayerPresence(puuid="P1", state=PresenceState.ONLINE),
        "P2": PlayerPresence(
            puuid="P2", state=PresenceState.IN_GAME, champion_id=275, queue_id=420
        ),
        "P3": PlayerPresence(puuid="P3", state=PresenceState.OFFLINE),
    }
    text = fmt_online_list([b1, b2, b3], presence, champs)
    assert "🟢" in text and "刘某" in text
    assert "🎮" in text and "游戏中" in text
    assert "其他 1 人离线" in text


def test_fmt_online_list_keeps_unobservable_binding_visible():
    champs = ChampionMap.__new__(ChampionMap)
    champs._id_to_name = {}
    champs._id_to_alias = {}
    binding = Binding(
        qq_id="1",
        puuid="P1",
        game_name="西电杰克爱",
        tag_line="58557",
    )

    presence = {
        "P1": PlayerPresence(
            puuid="P1",
            state=PresenceState.UNKNOWN,
            raw_game_status="not_friend",
        )
    }
    text = fmt_online_list([binding], presence, champs)

    assert "观察号未加好友 1 人" in text
    assert "添加后自动接管监控" in text
    assert "西电杰克爱#58557" in text
    assert "离线" not in text


@pytest.mark.asyncio
async def test_resolve_riot_id_uses_input_when_alias_only_returns_puuid():
    class FakeClient:
        async def get(self, path):
            if path.startswith("/lol-summoner/v1/alias/lookup"):
                return {"puuid": "P1"}
            if path == LcuPresenceProvider.FRIENDS_PATH:
                return []
            raise AssertionError(path)

    provider = LcuPresenceProvider.__new__(LcuPresenceProvider)
    provider.client = FakeClient()

    presence = await provider.resolve_player(" 西电杰克爱 # 58557 ")

    assert presence.puuid == "P1"
    assert presence.game_name == "西电杰克爱"
    assert presence.tag_line == "58557"
    assert presence.state == PresenceState.UNKNOWN


@pytest.mark.asyncio
async def test_snapshot_keeps_private_unnamed_friend_and_marks_missing_unknown():
    class FakeClient:
        async def get(self, path):
            assert path == LcuPresenceProvider.FRIENDS_PATH
            return [
                {
                    "puuid": "P1",
                    "relationshipOnRiot": "friend",
                    "availability": "offline",
                    "gameName": "",
                    "product": "",
                    "lol": {},
                }
            ]

    provider = LcuPresenceProvider(LcuCredentials("127.0.0.1", 1, "x"))
    provider.client = FakeClient()
    provider.set_monitored({"P1", "P2"})

    states = {p.puuid: p for p in await provider.snapshot()}

    assert states["P1"].state == PresenceState.OFFLINE
    assert states["P2"].state == PresenceState.UNKNOWN
    assert states["P2"].raw_game_status == "not_friend"


@pytest.mark.asyncio
async def test_observation_status_includes_watch_account():
    class FakeClient:
        async def get(self, path):
            if path == "/lol-chat/v1/me":
                return {"gameName": "观察号", "gameTag": "10001"}
            if path == LcuPresenceProvider.FRIENDS_PATH:
                return [
                    {
                        "puuid": "P1",
                        "relationshipOnRiot": "friend",
                        "availability": "offline",
                    }
                ]
            raise AssertionError(path)

    provider = LcuPresenceProvider(LcuCredentials("127.0.0.1", 1, "x"))
    provider.client = FakeClient()

    status = await provider.observation_status("P1")

    assert status.observable is True
    assert status.observer_display_name == "观察号#10001"


@pytest.mark.asyncio
async def test_ws_friend_delete_becomes_unknown_without_fake_offline():
    puuid = "87f222fe-b9ac-5b9b-9a50-020c4fa27550"
    provider = LcuPresenceProvider(LcuCredentials("127.0.0.1", 1, "x"))
    provider.set_monitored({puuid})
    emitted = []

    async def collect(updates):
        emitted.extend(updates)

    provider.add_callback(collect)
    payload = json.dumps(
        [
            8,
            provider.WS_EVENT,
            {
                "data": {},
                "eventType": "Delete",
                "uri": f"/lol-chat/v1/friends/{puuid}@pvp.net",
            },
        ]
    )

    await provider._on_ws_event(payload)

    assert len(emitted) == 1
    assert emitted[0].puuid == puuid
    assert emitted[0].state == PresenceState.UNKNOWN
    assert emitted[0].raw_game_status == "not_friend"


@pytest.mark.asyncio
async def test_ws_private_friend_create_accepts_chat_jid():
    puuid = "87f222fe-b9ac-5b9b-9a50-020c4fa27550"
    provider = LcuPresenceProvider(LcuCredentials("127.0.0.1", 1, "x"))
    provider.set_monitored({puuid})
    emitted = []

    async def collect(updates):
        emitted.extend(updates)

    provider.add_callback(collect)
    payload = json.dumps(
        [
            8,
            provider.WS_EVENT,
            {
                "data": {
                    "id": f"{puuid}@pvp.net",
                    "relationshipOnRiot": "friend",
                    "availability": "offline",
                },
                "eventType": "Create",
            },
        ]
    )

    await provider._on_ws_event(payload)

    assert len(emitted) == 1
    assert emitted[0].puuid == puuid
    assert emitted[0].state == PresenceState.OFFLINE


def test_fmt_party_excludes_ingame_and_offline():
    b1 = Binding(qq_id="1", puuid="P1", game_name="刘某", tag_line="1234")
    b2 = Binding(qq_id="2", puuid="P2", game_name="张某", tag_line="5678")
    b3 = Binding(qq_id="3", puuid="P3", game_name="李某", tag_line="0000")
    presence = {
        "P1": PlayerPresence(puuid="P1", state=PresenceState.LOBBY),
        "P2": PlayerPresence(puuid="P2", state=PresenceState.IN_GAME),
        "P3": PlayerPresence(puuid="P3", state=PresenceState.OFFLINE),
    }
    text = fmt_party_list([b1, b2, b3], presence)
    assert "刘某" in text
    assert "张某" not in text and "李某" not in text
    assert "共 1 人在线未开" in text


def test_fmt_game_start_without_champion_data():
    """无英雄表时不显示猜测的英雄名。"""
    champs = ChampionMap.__new__(ChampionMap)
    champs._id_to_name = {}
    champs._id_to_alias = {}
    b = Binding(qq_id="1", puuid="P1", game_name="王某", tag_line="0009")
    p = PlayerPresence(
        puuid="P1", state=PresenceState.IN_GAME, champion_id=275, queue_id=420
    )
    text = fmt_game_start(b, p, champs)
    assert "单双排" in text
    assert "亚索" not in text  # 英雄表未加载 => 不显示


def test_fmt_game_end_with_match():
    champs = ChampionMap.__new__(ChampionMap)
    champs._id_to_name = {275: "疾风剑豪·亚索"}
    champs._id_to_alias = {}
    from astrbot_plugin_lol_presence.providers.mock import make_finished_match

    b = Binding(qq_id="1", puuid="P1", game_name="赵某", tag_line="0004")
    match = make_finished_match("G1", "P1")
    text = fmt_game_end(b, PlayerPresence(puuid="P1"), match, champs)
    assert "亚索" in text
    assert "8/6/12" in text
    assert "胜利" in text
    assert "32:14" in text


def test_fmt_match_stats_win_rate_and_average_kda():
    binding = Binding(qq_id="1", puuid="P1", game_name="玩家", tag_line="10001")
    matches = [
        MatchSummary(
            game_id="G1",
            participants=[
                MatchParticipant(puuid="P1", kills=10, deaths=5, assists=5, win=True)
            ],
        ),
        MatchSummary(
            game_id="G2",
            participants=[
                MatchParticipant(puuid="P1", kills=0, deaths=5, assists=5, win=False)
            ],
        ),
    ]

    text = fmt_match_stats(binding, matches)

    assert "有效对局：2 场" in text
    assert "胜率：50.0%（1胜1负）" in text
    assert "场均 K/D/A：5.00 / 5.00 / 5.00" in text
    assert "平均 KDA：2.00" in text


def test_queue_names():
    assert queue_name(420) == "单双排"
    assert queue_name(450) == "极地大乱斗"
    assert queue_name(None) == "匹配"
