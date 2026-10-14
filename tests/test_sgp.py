"""SGP Provider 解析逻辑测试（不联网，只测纯函数与 token 缓存）。"""

import time

import pytest

from astrbot_plugin_lol_presence.models.match import MatchSummary
from astrbot_plugin_lol_presence.providers.sgp import SgpProvider, _jwt_exp


def test_jwt_exp_parses():
    import base64
    import json

    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": 1756500000}).encode())
        .decode()
        .rstrip("=")
    )
    token = f"a.{payload}.b"
    assert _jwt_exp(token) == 1756500000
    assert _jwt_exp("garbage") == 0
    assert _jwt_exp("") == 0


def test_summary_from_sgp_full():
    game = {
        "metadata": {"match_id": "hn1_123456", "timestamp": "2026-08-29T12:00:00.000Z"},
        "json": {
            "gameId": 123456,
            "queueId": 420,
            "gameMode": "CLASSIC",
            "gameDuration": 1934,
            "gameEndTimestamp": 1756468800000,
            "participants": [
                {
                    "puuid": "P1",
                    "teamId": 100,
                    "championId": 275,
                    "kills": 8,
                    "deaths": 6,
                    "assists": 12,
                    "win": True,
                },
                {
                    "puuid": "P2",
                    "teamId": 200,
                    "championId": 22,
                    "kills": 3,
                    "deaths": 8,
                    "assists": 4,
                    "win": False,
                },
            ],
        },
    }
    m: MatchSummary = SgpProvider._summary_from_sgp(game, "hn1")
    assert m.game_id == "123456"
    assert m.queue_id == 420
    assert m.game_duration_seconds == 1934
    assert m.duration_text == "32:14"
    p = m.participant_of("P1")
    assert p.kda == "8/6/12"
    assert p.win is True
    assert m.win_of("P2") is False


def test_summary_from_sgp_fallback_endtime_from_iso():
    game = {
        "metadata": {"match_id": "hn1_9", "timestamp": "2026-08-29T12:00:00Z"},
        "json": {"gameId": 9, "queueId": 450, "gameDuration": 60, "participants": []},
    }
    m = SgpProvider._summary_from_sgp(game, "hn1")
    assert m.game_end_time_ms > 0  # 从 ISO timestamp 推导


@pytest.mark.asyncio
async def test_token_cache(monkeypatch):
    """token 缓存：同一 token 未过期时不重复请求 LCU。"""
    calls = {"n": 0}

    class FakeLcu:
        async def get(self, path):
            calls["n"] += 1
            import base64
            import json

            payload = (
                base64.urlsafe_b64encode(
                    json.dumps({"exp": time.time() + 3600}).encode()
                )
                .decode()
                .rstrip("=")
            )
            return {"accessToken": f"aaa.{payload}.bbb"}

        async def get_text(self, path):
            calls["n"] += 1
            import base64
            import json

            payload = (
                base64.urlsafe_b64encode(
                    json.dumps({"exp": time.time() + 3600}).encode()
                )
                .decode()
                .rstrip("=")
            )
            return f"bbb.{payload}.ccc"

    p = SgpProvider(FakeLcu(), server_id="hn1")
    t1 = await p._get_token("entitlements")
    t2 = await p._get_token("entitlements")
    assert t1 == t2
    assert calls["n"] == 1
    s1 = await p._get_token("league-session")
    s2 = await p._get_token("league-session")
    assert s1 == s2
    assert calls["n"] == 2
    await p.close()
