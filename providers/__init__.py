"""Provider 工厂与导出。

auto 模式决策顺序（与 docs/research.md 能力矩阵一致）：
1. wegame  —— 腾讯云端 Presence（若可用）
2. lcu     —— 本地 League Client 观察号（云端不可用时的实时层）
3. 均不可用 => 返回 None（播报停用，查询指令降级）
mock 模式仅供测试。
"""

from __future__ import annotations

import logging

from .base import BaseProvider, MatchProvider, ProviderCapabilities
from .mock import MockMatchProvider, MockPresenceProvider

logger = logging.getLogger("lol_presence.providers")

__all__ = [
    "BaseProvider",
    "MatchProvider",
    "ProviderCapabilities",
    "MockPresenceProvider",
    "MockMatchProvider",
    "build_providers",
]


async def build_providers(
    mode: str,
    poll_interval: float,
    reconcile_interval: float,
    cfg: dict,
) -> BaseProvider | None:
    """构建 Presence Provider。失败返回 None，绝不静默伪造数据源。"""
    mode = (mode or "auto").lower()

    if mode == "mock":
        logger.warning("[LOL] using MOCK presence provider (测试模式)")
        return MockPresenceProvider(poll_interval=min(poll_interval, 10))

    if mode in ("auto", "wegame"):
        provider = await _try_wegame(cfg)
        if provider is not None:
            return provider
        if mode == "wegame":
            logger.error("[LOL] wegame provider 不可用，请检查配置或改用 auto/lcu")

    if mode in ("auto", "lcu"):
        provider = await _try_lcu(cfg, poll_interval, reconcile_interval)
        if provider is not None:
            return provider
        if mode == "lcu":
            logger.error("[LOL] 未发现本地 League Client，lcu provider 启动失败")

    if mode == "auto":
        logger.warning(
            "[LOL] 无可用 Presence 数据源（云端不可用且未发现本地 League Client）。"
            "如需实时播报，请在一台机器上部署观察号客户端，或将本插件与 LOL 客户端同机部署。"
        )
    return None


async def _try_wegame(cfg: dict) -> BaseProvider | None:
    try:
        from .wegame import WegamePresenceProvider
    except ImportError:
        return None
    provider = WegamePresenceProvider.from_config(cfg.get("wegame") or {})
    if provider is None:
        return None
    if await provider.health_check():
        logger.info("[LOL] presence provider=wegame (cloud)")
        return provider
    logger.info("[LOL] wegame cloud 不可用，尝试下一数据源")
    return None


async def _try_lcu(
    cfg: dict, poll_interval: float, reconcile_interval: float
) -> BaseProvider | None:
    from .lcu import LcuPresenceProvider, discover_lcu

    lcu_cfg = cfg.get("lcu") or {}
    creds = await discover_lcu(
        auto_detect=bool(lcu_cfg.get("auto_detect", True)),
        lockfile_dir=str(lcu_cfg.get("lockfile_dir") or ""),
        install_dir=str(lcu_cfg.get("install_dir") or ""),
        host=str(lcu_cfg.get("host") or ""),
        password=str(lcu_cfg.get("password") or ""),
    )
    if creds is None:
        return None
    logger.info("[LOL] discovered League Client on port %s", creds.port)
    return LcuPresenceProvider(
        creds,
        poll_interval=poll_interval,
        reconcile_interval=reconcile_interval,
        use_ws=True,
    )
