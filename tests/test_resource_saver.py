"""ResourceSaver 决策逻辑测试（注入假 client，不碰真实客户端）。"""

import asyncio

import pytest

from astrbot_plugin_lol_presence.services.resource_saver import ResourceSaver


class FakeCreds:
    def __init__(self, port, password):
        self.port = port
        self.password = password


class FakeClient:
    def __init__(self, phase="None", port=58528):
        self.creds = FakeCreds(port, "tok")
        self.phase = phase
        self.calls = []

    async def get(self, path):
        assert path == "/lol-gameflow/v1/gameflow-phase"
        return self.phase

    async def request(self, method, path):
        self.calls.append((method, path))
        return None


def _fast_saver(client, **kw):
    loops = kw.pop("loops", 1)
    ticks = {"n": 0}

    async def sleep(_t):
        # 真实短等待，让"稍后改变状态"的测试场景有时间生效
        await asyncio.sleep(0.05)
        ticks["n"] += 1
        if ticks["n"] > loops:
            raise asyncio.CancelledError

    saver = ResourceSaver(
        delay_seconds=0.05,
        check_interval=0.05,
        set_priority=False,
        get_client=lambda: client,
        sleep=sleep,
        **kw,
    )
    return saver, ticks


@pytest.mark.asyncio
async def test_kills_ux_when_safe():
    client = FakeClient(phase="None")
    saver, _ = _fast_saver(client)
    try:
        await saver.run_loop()
    except asyncio.CancelledError:
        pass
    assert ("/riotclient/kill-ux", "POST") in [(m, p) for m, p in client.calls] or (
        "POST",
        "/riotclient/kill-ux",
    ) in client.calls


@pytest.mark.asyncio
async def test_never_kills_during_game():
    """选人/对局中绝不执行 kill-ux。"""
    for phase in (
        "ChampSelect",
        "InProgress",
        "GameLaunch",
        "WaitingForStats",
        "Ended",
    ):
        client = FakeClient(phase=phase)
        saver, _ = _fast_saver(client)
        try:
            await saver.run_loop()
        except asyncio.CancelledError:
            pass
        assert client.calls == [], f"phase={phase} 不应执行 kill-ux"


@pytest.mark.asyncio
async def test_applies_once_per_session():
    """同一客户端会话只执行一次。"""
    client = FakeClient(phase="None")
    saver, ticks = _fast_saver(client, loops=4)
    try:
        await saver.run_loop()
    except asyncio.CancelledError:
        pass
    kill_calls = [c for c in client.calls if "kill-ux" in c[1]]
    assert len(kill_calls) == 1
    assert ticks["n"] >= 5  # 周期循环持续运行


@pytest.mark.asyncio
async def test_reapplies_for_new_session():
    """客户端重启（新端口/token）后重新应用。"""
    client = FakeClient(phase="None", port=1111)
    saver, _ = _fast_saver(client, loops=40)  # 保持循环运行直到手动取消

    async def cycle():
        try:
            await saver.run_loop()
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(cycle())
    await asyncio.sleep(0.4)
    assert len([c for c in client.calls if "kill-ux" in c[1]]) == 1
    # 模拟客户端重启：换端口换 token
    client.creds = FakeCreds(2222, "tok2")
    await asyncio.sleep(0.4)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    kill_calls = [c for c in client.calls if "kill-ux" in c[1]]
    assert len(kill_calls) == 2


@pytest.mark.asyncio
async def test_disabled_mode_noop():
    client = FakeClient(phase="None")
    saver, _ = _fast_saver(client, mode="off")
    try:
        await saver.run_loop()
    except asyncio.CancelledError:
        pass
    assert client.calls == []
