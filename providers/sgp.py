"""SGP（腾讯 Service Gateway Proxy）数据源 —— 战绩/段位/游戏中状态层。

调研结论（2026-08，详见 docs/research.md）：
- 域名：{sub}-sgp.lol.qq.com:21019（hn1-k8s / hn10-k8s / tj100 / tj101 /
  nj100 / gz100 / cq100 / bgp2-k8s），服务实测在线（401=缺 token）。
- 认证：两个 JWT 均从运行中的 League Client 取得：
    * entitlements: LCU GET /entitlements/v1/token        -> Bearer accessToken
    * league-session: LCU GET /lol-league-session/v1/league-session-token -> 原文 JWT
  纯云端（无客户端）获取 token 目前无公开复现，因此 SGP 与 LCU 观察号
  是共生关系，不是替代关系。
- 端点（来源 LeagueAkari 源码，2026-05~08 快照验证）：
    * GET /match-history-query/v1/products/lol/player/{puuid}/SUMMARY  (entitlements)
    * GET /match-history-query/v1/products/lol/{sub}_{gid}/DETAILS     (entitlements)
    * GET /leagues-ledge/v2/rankedStats/puuid/{puuid}                  (league-session)
    * GET /gsm/v1/ledge/region/{sub}/puuid/{puuid}                     (league-session)
    * GET https://prod-rso.lol.qq.com:3001/aliases/v1/aliases?gameName=&tagLine= (entitlements)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

import aiohttp

from ..models.match import MatchParticipant, MatchSummary, RankedInfo
from .base import MatchProvider, _sanitize

logger = logging.getLogger("lol_presence.sgp")

# 腾讯 SGP 服务器（LeagueAkari builtin.ts，2026-07 更新，全部 :21019）
SGP_SERVERS: dict[str, str] = {
    "hn1": "https://hn1-k8s-sgp.lol.qq.com:21019",
    "hn10": "https://hn10-k8s-sgp.lol.qq.com:21019",
    "tj100": "https://tj100-sgp.lol.qq.com:21019",
    "tj101": "https://tj101-sgp.lol.qq.com:21019",
    "nj100": "https://nj100-sgp.lol.qq.com:21019",
    "gz100": "https://gz100-sgp.lol.qq.com:21019",
    "cq100": "https://cq100-sgp.lol.qq.com:21019",
    "bgp2": "https://bgp2-k8s-sgp.lol.qq.com:21019",
}

RSO_ALIASES_URL = "https://prod-rso.lol.qq.com:3001/aliases/v1/aliases"

_TOKEN_REFRESH_MARGIN = 300.0  # 过期前 5 分钟刷新


def _jwt_exp(token: str) -> float:
    """解析 JWT exp（失败返回 0 => 视为随时过期）。"""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp", 0))
    except Exception:  # noqa: BLE001
        return 0.0


class SgpError(Exception):
    pass


class SgpAuthError(SgpError):
    pass


class SgpRateLimitError(SgpError):
    pass


class SgpProvider(MatchProvider):
    """SGP 战绩/段位 Provider。需要传入已连接的 LcuClient 供 token 获取。"""

    name = "sgp"

    def __init__(self, lcu_client, server_id: str = "hn1", insecure: bool = False):
        self.lcu = lcu_client  # providers.lcu.LcuClient
        self.server_id = server_id if server_id in SGP_SERVERS else "hn1"
        self.base_url = SGP_SERVERS[self.server_id]
        self._insecure = insecure
        self._session: aiohttp.ClientSession | None = None
        self._entitlements: str | None = None
        self._entitlements_exp: float = 0.0
        self._league_session: str | None = None
        self._league_session_exp: float = 0.0
        self._lock = asyncio.Lock()

    # ---------- token ----------

    async def _get_token(self, kind: str) -> str:
        async with self._lock:
            if kind == "entitlements":
                if (
                    self._entitlements
                    and time.time() < self._entitlements_exp - _TOKEN_REFRESH_MARGIN
                ):
                    return self._entitlements
                data = await self.lcu.get("/entitlements/v1/token")
                token = (
                    (data or {}).get("accessToken") if isinstance(data, dict) else None
                )
                if not token:
                    raise SgpAuthError("entitlements token unavailable from LCU")
                self._entitlements = token
                self._entitlements_exp = _jwt_exp(token) or (time.time() + 1800)
                return token
            else:  # league-session
                if (
                    self._league_session
                    and time.time() < self._league_session_exp - _TOKEN_REFRESH_MARGIN
                ):
                    return self._league_session
                token = await self.lcu.get_text(
                    "/lol-league-session/v1/league-session-token"
                )
                if not token:
                    raise SgpAuthError("league-session token unavailable from LCU")
                self._league_session = token
                self._league_session_exp = _jwt_exp(token) or (time.time() + 1800)
                return token

    async def invalidate_tokens(self) -> None:
        self._entitlements = None
        self._league_session = None

    # ---------- HTTP ----------

    def _ssl(self):
        if self._insecure:
            import ssl

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return None

    async def _session_http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=12),
                connector=aiohttp.TCPConnector(ssl=self._ssl()),
            )
        return self._session

    async def _sgp_get(
        self, path: str, token_kind: str, params: dict | None = None
    ) -> dict:
        token = await self._get_token(token_kind)
        s = await self._session_http()
        async with s.get(
            f"{self.base_url}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        ) as resp:
            if resp.status == 401:
                await self.invalidate_tokens()
                raise SgpAuthError("sgp 401; token refreshed for next call")
            if resp.status == 404:
                return {}
            if resp.status == 429:
                raise SgpRateLimitError("sgp 429 rate limited")
            if resp.status >= 500:
                raise SgpError(f"sgp server error {resp.status}")
            return await resp.json(content_type=None) or {}

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- MatchProvider ----------

    async def get_match_history(
        self, puuid: str, start: int = 0, count: int = 10
    ) -> list[MatchSummary]:
        data = await self._sgp_get(
            f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY",
            token_kind="entitlements",
            params={"startIndex": start, "count": count},
        )
        games = data.get("games") or []
        out = []
        for g in games:
            try:
                out.append(self._summary_from_sgp(g, self.server_id))
            except (KeyError, TypeError, ValueError) as e:
                logger.debug("[LOL] skip malformed sgp game: %s", _sanitize(e))
        return out

    async def get_match_detail(self, puuid: str, game_id: str) -> MatchSummary | None:
        data = await self._sgp_get(
            f"/match-history-query/v1/products/lol/{self.server_id}_{game_id}/DETAILS",
            token_kind="entitlements",
        )
        if data:
            return self._summary_from_sgp(data, self.server_id)
        return None

    async def get_ranked(self, puuid: str) -> list[RankedInfo]:
        data = await self._sgp_get(
            f"/leagues-ledge/v2/rankedStats/puuid/{puuid}",
            token_kind="league-session",
        )
        queue_map = data.get("queueMap") or {}
        out: list[RankedInfo] = []
        for qt, q in queue_map.items():
            if not isinstance(q, dict) or not q.get("tier"):
                continue
            out.append(
                RankedInfo(
                    puuid=puuid,
                    queue_type=qt,
                    tier=str(q.get("tier") or ""),
                    division=str(q.get("division") or ""),
                    league_points=int(
                        q.get("leaguePoints") or q.get("leaguePoint") or 0
                    ),
                    wins=int(q.get("wins") or 0),
                    losses=int(q.get("losses") or 0),
                )
            )
        return out

    async def get_game_state(self, puuid: str) -> dict | None:
        """GSM 点查：任意玩家当前是否在游戏中（含阵容/英雄）。
        404/空 => 不在游戏中。供扩展使用（如非好友的开局检测）。"""
        try:
            return await self._sgp_get(
                f"/gsm/v1/ledge/region/{self.server_id}/puuid/{puuid}",
                token_kind="league-session",
            )
        except SgpError:
            return None

    async def resolve_alias(self, game_name: str, tag_line: str = "") -> str | None:
        """按名字查 puuid（全大区；tag 可省略）。失败返回 None。"""
        params = {"gameName": game_name}
        if tag_line:
            params["tagLine"] = tag_line
        try:
            token = await self._get_token("entitlements")
            s = await self._session_http()
            async with s.get(
                RSO_ALIASES_URL,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 404:
                    return None
                if resp.status == 401:
                    await self.invalidate_tokens()
                    raise SgpAuthError("aliases 401")
                data = await resp.json(content_type=None)
            aliased = (data or {}).get("aliasedEntities") or []
            if aliased:
                return aliased[0].get("puuid")
            return None
        except SgpAuthError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("[LOL] sgp alias lookup failed: %s", _sanitize(e))
            return None

    async def health_check(self) -> bool:
        try:
            await self._get_token("entitlements")
            return True
        except Exception:  # noqa: BLE001
            return False

    # ---------- 响应解析 ----------

    @staticmethod
    def _summary_from_sgp(g: dict, server_id: str) -> MatchSummary:
        meta = g.get("metadata") or {}
        body = g.get("json") or {}
        participants = [
            MatchParticipant(
                puuid=p.get("puuid") or "",
                champion_id=int(p.get("championId") or 0),
                kills=int(p.get("kills") or 0),
                deaths=int(p.get("deaths") or 0),
                assists=int(p.get("assists") or 0),
                win=bool(p.get("win", False)),
                team_id=int(p.get("teamId") or 0),
            )
            for p in (body.get("participants") or [])
            if isinstance(p, dict)
        ]
        duration = int(body.get("gameDuration") or 0)
        end_ms = int(body.get("gameEndTimestamp") or 0)
        if not end_ms:
            ts = meta.get("timestamp")
            if ts:
                try:
                    from datetime import datetime

                    end_ms = int(
                        datetime.fromisoformat(
                            str(ts).replace("Z", "+00:00")
                        ).timestamp()
                        * 1000
                    )
                except ValueError:
                    end_ms = 0
        if not end_ms and duration:
            end_ms = int(body.get("gameCreation") or 0) + duration * 1000
        match_id = str(meta.get("match_id") or "")
        game_id = str(
            body.get("gameId") or (match_id.split("_", 1)[-1] if match_id else "")
        )
        return MatchSummary(
            game_id=game_id,
            queue_id=int(body.get("queueId") or 0),
            game_mode=str(body.get("gameMode") or ""),
            map_id=int(body.get("mapId") or 0),
            game_duration_seconds=duration,
            game_creation_ms=int(body.get("gameCreation") or 0),
            game_end_time_ms=end_ms,
            participants=participants,
        )
