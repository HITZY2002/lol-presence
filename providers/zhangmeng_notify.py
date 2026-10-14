"""掌盟通知 Presence Provider。

被动接收 Webhook 推送的掌盟通知，用可配置的解析规则提取玩家昵称和状态，
转换为统一的 PlayerPresence 上抛给 PresenceService。

架构约束：
- 不修改现有 LCU/SGP 功能，只作为新的 Presence Provider 增量接入
- 解析规则独立可配置（data/zhangmeng_patterns.json），不把 Regex 写死在业务逻辑里
- 同一玩家的同一类型事件在 cooldown 窗口内只提醒一次（Provider 层去重 + PresenceService 状态机双重保障）
- 未识别的通知由 Webhook 层写入 JSONL 日志，本层只负责解析和上抛
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from ..models.presence import PlayerPresence, PresenceState
from ..services.dedup import ZhangMengDedup
from .base import BaseProvider, ProviderCapabilities

logger = logging.getLogger("lol_presence.zhangmeng.provider")


class ZhangMengNotifyProvider(BaseProvider):
    """掌盟通知驱动的 Presence Provider。

    与 LCU/SGP 的轮询模式不同，本 Provider 是被动推送模式：
    - Webhook 层收到 SmsForwarder 转发的掌盟通知后，调用 handle_notification()
    - 本 Provider 解析通知 → 查找绑定 → 创建 PlayerPresence → _emit() 上抛
    - PresenceService 状态机推导事件 → NotificationService 发送群提醒
    """

    name = "zhangmeng_notify"
    capabilities = ProviderCapabilities(
        qq_to_lol_binding=False,
        summoner_lookup=False,
        presence=True,
        presence_granularity="coarse",  # 只有 OFFLINE / ONLINE / IN_GAME 三态
        match_history=False,
        ranked=False,
        needs_watch_account=False,
        needs_local_client=False,
    )

    def __init__(
        self,
        db,  # storage.database.Database（延迟类型标注避免循环 import）
        patterns_path: str,
        default_cooldown_minutes: int = 10,
    ):
        super().__init__(poll_interval=0)  # 不使用轮询
        self.db = db
        self.patterns_path = patterns_path
        self.rules: list[dict[str, Any]] = []
        self.fallback: dict[str, Any] = {}
        self.dedup = ZhangMengDedup(default_cooldown_minutes=default_cooldown_minutes)
        self._load_patterns()

    # ---------- 配置加载 ----------

    def _load_patterns(self) -> None:
        """从 JSON 文件加载解析规则。文件不存在时用空规则（只记录日志，不产生事件）。"""
        if not os.path.exists(self.patterns_path):
            logger.warning(
                "[zhangmeng] 解析规则文件不存在: %s，将只记录日志不产生事件",
                self.patterns_path,
            )
            self.rules = []
            self.fallback = {}
            return
        try:
            with open(self.patterns_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.rules = [r for r in data.get("rules", []) if r.get("enabled", True)]
            self.fallback = data.get("fallback", {})
            logger.info("[zhangmeng] 已加载 %d 条解析规则", len(self.rules))
        except Exception as e:
            logger.error("[zhangmeng] 加载解析规则失败: %s", e)
            self.rules = []
            self.fallback = {}

    def reload_patterns(self) -> None:
        """重新加载解析规则（运行时可调用）。"""
        self._load_patterns()

    # ---------- 通知解析 ----------

    def _match_rules(self, text: str) -> Optional[dict[str, Any]]:
        """按规则顺序匹配文本，命中即停。

        Returns:
            匹配结果 dict，包含 name、state、rule_name、cooldown_minutes；未命中返回 None。
        """
        if not text:
            return None
        for rule in self.rules:
            patterns = rule.get("patterns", [])
            name_field = rule.get("name_field", "name")
            state = rule.get("state", "ONLINE")
            cooldown = rule.get("cooldown_minutes")
            for pattern in patterns:
                try:
                    match = re.search(pattern, text)
                except re.error as e:
                    logger.warning(
                        "[zhangmeng] 规则 %s 的正则无效: %s, error=%s",
                        rule.get("name"),
                        pattern,
                        e,
                    )
                    continue
                if match:
                    name = (
                        match.group(name_field)
                        if name_field in match.groupdict()
                        else ""
                    )
                    if not name:
                        # 尝试用第一个捕获组
                        groups = match.groups()
                        name = groups[0] if groups else ""
                    if name:
                        return {
                            "name": name.strip(),
                            "state": state.upper(),
                            "rule_name": rule.get("name", "unknown"),
                            "cooldown_minutes": cooldown,
                            "matched_pattern": pattern,
                        }
        return None

    def _fallback_classify(self, text: str) -> Optional[str]:
        """兜底关键词分类（只用于日志标记，不产生事件）。"""
        if not text:
            return None
        online_kw = self.fallback.get("online_keywords", [])
        in_game_kw = self.fallback.get("in_game_keywords", [])
        offline_kw = self.fallback.get("offline_keywords", [])
        for kw in online_kw:
            if kw in text:
                return "ONLINE"
        for kw in in_game_kw:
            if kw in text:
                return "IN_GAME"
        for kw in offline_kw:
            if kw in text:
                return "OFFLINE"
        return None

    # ---------- 核心：处理单条通知 ----------

    async def handle_notification(self, notif) -> dict[str, Any]:
        """处理单条掌盟通知。

        Args:
            notif: ZhangMengNotification（webhook.zhangmeng 中定义）

        Returns:
            处理结果 dict，包含 parsed、event、player、rule_name 等字段。
        """
        # 拼接 title + content 作为解析文本
        text_parts = []
        if notif.title:
            text_parts.append(notif.title)
        if notif.content:
            text_parts.append(notif.content)
        text = "\n".join(text_parts).strip()

        if not text:
            logger.info("[zhangmeng] 通知内容为空，跳过解析")
            return {"parsed": False, "reason": "empty_text"}

        # 1. 规则匹配
        match_result = self._match_rules(text)
        if match_result is None:
            # 兜底分类（只用于日志）
            fallback_state = self._fallback_classify(text)
            logger.info(
                "[zhangmeng] 未匹配到规则，兜底分类=%s, text=%s",
                fallback_state,
                text[:100],
            )
            return {
                "parsed": False,
                "reason": "no_rule_matched",
                "fallback_state": fallback_state,
                "text_preview": text[:200],
            }

        name = match_result["name"]
        state_str = match_result["state"]
        rule_name = match_result["rule_name"]
        cooldown_minutes = match_result.get("cooldown_minutes")

        logger.info(
            "[zhangmeng] 匹配规则=%s, 玩家=%s, 状态=%s, text=%s",
            rule_name,
            name,
            state_str,
            text[:100],
        )

        # 2. 去重检查（Provider 层）
        if not self.dedup.should_notify(
            name, state_str, cooldown_minutes=cooldown_minutes
        ):
            logger.info(
                "[zhangmeng] 玩家 %s 的 %s 事件在冷却期内，跳过", name, state_str
            )
            return {
                "parsed": True,
                "deduped": True,
                "player": name,
                "state": state_str,
                "rule_name": rule_name,
            }

        # 3. 查找绑定关系（昵称 → puuid）
        binding = None
        try:
            binding = await self.db.find_binding_by_name(name)
        except Exception as e:
            logger.warning("[zhangmeng] 查找绑定失败 name=%s: %s", name, e)

        if binding is None:
            logger.info("[zhangmeng] 玩家 %s 未绑定 LOL 账号，跳过提醒（仅记录）", name)
            # 未绑定的玩家不标记去重（绑定后可以立即提醒）
            return {
                "parsed": True,
                "bound": False,
                "player": name,
                "state": state_str,
                "rule_name": rule_name,
            }

        # 4. 创建 PlayerPresence 并上抛
        state = self._map_state(state_str)
        presence = PlayerPresence(
            puuid=binding.puuid,
            state=state,
            qq_id=binding.qq_id,
            game_name=binding.game_name or name,
            tag_line=binding.tag_line or "",
            platform=binding.platform or "tencent",
            raw_game_status=f"zhangmeng:{rule_name}",
            updated_at=time.time(),
        )

        try:
            await self._emit([presence])
            self.dedup.mark_sent(name, state_str)
            logger.info(
                "[zhangmeng] 已上抛 presence: puuid=%s, name=%s, state=%s",
                binding.puuid[:8],
                name,
                state.value,
            )
        except Exception as e:
            logger.error("[zhangmeng] 上抛 presence 失败: %s", e)
            return {"parsed": True, "error": str(e), "player": name}

        return {
            "parsed": True,
            "bound": True,
            "player": name,
            "puuid": binding.puuid,
            "qq_id": binding.qq_id,
            "state": state.value,
            "rule_name": rule_name,
            "emitted": True,
        }

    @staticmethod
    def _map_state(state_str: str) -> PresenceState:
        """将规则中的状态字符串映射为 PresenceState。"""
        mapping = {
            "ONLINE": PresenceState.ONLINE,
            "IN_GAME": PresenceState.IN_GAME,
            "OFFLINE": PresenceState.OFFLINE,
            "LOBBY": PresenceState.LOBBY,
            "MATCHMAKING": PresenceState.MATCHMAKING,
            "CHAMP_SELECT": PresenceState.CHAMP_SELECT,
        }
        return mapping.get(state_str.upper(), PresenceState.UNKNOWN)

    # ---------- BaseProvider 抽象方法实现 ----------

    async def snapshot(self) -> list[PlayerPresence]:
        """被动推送模式不需要全量快照，返回空列表。

        PresenceService 启动时会调用一次 snapshot 作为初始状态。
        掌盟通知是事件驱动的，没有全量状态，所以返回空列表。
        这意味着：插件重启后，第一个上线通知会被当作"首次见到"，只登记不广播（PresenceService 的逻辑）。
        这是正确的行为——避免重启刷屏。
        """
        return []

    async def resolve_player(
        self, query: str, qq_id: str = ""
    ) -> PlayerPresence | None:
        """通过昵称解析玩家。查询失败返回 None。"""
        try:
            binding = await self.db.find_binding_by_name(query)
            if binding is None:
                return None
            return PlayerPresence(
                puuid=binding.puuid,
                state=PresenceState.UNKNOWN,
                qq_id=binding.qq_id,
                game_name=binding.game_name or query,
                tag_line=binding.tag_line or "",
                platform=binding.platform or "tencent",
            )
        except Exception as e:
            logger.warning("[zhangmeng] resolve_player 失败 query=%s: %s", query, e)
            return None

    async def health_check(self) -> bool:
        """掌盟 Provider 只要初始化成功就可用（Webhook 由插件主入口注册）。"""
        return True
