"""Generate Hexdata cards through its normal, authenticated website UI."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("lol_presence.hexdata")
HEXDATA_URL = "https://hexdata.com.cn/matchmaking-position"


class HexdataError(Exception):
    """A safe, user-facing failure without browser or credential details."""


@dataclass(frozen=True)
class HexdataCard:
    game_id: str
    path: Path
    created_at: float
    cached: bool = False


class HexdataBrowser:
    """Use an isolated persistent profile without reading session credentials."""

    def __init__(self, data_dir: Path, config: dict):
        self.enabled = bool(config.get("enabled", False))
        self.channel = str(config.get("browser_channel", "chromium"))
        self.headless = bool(config.get("headless", False))
        self.cache_seconds = max(60, min(1800, int(config.get("cache_seconds", 300))))
        self.cooldown = max(15, int(config.get("cooldown_seconds", 30)))
        self.profile_dir = Path(data_dir) / "hexdata-browser"
        self.card_dir = Path(data_dir) / "hexdata-cards"
        self._runtime = None
        self._context = None
        self._lock = asyncio.Lock()
        self._active_task = None
        self._closed = False
        self._next_query = 0.0
        self._cache: dict[str, HexdataCard] = {}

    async def _start_browser(self) -> None:
        """Start only on demand, without changing browser security defaults.

        Raises:
            HexdataError: The feature is disabled or its browser is unavailable.
        """
        if not self.enabled:
            raise HexdataError(
                "海斗查分尚未启用，请管理员在插件配置中开启 Hexdata 网页查分。"
            )
        if self._closed:
            raise HexdataError("查分服务正在重载，请稍后再试。")
        if self._context is not None:
            if any(not page.is_closed() for page in self._context.pages):
                return
            # A user may close the dedicated window between two commands.
            # Reopen its own profile, never attach to the daily browser.
            await self._context.close()
            self._context = None
            if self._runtime is not None:
                await self._runtime.stop()
                self._runtime = None
        if self.channel not in {"chromium", "msedge", "chrome"}:
            raise HexdataError(
                "查分浏览器配置无效，请选择 chromium、msedge 或 chrome。"
            )
        try:
            from playwright.async_api import async_playwright

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._runtime = await async_playwright().start()
            self._context = await self._runtime.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel=None if self.channel == "chromium" else self.channel,
                headless=self.headless,
                locale="zh-CN",
                viewport={"width": 1360, "height": 1000},
                accept_downloads=False,
                timeout=30000,
            )
            self._context.set_default_timeout(10000)
        except Exception as exc:
            if self._runtime is not None:
                await self._runtime.stop()
            self._runtime = self._context = None
            logger.warning("Hexdata browser startup failed (%s)", type(exc).__name__)
            raise HexdataError(
                "无法启动查分浏览器。请安装 Playwright 及对应浏览器，"
                "Windows 可配置使用已安装的 msedge；不要同时打开同一个专用配置目录。"
            ) from None

    async def open_login(self) -> None:
        """Open the dedicated window for a human to sign in.

        Raises:
            HexdataError: Another query is active or the browser cannot open.
        """
        if self.headless:
            raise HexdataError("请先在插件配置中关闭 headless，再使用 /lol 查分 登录。")
        if self._lock.locked():
            raise HexdataError("正在生成其他战绩卡，请完成后再打开登录窗口。")
        async with self._lock:
            await self._start_browser()
            try:
                pages = [p for p in self._context.pages if not p.is_closed()]
                page = pages[0] if pages else await self._context.new_page()
                await page.goto(
                    HEXDATA_URL, wait_until="domcontentloaded", timeout=30000
                )
                await page.bring_to_front()
            except Exception as exc:
                logger.warning("Hexdata login window failed (%s)", type(exc).__name__)
                raise HexdataError("登录窗口打开失败，请重载插件后重试。") from None

    async def get_card(self, game_id: str) -> HexdataCard:
        """Return a fresh or clearly marked cached card with bounded concurrency.

        Args:
            game_id: Full Riot ID, including the tag after '#'.

        Returns:
            An original-size PNG exported from the generated card image.

        Raises:
            HexdataError: Invalid input, login, quota, UI, or browser failure.
        """
        game_id = unicodedata.normalize("NFKC", game_id).strip()
        if (
            len(game_id) > 160
            or not re.fullmatch(r"[^#\x00-\x1f]+#[^#\s\x00-\x1f]+", game_id)
            or not game_id.rsplit("#", 1)[0].strip()
        ):
            raise HexdataError(
                "请输入完整游戏 ID：/lol 查分 游戏名#标签（游戏名可含空格）。"
            )
        if self._closed:
            raise HexdataError("查分服务正在重载，请稍后再试。")
        if not self.enabled:
            raise HexdataError(
                "海斗查分尚未启用，请管理员在插件配置中开启 Hexdata 网页查分。"
            )
        key = game_id.casefold()
        cached = self._cache.get(key)
        if (
            cached
            and time.time() - cached.created_at < self.cache_seconds
            and cached.path.is_file()
        ):
            return HexdataCard(
                cached.game_id, cached.path, cached.created_at, cached=True
            )
        if self._lock.locked():
            raise HexdataError("正在生成其他战绩卡，请稍后再试，不必重复发送指令。")
        remaining = self._next_query - time.monotonic()
        if remaining > 0:
            raise HexdataError(f"查分冷却中，请 {int(remaining) + 1} 秒后再试。")
        async with self._lock:
            self._active_task = asyncio.current_task()
            self._next_query = time.monotonic() + self.cooldown
            try:
                await self._start_browser()
                card = await asyncio.wait_for(self._generate_card(game_id), timeout=120)
                self._cache[key] = card
                if len(self._cache) > 64:
                    self._cache.pop(next(iter(self._cache)))
                return card
            except HexdataError:
                raise
            except asyncio.TimeoutError:
                raise HexdataError(
                    "网页查询或战绩卡生成超时，本次未发送图片，请稍后再试。"
                ) from None
            except Exception as exc:
                logger.warning("Hexdata UI query failed (%s)", type(exc).__name__)
                raise HexdataError(
                    "查分网页操作失败或浏览器已关闭，请重载插件后重试。"
                ) from None
            finally:
                self._active_task = None

    async def _generate_card(self, game_id: str) -> HexdataCard:
        """Operate visible controls and export only the verified card image.

        Args:
            game_id: Validated Riot ID for this one query.

        Returns:
            The card rendered by Hexdata, never a locally fabricated score.

        Raises:
            HexdataError: Login, quota, verification, or image validation failed.
        """
        page = await self._context.new_page()
        try:
            await page.goto(HEXDATA_URL, wait_until="domcontentloaded", timeout=30000)
            await page.get_by_role("textbox", name="游戏 ID", exact=True).fill(game_id)
            await page.get_by_role("button", name="查分", exact=True).click()
            # Observe DOM state only. Do not call private APIs, read tokens, solve
            # verification challenges, or change automation detection settings.
            await page.wait_for_function(
                """() => {
                  const article = document.querySelector('[data-matchmaking-state]');
                  const state = article?.getAttribute('data-matchmaking-state');
                  const text = document.body.innerText;
                  return ['ready','collecting','rate_limited','not_found','failed'].includes(state)
                    || document.querySelector('[role="dialog"]') !== null
                    || /验证码|安全验证|不支持机器人或脚本|仅支持在官网查询/.test(text);
                }""",
                timeout=45000,
            )
            text = await page.locator("body").inner_text()
            if re.search(r"验证码|安全验证|不支持机器人或脚本|仅支持在官网查询", text):
                raise HexdataError(
                    "站点要求登录/人工验证或限制自动化，本次已停止。请在专用浏览器中检查，不会自动绕过。"
                )
            if await page.get_by_role("dialog").count():
                raise HexdataError(
                    "Hexdata 未登录或登录已过期，请管理员在本机使用 /lol 查分 登录，并手动完成登录。"
                )
            state = await page.locator("[data-matchmaking-state]").get_attribute(
                "data-matchmaking-state"
            )
            if state == "rate_limited":
                self._next_query = max(self._next_query, time.monotonic() + 60)
                raise HexdataError(
                    "Hexdata 查询额度用尽或操作过快，请在官网查看重置时间；不会自动重复请求。"
                )
            if state == "not_found":
                raise HexdataError(
                    "Hexdata 未找到该账号，请核对游戏名和 # 后的标签。未找到不代表 0 分。"
                )
            if state not in {"ready", "collecting"}:
                raise HexdataError("Hexdata 暂时无法完成查询，请稍后再试或在官网查看。")

            card_region = page.get_by_role("region", name="战绩卡", exact=True)
            generate = card_region.get_by_role("button", name="生成战绩卡", exact=True)
            if await generate.count():
                await generate.click()
            await page.wait_for_function(
                """() => {
                  const card = document.querySelector('[data-card-state]');
                  return card && ['ready','failed','expired'].includes(card.getAttribute('data-card-state'));
                }""",
                timeout=65000,
            )
            card_state = await card_region.get_attribute("data-card-state")
            if card_state != "ready":
                raise HexdataError(
                    "官网战绩卡生成失败或已过期，本次未发送图片；请稍后手动重试。"
                )
            image = card_region.get_by_role("img")
            alt = await image.get_attribute("alt") or ""
            expected = f"{game_id} 的 Hexdata 战绩卡"
            if unicodedata.normalize("NFKC", alt).casefold() != expected.casefold():
                raise HexdataError(
                    "战绩卡账号与本次查询不一致，已阻止发送旧卡。请核对 ID 后重试。"
                )
            await image.wait_for(state="visible", timeout=10000)
            await page.wait_for_function(
                """() => {
                  const img = document.querySelector('[data-card-state="ready"] img');
                  return img && img.complete && img.naturalWidth > 0;
                }""",
                timeout=10000,
            )
            # Export pixels from the displayed original-size image, not from an
            # authenticated API URL. No cookies or session storage are exported.
            encoded = await image.evaluate(
                """img => {
                  if (!img.complete || !img.naturalWidth || !img.naturalHeight
                      || img.naturalWidth > 8192 || img.naturalHeight > 8192) return null;
                  const canvas = document.createElement('canvas');
                  canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
                  canvas.getContext('2d').drawImage(img, 0, 0);
                  return canvas.toDataURL('image/png').split(',')[1];
                }"""
            )
            if not encoded or len(encoded) > 20_000_000:
                raise HexdataError(
                    "官网图片未加载完成或过大，本次未发送图片，请稍后再试。"
                )
            content = base64.b64decode(encoded, validate=True)
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HexdataError("官网未返回有效战绩卡图片，本次未发送。")
            self.card_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(game_id.casefold().encode()).hexdigest()[:24]
            path = self.card_dir / f"hexdata-{digest}.png"
            await asyncio.to_thread(path.write_bytes, content)
            return HexdataCard(game_id, path.resolve(), time.time())
        finally:
            try:
                await page.close()
            except Exception:
                logger.debug("Hexdata query tab was already closed")

    async def close(self) -> None:
        """Cancel an active lookup and release only the plugin-owned browser."""
        self._closed = True
        active = self._active_task
        if active is not None and active is not asyncio.current_task():
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        try:
            if self._context is not None:
                await self._context.close()
        finally:
            self._context = None
            if self._runtime is not None:
                await self._runtime.stop()
                self._runtime = None
