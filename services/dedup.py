"""掌盟通知去重服务。

同一玩家的同一类型事件在 cooldown 窗口内只提醒一次。
基于玩家昵称（name）+ 事件类型（state）去重，不依赖 puuid（因为掌盟通知可能没有 puuid）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DedupRecord:
    """单条去重记录。"""

    name: str
    state: str
    last_sent_at: float = 0.0


class ZhangMengDedup:
    """掌盟通知去重器。

    用法：
        dedup = ZhangMengDedup(default_cooldown_minutes=10)
        if dedup.should_notify("张三", "ONLINE", cooldown_minutes=10):
            ... 发送提醒 ...
            dedup.mark_sent("张三", "ONLINE")
    """

    def __init__(self, default_cooldown_minutes: int = 10):
        self.default_cooldown = max(0, default_cooldown_minutes) * 60
        # key: f"{name.lower()}:{state}" -> DedupRecord
        self._records: dict[str, DedupRecord] = {}

    def _key(self, name: str, state: str) -> str:
        return f"{name.strip().lower()}:{state.upper()}"

    def should_notify(
        self,
        name: str,
        state: str,
        cooldown_minutes: Optional[int] = None,
    ) -> bool:
        """判断是否应该发送提醒。

        Args:
            name: 玩家昵称
            state: 状态（ONLINE / IN_GAME / OFFLINE）
            cooldown_minutes: 本次使用的冷却时间（分钟），None 则用默认值。
                              0 表示不冷却，每次都提醒。

        Returns:
            True 表示应该发送，False 表示在冷却期内应跳过。
        """
        if not name or not state:
            return False
        cooldown = (
            cooldown_minutes * 60
            if cooldown_minutes is not None
            else self.default_cooldown
        )
        if cooldown <= 0:
            return True

        key = self._key(name, state)
        record = self._records.get(key)
        if record is None:
            return True
        elapsed = time.time() - record.last_sent_at
        return elapsed >= cooldown

    def mark_sent(self, name: str, state: str) -> None:
        """标记已发送提醒。"""
        if not name or not state:
            return
        key = self._key(name, state)
        self._records[key] = DedupRecord(
            name=name.strip(),
            state=state.upper(),
            last_sent_at=time.time(),
        )

    def clear(self, name: Optional[str] = None, state: Optional[str] = None) -> int:
        """清空调重记录。返回清除的条数。"""
        if name is None and state is None:
            count = len(self._records)
            self._records.clear()
            return count
        removed = 0
        for key in list(self._records.keys()):
            rec = self._records[key]
            if name and rec.name.strip().lower() != name.strip().lower():
                continue
            if state and rec.state.upper() != state.upper():
                continue
            del self._records[key]
            removed += 1
        return removed

    def stats(self) -> dict[str, int]:
        """返回去重器统计。"""
        return {"tracked_players": len(self._records)}
