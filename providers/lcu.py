"""本地 League Client (LCU) 数据源 —— Presence fallback 层。

适用场景：云端 WeGame Presence 不可用时，用一台长期登录的国服观察号客户端
（观察号添加全部群友为好友），通过 LCU 的好友 Presence 判断上线/游戏中：

- REST  : GET /lol-chat/v1/friends        全量好友快照
- WSS   : OnJsonApiEvent_lol-chat_v1_friends  好友状态增量推送

注意：监控对象是【好友 Presence】，不是本机 gameflow-phase。
后者只表示观察号自己的流程，禁止用于判断群友状态。

国服说明：客户端由 WeGame/TCLS 启动，LCU 端口与 token 通过
LeagueClientUx.exe 进程参数或 lockfile 获取，认证与外服一致
(Basic riot:<remoting-auth-token>)。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import ssl
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from ..models.match import MatchParticipant, MatchSummary, RankedInfo
from ..models.presence import (
    PlayerPresence,
    PresenceState,
    map_game_status,
)
from .base import (
    BaseProvider,
    MatchProvider,
    ObservationStatus,
    ProviderCapabilities,
    _sanitize,
)

logger = logging.getLogger("lol_presence.lcu")

# 允许 LolGameStatus 原始值缺失时的处理：见 _presence_from_friend


@dataclass
class LcuCredentials:
    host: str
    port: int
    password: str
    protocol: str = "https"

    @property
    def base_url(self) -> str:
        return f"{self.protocol}://{self.host}:{self.port}"

    def auth_header(self) -> str:
        raw = f"riot:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()


# ---------------- 发现 LCU ----------------

# 国服 WeGame/TCLS 常见安装目录（lockfile 候选）
_LOCKFILE_CANDIDATE_DIRS = [
    r"C:\WeGame\wegame_op\game\TCLS\LeagueClient",
    r"C:\WeGame\wegame_op\game\TCLS\league_client",
    r"D:\WeGame\wegame_op\game\TCLS\LeagueClient",
    r"E:\WeGame\wegame_op\game\TCLS\LeagueClient",
    r"C:\Riot Games\League of Legends",
    r"D:\Riot Games\League of Legends",
]

# 2026 国服注册表安装路径键（HKCU，无需提权）
_CN_REGISTRY_KEY = r"SOFTWARE\Tencent\LoL"


def _cn_install_dir_from_registry() -> str | None:
    """从注册表读国服安装目录（HKCU\\SOFTWARE\\Tencent\\LoL\\InstallPath）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CN_REGISTRY_KEY) as k:
            val, _ = winreg.QueryValueEx(k, "InstallPath")
            return val if val else None
    except OSError:
        return None


def _parse_lockfile_parts(parts: list[str]) -> tuple[int, str] | None:
    """兼容两种 lockfile 格式，返回 (port, password)。

    经典: LeagueClient:<port>:<password>:riot:<protocol>
    2026 国服 Riot Client: Riot Client:<pid>:<port>:<password>:<protocol>
    """
    if len(parts) >= 5 and parts[3] == "riot":
        try:
            return int(parts[1]), parts[2]
        except ValueError:
            return None
    if len(parts) >= 5 and parts[1].isdigit() and parts[2].isdigit():
        # 新格式：field2=PID, field3=port, field4=password
        return int(parts[2]), parts[3]
    return None


def _tcp_listening(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Return whether an LCU candidate port is currently listening."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _discover_via_ux_log(install_dir: Path) -> LcuCredentials | None:
    """解析最新的 LeagueClientUx 日志拿 LCU 端口与 token。

    2026 国服客户端 lockfile 为空（日志报 'Lockfile contents invalid'），
    但 UX 日志的 'Command line arguments:' 行包含完整
    --app-port / --remoting-auth-token，且普通权限可读。
    """
    try:
        logs = sorted(
            (install_dir / "LeagueClient").glob("*LeagueClientUx.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    # Old client sessions leave valid-looking credentials in their logs. Scan
    # newest-to-oldest and accept only the session whose port is still alive.
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        port = re.search(r"--app-port=(\d+)", text)
        tok = re.search(r"--remoting-auth-token=([\w-]+)", text)
        if port and tok:
            creds = LcuCredentials(
                host="127.0.0.1",
                port=int(port.group(1)),
                password=tok.group(1),
            )
            # Stale UX logs are common after resource-saver closes the UI. A
            # blocking socket timeout for every old log can freeze AstrBot's
            # event loop for many seconds, so probe ports in a worker thread.
            if await asyncio.to_thread(_tcp_listening, creds.port):
                return creds
            logger.debug(
                "[LOL] ux log port %s not listening (stale session)", creds.port
            )
    return None


async def _discover_via_riot_client(install_dir: Path) -> LcuCredentials | None:
    """2026 国服 Riot Client 架构发现链：
    Riot Client lockfile(新5字段) -> API 认证可用 -> UX 日志解析 LCU 凭据。"""
    rc_lock = install_dir / "Riot Client Data" / "User Data" / "Config" / "lockfile"
    if rc_lock.is_file():
        try:
            parts = (
                rc_lock.read_text(encoding="utf-8", errors="replace").strip().split(":")
            )
            parsed = _parse_lockfile_parts(parts)
            if parsed:
                # Riot Client API 可用性佐证（失败不阻塞，UX 日志才是凭据来源）
                logger.debug("[LOL] riot client api on port %s", parsed[0])
        except OSError:
            pass
    return await _discover_via_ux_log(install_dir)


async def _discover_via_process() -> LcuCredentials | None:
    """Windows: 从 LeagueClientUx.exe 命令行参数直接拿端口与 token。

    仅当本进程有足够权限读取提权启动的客户端 cmdline 时可用；
    无权限时由 lockfile/UX 日志通道兜底。"""
    if sys.platform != "win32":
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell",
            "-NoProfile",
            "-Command",
            "(Get-CimInstance Win32_Process -Filter \"Name='LeagueClientUx.exe'\" "
            "| Select-Object -First 1).CommandLine",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        cmdline = out.decode(errors="ignore")
        port_m = re.search(r"--app-port=(\d+)", cmdline)
        tok_m = re.search(r"--remoting-auth-token=([\w-]+)", cmdline)
        if port_m and tok_m:
            return LcuCredentials(
                host="127.0.0.1",
                port=int(port_m.group(1)),
                password=tok_m.group(1),
            )
        return None
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as e:
        logger.debug("[LOL] LCU process discovery failed: %s", _sanitize(e))
        return None


async def _discover_via_lockfile(
    extra_dirs: list[str] | None = None,
) -> LcuCredentials | None:
    dirs = list(_LOCKFILE_CANDIDATE_DIRS)
    if extra_dirs:
        dirs = list(extra_dirs) + dirs
    for d in dirs:
        lock = Path(d) / "lockfile"
        if not lock.is_file():
            continue
        try:
            parts = (
                lock.read_text(encoding="utf-8", errors="replace").strip().split(":")
            )
            parsed = _parse_lockfile_parts(parts)
            if parsed and await asyncio.to_thread(_tcp_listening, parsed[0]):
                return LcuCredentials(
                    host="127.0.0.1",
                    port=parsed[0],
                    password=parsed[1],
                    protocol="https",
                )
        except (OSError, IndexError) as e:
            logger.debug("[LOL] bad lockfile at %s: %s", lock, _sanitize(e))
    return None


async def discover_lcu(
    auto_detect: bool = True,
    lockfile_dir: str = "",
    install_dir: str = "",
    host: str = "",
    password: str = "",
) -> LcuCredentials | None:
    """按 手动配置 -> 进程参数 -> 安装目录(注册表/候选) -> lockfile 的顺序发现 LCU。

    2026 国服注意：lockfile 为空是常态，依赖 UX 日志通道。"""
    if host and password:
        try:
            return LcuCredentials(
                host=host,
                port=int(host.split(":")[1]) if ":" in host else 2999,
                password=password,
            )
        except (ValueError, IndexError):
            return None
    if not auto_detect:
        return None

    # 1) 进程命令行（需要与本客户端同等权限）
    creds = await _discover_via_process()
    if creds:
        return creds

    # 2) 安装目录：注册表 + 配置 + 候选列表
    candidates: list[Path] = []
    for d in (install_dir, _cn_install_dir_from_registry()):
        if d:
            candidates.append(Path(d))
    if lockfile_dir:
        candidates.append(Path(lockfile_dir))
    candidates.extend(Path(d) for d in _LOCKFILE_CANDIDATE_DIRS)

    for inst in candidates:
        if not inst.is_dir():
            continue
        # 2026 国服链路：Riot Client lockfile + UX 日志
        creds = await _discover_via_riot_client(inst)
        if creds:
            return creds
        # 经典 lockfile（老客户端）
        creds = await _discover_via_lockfile([str(inst / "LeagueClient"), str(inst)])
        if creds:
            return creds
    return None


# ---------------- Presence 映射 ----------------


def _friend_puuid(friend: dict) -> str:
    """Read a PUUID from either the explicit field or the chat JID."""
    puuid = str(friend.get("puuid") or "")
    if puuid:
        return puuid
    candidate = str(friend.get("id") or friend.get("pid") or "").split("@", 1)[0]
    if re.fullmatch(r"[0-9a-fA-F-]{36}", candidate):
        return candidate
    return ""


def _presence_from_friend(friend: dict) -> PlayerPresence | None:
    """LCU 好友对象 -> PlayerPresence。

    关键规则：
    - availability == 'offline' 一律 OFFLINE（即使残留 lol 字段）
    - 否则按 lol.gameStatus 映射；缺失时保守给 ONLINE（客户端在线才会推送）
    - 英雄/队列等信息缺失就留空，绝不猜测
    """
    puuid = _friend_puuid(friend)
    if not puuid:
        return None
    availability = (friend.get("availability") or "").lower()
    product = str(friend.get("productName") or friend.get("product") or "").lower()
    lol = friend.get("lol") or {}
    raw_status = lol.get("gameStatus") or ""

    if availability == "offline":
        state = PresenceState.OFFLINE
    elif raw_status:
        mapped = map_game_status(raw_status)
        # map_game_status 对未知值返回 UNKNOWN；在线但状态未知保守按 ONLINE
        state = PresenceState.ONLINE if mapped == PresenceState.UNKNOWN else mapped
    elif product and not product.startswith("league_of_legends"):
        # Riot Mobile / another Riot game does not mean the League client is online.
        state = PresenceState.OFFLINE
    elif availability:
        state = PresenceState.ONLINE
    else:
        state = PresenceState.UNKNOWN

    champ = lol.get("championId")
    queue = lol.get("queueId")
    return PlayerPresence(
        puuid=puuid,
        state=state,
        availability=availability,
        raw_game_status=raw_status,
        champion_id=int(champ) if champ else None,
        queue_id=int(queue) if queue else None,
        game_id=str(lol["gameId"]) if lol.get("gameId") else None,
        game_mode=lol.get("gameMode") or "",
        game_queue_type=lol.get("gameQueueType") or "",
        ranked_tier=lol.get("rankedLeagueTier") or "",
        ranked_division=lol.get("rankedLeagueDivision") or "",
        game_name=friend.get("gameName") or friend.get("name") or "",
        tag_line=friend.get("gameTag") or friend.get("tagName") or "",
        platform=str(lol.get("platformId") or ""),
    )


# ---------------- LCU HTTP 客户端 ----------------


class LcuClient:
    def __init__(self, creds: LcuCredentials, timeout: float = 8.0):
        self.creds = creds
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # LCU 自签证书
        return ctx

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=aiohttp.TCPConnector(ssl=self._ssl_ctx()),
                headers={"Authorization": self.creds.auth_header()},
            )
        return self._session

    async def request(self, method: str, path: str, **kwargs) -> dict | list | None:
        s = await self.session()
        url = f"{self.creds.base_url}{path}"
        async with s.request(method, url, **kwargs) as resp:
            if resp.status == 404:
                return None
            if resp.status == 401:
                raise LcuAuthError("LCU auth rejected (401); client restarted?")
            if resp.status == 429:
                raise LcuRateLimitError("LCU rate limited (429)")
            if resp.status >= 500:
                raise LcuError(f"LCU server error {resp.status}")
            try:
                return await resp.json()
            except (aiohttp.ContentTypeError, json.JSONDecodeError):
                return None

    async def get(self, path: str) -> dict | list | None:
        return await self.request("GET", path)

    async def get_text(self, path: str) -> str:
        """返回原始文本响应（如 league-session JWT，响应体就是 token 本身）。"""
        s = await self.session()
        url = f"{self.creds.base_url}{path}"
        async with s.get(url) as resp:
            if resp.status != 200:
                raise LcuError(f"LCU {path} -> {resp.status}")
            text = (await resp.text()).strip()
            return text.strip('"')  # 兼容 JSON 字符串包裹

    async def ws_connect(self) -> aiohttp.ClientWebSocketResponse:
        s = await self.session()
        wss_url = f"wss://{self.creds.host}:{self.creds.port}/"
        return await s.ws_connect(
            wss_url,
            ssl=self._ssl_ctx(),
            heartbeat=30,
            autoping=True,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


class LcuError(Exception):
    pass


class LcuAuthError(LcuError):
    pass


class LcuRateLimitError(LcuError):
    pass


# ---------------- Presence Provider ----------------


class LcuPresenceProvider(BaseProvider):
    name = "lcu"
    capabilities = ProviderCapabilities(
        qq_to_lol_binding=False,
        summoner_lookup=True,
        presence=True,
        presence_granularity="fine",
        match_history=True,  # 旧版 LCU 接口，可能受限
        ranked=True,
        needs_watch_account=True,
        needs_local_client=True,
    )

    WS_EVENT = "OnJsonApiEvent_lol-chat_v1_friends"
    FRIENDS_PATH = "/lol-chat/v1/friends"

    def __init__(
        self,
        creds: LcuCredentials,
        poll_interval: float = 45.0,
        reconcile_interval: float = 300.0,
        use_ws: bool = True,
    ):
        super().__init__(poll_interval=poll_interval)
        self.client = LcuClient(creds)
        self.reconcile_interval = reconcile_interval
        self.use_ws = use_ws
        self._ws_connected = asyncio.Event()

    async def start(self) -> None:
        await super().start()
        if self.use_ws:
            self._tasks.append(asyncio.create_task(self._ws_loop(), name="lcu-ws"))
        self._tasks.append(
            asyncio.create_task(self._reconcile_loop(), name="lcu-reconcile")
        )
        if not self.use_ws:
            # 无 WS 时退化为 HTTP 轮询
            self._tasks.append(asyncio.create_task(self.poll_loop(), name="lcu-poll"))
        logger.info(
            "[LOL] presence provider=lcu mode=%s",
            "websocket" if self.use_ws else "http-poll",
        )

    async def stop(self) -> None:
        await super().stop()
        await self.client.close()
        logger.info("[LOL] lcu provider stopped")

    # ----- 核心数据流 -----

    async def _fetch_friends(self) -> list[dict]:
        data = await self.client.get(self.FRIENDS_PATH)
        if not isinstance(data, list):
            raise LcuError("LCU friends endpoint returned an invalid payload")
        # The CN client clears product fields when a League friend goes offline.
        # Private profiles may also omit gameName. Keep every confirmed Riot
        # friendship with a PUUID so an old online state can converge safely.
        friends: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            puuid = _friend_puuid(item)
            product = str(item.get("productName") or item.get("product") or "")
            confirmed_friend = (
                str(item.get("relationshipOnRiot") or "").lower() == "friend"
            )
            if not puuid or not (
                product.startswith("league_of_legends")
                or confirmed_friend
                or (not product and bool(item.get("gameName")))
            ):
                continue
            friend = dict(item)
            friend["puuid"] = puuid
            friends.append(friend)
        return friends

    async def snapshot(self) -> list[PlayerPresence]:
        friends = await self._fetch_friends()
        out = []
        seen: set[str] = set()
        for f in friends:
            p = _presence_from_friend(f)
            if p and (not self._monitored or p.puuid in self._monitored):
                out.append(p)
                seen.add(p.puuid)
        # A monitored account missing from a valid full roster is not offline:
        # its state is unobservable (usually not a friend). Explicit UNKNOWN
        # prevents a stale cached ONLINE/IN_GAME state from living forever.
        out.extend(
            PlayerPresence(
                puuid=puuid,
                state=PresenceState.UNKNOWN,
                raw_game_status="not_friend",
            )
            for puuid in self._monitored - seen
        )
        self.status.last_snapshot_at = asyncio.get_running_loop().time()
        return out

    async def resolve_player(
        self, query: str, qq_id: str = ""
    ) -> PlayerPresence | None:
        """LCU 侧解析：支持 游戏名#TAG(Riot ID) 与 召唤师名 两种。
        国服若 riot-id 接口不可用会自动落到召唤师名查询。"""
        query = (query or "").strip()
        if not query:
            return None
        if "#" in query:
            name, tag = query.split("#", 1)
            name = name.strip()
            tag = tag.strip()
            try:
                data = await self.client.get(
                    "/lol-summoner/v1/alias/lookup"
                    f"?gameName={_urlq(name)}&tagLine={_urlq(tag)}"
                )
                if isinstance(data, dict) and data.get("puuid"):
                    # The alias endpoint may return only a PUUID when career
                    # visibility is disabled. The submitted Riot ID is still
                    # authoritative display data for a successful lookup.
                    identity = dict(data)
                    identity["gameName"] = identity.get("gameName") or name
                    identity["gameTag"] = (
                        identity.get("gameTag") or identity.get("tagLine") or tag
                    )
                    return await self._presence_of_puuid(identity["puuid"], identity)
            except LcuError:
                pass
        # 兜底：按召唤师名（国服主方式）
        data = await self.client.get(f"/lol-summoner/v1/summoners?name={_urlq(query)}")
        if isinstance(data, dict) and data.get("puuid"):
            return await self._presence_of_puuid(data["puuid"], data)
        return None

    async def _presence_of_puuid(self, puuid: str, summoner: dict) -> PlayerPresence:
        """在好友列表中找该 puuid 的实时状态；找不到则构造未知占位。"""
        try:
            friends = await self._fetch_friends()
        except LcuError:
            # Identity lookup already succeeded. A temporary chat failure must
            # not turn a valid Riot ID into a binding failure.
            friends = []
        for f in friends:
            if f.get("puuid") == puuid:
                p = _presence_from_friend(f)
                if p:
                    return p
        return PlayerPresence(
            puuid=puuid,
            state=PresenceState.UNKNOWN,
            game_name=summoner.get("gameName") or summoner.get("name") or "",
            tag_line=summoner.get("gameTag") or summoner.get("tagLine") or "",
        )

    async def observation_status(self, puuid: str) -> ObservationStatus:
        me = await self.client.get("/lol-chat/v1/me")
        friends = await self._fetch_friends()
        me = me if isinstance(me, dict) else {}
        return ObservationStatus(
            observable=any(friend.get("puuid") == puuid for friend in friends),
            observer_game_name=str(me.get("gameName") or me.get("name") or ""),
            observer_tag_line=str(me.get("gameTag") or me.get("tagLine") or ""),
        )

    async def health_check(self) -> bool:
        try:
            await self.client.get("/lol-chat/v1/me")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ----- WebSocket 增量 + 周期对账 -----

    async def _ws_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                ws = await self.client.ws_connect()
                await ws.send_str(json.dumps([5, self.WS_EVENT]))
                logger.info("[LOL] presence connected (lcu websocket)")
                self.status.connected = True
                self._ws_connected.set()
                backoff = 1.0
                # 连接后立即对账一次，防止断线期间丢事件
                updates = await self.snapshot()
                if updates:
                    await self._emit(updates)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._on_ws_event(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                    if not self._running:
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.status.last_error = str(e)
                self._ws_connected.clear()
            self.status.connected = False
            self.status.reconnects += 1
            logger.warning("[LOL] lcu websocket lost, reconnect in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    async def _on_ws_event(self, payload: str) -> None:
        try:
            data = json.loads(payload)
            body = data[2] if len(data) > 2 else {}
            friend = body.get("data") if isinstance(body, dict) else None
            if not isinstance(body, dict):
                return
            friend = friend if isinstance(friend, dict) else {}
            puuid = _friend_puuid(friend)
            if not puuid:
                uri = str(body.get("uri") or "")
                candidate = uri.rsplit("/", 1)[-1].split("@", 1)[0]
                if re.fullmatch(r"[0-9a-fA-F-]{36}", candidate):
                    puuid = candidate
            if not puuid or not (not self._monitored or puuid in self._monitored):
                return
            if str(body.get("eventType") or "").lower() in {"delete", "remove"}:
                p = PlayerPresence(
                    puuid=puuid,
                    state=PresenceState.UNKNOWN,
                    raw_game_status="not_friend",
                )
            else:
                friend["puuid"] = puuid
                p = _presence_from_friend(friend)
            if p:
                await self._emit([p])
        except (json.JSONDecodeError, IndexError, KeyError, TypeError):
            logger.debug("[LOL] bad ws payload dropped")

    async def _reconcile_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.reconcile_interval)
            try:
                updates = await self.snapshot()
                if updates:
                    await self._emit(updates)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning("[LOL] lcu reconcile failed: %s", _sanitize(e))


# ---------------- Match/Ranked Provider (LCU 旧接口) ----------------


class LcuMatchProvider(MatchProvider):
    """通过 LCU 查战绩/段位。

    国服 LCU 的 match-history 接口近年多次受限（可能 404/403），
    上层应优先 SGP，本类只作 fallback，并在失败时如实抛错。
    """

    name = "lcu-match"

    def __init__(self, client: LcuClient):
        self.client = client

    async def get_match_history(
        self, puuid: str, start: int = 0, count: int = 10
    ) -> list[MatchSummary]:
        data = await self.client.get(
            f"/lol-match-history/v1/products/lol/{puuid}/matches"
            f"?start={start}&count={count}"
        )
        if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
            return []
        return [_summary_from_lcu(m) for m in data["matches"]]

    async def get_match_detail(self, puuid: str, game_id: str) -> MatchSummary | None:
        data = await self.client.get(
            f"/lol-match-history/v1/products/lol/{puuid}/matches/{game_id}"
        )
        if isinstance(data, dict):
            return _summary_from_lcu(data)
        return None

    async def get_ranked(self, puuid: str) -> list[RankedInfo]:
        data = await self.client.get(f"/lol-ranked/v1/ranked-stats/{puuid}")
        if not isinstance(data, dict):
            return []
        queue_map = data.get("queueMap") or {}
        out: list[RankedInfo] = []
        solo = queue_map.get("RANKED_SOLO_5x5") or {}
        flex = queue_map.get("RANKED_FLEX_SR") or {}
        for qt, q in (("RANKED_SOLO_5x5", solo), ("RANKED_FLEX_SR", flex)):
            if not q:
                continue
            out.append(
                RankedInfo(
                    puuid=puuid,
                    queue_type=qt,
                    tier=q.get("tier") or "",
                    division=q.get("division") or "",
                    league_points=int(q.get("leaguePoints") or 0),
                    wins=int(q.get("wins") or 0),
                    losses=int(q.get("losses") or 0),
                )
            )
        return out

    async def health_check(self) -> bool:
        return True  # 有 LCU 即可用；具体接口失败由调用处处理


def _summary_from_lcu(m: dict) -> MatchSummary:
    participants = []
    for p in m.get("participants") or []:
        participants.append(
            MatchParticipant(
                puuid=p.get("puuid") or "",
                champion_id=int(p.get("championId") or 0),
                kills=int(p.get("stats", {}).get("kills", 0)),
                deaths=int(p.get("stats", {}).get("deaths", 0)),
                assists=int(p.get("stats", {}).get("assists", 0)),
                win=bool(p.get("stats", {}).get("win", False)),
                team_id=int(p.get("teamId") or 0),
            )
        )
    return MatchSummary(
        game_id=str(m.get("gameId") or ""),
        queue_id=int(m.get("queueId") or 0),
        game_mode=m.get("gameMode") or "",
        map_id=int(m.get("mapId") or 0),
        game_duration_seconds=int(m.get("gameDuration") or 0),
        game_creation_ms=int(m.get("gameCreation") or 0),
        game_end_time_ms=int(
            m.get("gameEndTimestamp")
            or (
                int(m.get("gameCreation") or 0) + int(m.get("gameDuration") or 0) * 1000
            )
        ),
        participants=participants,
    )


def _urlq(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")
