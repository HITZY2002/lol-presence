"""观察号客户端资源守护（可选功能，默认关闭）。

实测结论（2026-08，国服 16.17 客户端）：
- LCU 官方端点 POST /riotclient/kill-ux 可关闭客户端界面进程（LeagueClientUx
  及全部 CEF 渲染进程），后端 LeagueClient 继续持有聊天/好友 Presence/SGP token。
  实测资源：912MB/17 进程 -> 286MB/2 进程（-69%），监控链路（REST+WSS）不受影响。
- 进程优先级修改对提权启动的客户端默认被拒绝（需要同等权限），失败时静默忽略。

安全护栏：
- 仅在 gameflow-phase ∈ {None, Lobby} 时执行 kill-ux，绝不在选人/对局中执行
  （对局中客户端本来就会自己关掉 UI）。
- 客户端重连（新会话）后重新应用一次；用户手动重开界面后下一轮也会再关，
  这是该功能的语义，不需要此行为时请关闭本功能。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("lol_presence.resource")

# 这些 gameflow 阶段允许关闭 UI（None=大厅未选区, Lobby=房间外）
_SAFE_PHASES = {"None", "Lobby", ""}


class ResourceSaver:
    def __init__(
        self,
        mode: str = "kill_ux",
        delay_seconds: float = 90.0,
        check_interval: float = 120.0,
        set_priority: bool = True,
        get_client: Callable[[], object | None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.mode = mode
        self.delay_seconds = delay_seconds
        self.check_interval = check_interval
        self.set_priority = set_priority
        self._get_client = get_client or (lambda: None)
        self._sleep = sleep  # 测试可注入
        self._applied_for: tuple[int, str] | None = None  # (port, password) 标识会话
        self._first_run = True

    async def run_loop(self) -> None:
        # 启动后等待 delay_seconds，给登录/好友列表加载留时间
        await self._sleep(self.delay_seconds)
        while True:
            try:
                await self._apply_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug("[LOL] resource saver cycle error: %s", e)
            await self._sleep(self.check_interval)

    async def _apply_once(self) -> None:
        client = self._get_client()
        if client is None:
            return
        key = (client.creds.port, client.creds.password)
        if key == self._applied_for:
            return  # 本会话已处理过

        if self.mode != "kill_ux":
            return
        if not await self._safe_to_kill(client):
            return  # 阶段不安全，下轮再试
        result = await client.request("POST", "/riotclient/kill-ux")
        logger.info("[LOL] resource saver: UX closed (kill-ux) result=%s", result)
        self._applied_for = key

        if self.set_priority:
            await self._lower_priority()

    @staticmethod
    async def _safe_to_kill(client) -> bool:
        """仅在未进行任何对局流程时关闭 UI。"""
        try:
            data = await client.get("/lol-gameflow/v1/gameflow-phase")
            phase = str(data) if data else ""
            return phase in _SAFE_PHASES
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _lower_priority() -> None:
        """把客户端进程降为 BelowNormal。非提权环境会被拒绝，静默忽略。"""
        import subprocess

        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process LeagueClient,RiotClientServices -ErrorAction SilentlyContinue "
                "| ForEach-Object { try { $_.PriorityClass = 'BelowNormal' } catch {} }",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=20)
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            pass
