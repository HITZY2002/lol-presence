"""状态机与 Presence 映射测试（任务第二十六节要求的最小集合）。"""

from astrbot_plugin_lol_presence.models.presence import (
    PresenceEvent,
    PresenceState,
    classify_transition,
    map_game_status,
)


def test_offline_to_online_once():
    """offline -> online 只产生一次 PLAYER_ONLINE。"""
    assert (
        classify_transition(PresenceState.OFFLINE, PresenceState.ONLINE)
        == PresenceEvent.PLAYER_ONLINE
    )


def test_online_to_online_no_notify():
    assert classify_transition(PresenceState.ONLINE, PresenceState.ONLINE) is None


def test_online_to_ingame_starts():
    assert (
        classify_transition(PresenceState.ONLINE, PresenceState.IN_GAME)
        == PresenceEvent.GAME_STARTED
    )
    assert (
        classify_transition(PresenceState.CHAMP_SELECT, PresenceState.IN_GAME)
        == PresenceEvent.GAME_STARTED
    )


def test_ingame_to_online_ends():
    assert (
        classify_transition(PresenceState.IN_GAME, PresenceState.ONLINE)
        == PresenceEvent.GAME_ENDED
    )


def test_offline_to_ingame_is_online_event():
    """上号时已在游戏中：按任务第八节产生 ONLINE 事件（服务层会补发开局）。"""
    assert (
        classify_transition(PresenceState.OFFLINE, PresenceState.IN_GAME)
        == PresenceEvent.PLAYER_ONLINE
    )


def test_to_offline_is_offline_event():
    assert (
        classify_transition(PresenceState.ONLINE, PresenceState.OFFLINE)
        == PresenceEvent.PLAYER_OFFLINE
    )
    assert (
        classify_transition(PresenceState.IN_GAME, PresenceState.OFFLINE)
        == PresenceEvent.PLAYER_OFFLINE
    )


def test_game_status_mapping():
    assert map_game_status("outOfGame") == PresenceState.ONLINE
    assert map_game_status("inGame") == PresenceState.IN_GAME
    assert map_game_status("champSelect") == PresenceState.CHAMP_SELECT
    assert map_game_status("inQueue") == PresenceState.MATCHMAKING
    assert map_game_status("lobby") == PresenceState.LOBBY
    # 未知值不猜测
    assert map_game_status("someFutureValue") == PresenceState.UNKNOWN
    assert map_game_status("") == PresenceState.UNKNOWN
    assert map_game_status(None) == PresenceState.UNKNOWN


def test_unknown_is_not_a_real_presence_transition():
    assert classify_transition(PresenceState.ONLINE, PresenceState.UNKNOWN) is None
    assert classify_transition(PresenceState.UNKNOWN, PresenceState.ONLINE) is None
    assert classify_transition(PresenceState.IN_GAME, PresenceState.UNKNOWN) is None
