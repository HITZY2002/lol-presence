"""英雄 ID → 名称映射。

数据源：腾讯官方 CDN hero_list.js（game.gtimg.cn），
字段为 {heroId, name, title, alias} 结构（JS 变量赋值，需清洗）。
失败时尝试 Data Dragon zh_CN 作为兜底；两者都失败则显示 heroId。
"""

from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp

HERO_LIST_URL = "https://game.gtimg.cn/images/lol/act/img/js/heroList/hero_list.js"
DDRAGON_URL = (
    "https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json"
)

_CACHE_TTL = 24 * 3600


class ChampionMap:
    def __init__(self, cdn_url: str = HERO_LIST_URL):
        self.cdn_url = cdn_url or HERO_LIST_URL
        self._id_to_name: dict[int, str] = {}
        self._id_to_alias: dict[int, str] = {}
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def ensure_loaded(self) -> None:
        async with self._lock:
            if self._id_to_name and time.time() - self._fetched_at < _CACHE_TTL:
                return
            if not await self._fetch_gtimg():
                if not await self._fetch_ddragon():
                    return  # 保持旧缓存或空
            self._fetched_at = time.time()

    async def _fetch_gtimg(self) -> bool:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(self.cdn_url) as resp:
                    if resp.status != 200:
                        return False
                    text = await resp.text()
            # hero_list.js 内容形如: var heroList = {...}; 或直接 JSON
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                return False
            data = json.loads(m.group(0))
            lst = data.get("hero") or []
            mapping: dict[int, str] = {}
            aliases: dict[int, str] = {}
            for h in lst:
                try:
                    hid = int(h.get("heroId", 0))
                except (TypeError, ValueError):
                    continue
                if hid <= 0:
                    continue
                mapping[hid] = f"{h.get('name', '')}·{h.get('title', '')}".strip(
                    "·"
                ) or str(hid)
                aliases[hid] = h.get("alias", "")
            if mapping:
                self._id_to_name = mapping
                self._id_to_alias = aliases
                return True
            return False
        except Exception:
            return False

    async def _fetch_ddragon(self) -> bool:
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(
                    "https://ddragon.leagueoflegends.com/api/versions.json"
                ) as r:
                    if r.status != 200:
                        return False
                    versions = await r.json()
                if not versions:
                    return False
                async with session.get(DDRAGON_URL.format(version=versions[0])) as r2:
                    if r2.status != 200:
                        return False
                    data = await r2.json()
            mapping: dict[int, str] = {}
            for champ in (data.get("data") or {}).values():
                mapping[int(champ["key"])] = champ.get("name") or champ["id"]
            if mapping:
                self._id_to_name = mapping
                self._id_to_alias = {}
                return True
            return False
        except Exception:
            return False

    def name(self, champion_id: int | None) -> str | None:
        if not champion_id:
            return None
        return self._id_to_name.get(int(champion_id))

    def display(self, champion_id: int | None) -> str | None:
        """优先中文名，失败返回 None（调用方不得显示猜测值）。"""
        if not champion_id:
            return None
        cid = int(champion_id)
        if cid in self._id_to_name:
            return self._id_to_name[cid]
        if cid in self._id_to_alias:
            return self._id_to_alias[cid]
        return None


# ---------------- 队列映射 ----------------

# 常见国服/通用 queueId → 展示名（取自 Riot queue.json 常用子集）
QUEUE_MAP: dict[int, str] = {
    420: "单双排",
    430: "灵活排位",
    440: "灵活组排",
    450: "极地大乱斗",
    900: "终极魔典",
    1020: "轮换模式",
    1090: "云顶之弈",
    1100: "云顶之弈排位",
    1130: "无限火力",
    1200: "极限闪击",
    1700: "斗魂竞技场",
    2400: "海克斯大乱斗",
    9001: "训练模式",
    76: "无限乱斗",
    986: "无限火力(6v6)",
}

DEFAULT_QUEUE_TEXT = "匹配"


def queue_name(queue_id: int | None) -> str:
    if queue_id is None:
        return DEFAULT_QUEUE_TEXT
    return QUEUE_MAP.get(int(queue_id), DEFAULT_QUEUE_TEXT)


# 云顶等不需要英雄名的模式
NO_CHAMPION_QUEUE_IDS = {1090, 1100, 1160}


def shows_champion(queue_id: int | None) -> bool:
    return queue_id not in NO_CHAMPION_QUEUE_IDS
