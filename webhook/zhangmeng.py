"""掌盟 Webhook 接收端。

SmsForwarder → HTTP POST → 本模块 → 完整记录原始请求 → 调用 Provider 解析。

第一阶段原则：
- 不假设掌盟通知的具体字段格式，完整记录收到的一切（package/from/title/content/timestamp/receive_time/raw_body）
- 只接受包名 com.tencent.qt.qtl 的通知
- 支持 Bearer Token 鉴权
- 未识别的通知写入 JSONL 日志，方便后续根据真实样本补 Parser
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from astrbot.api.web import request as web_request

logger = logging.getLogger("lol_presence.zhangmeng.webhook")

# 掌盟包名
ZHANGMENG_PACKAGE = "com.tencent.qt.qtl"

# SmsForwarder 常见字段名（仅用于提取，不做格式假设）
_KNOWN_FIELDS = {
    "package": ["package", "pkg", "app_package", "from"],
    "title": ["title", "notification_title", "notify_title"],
    "content": ["content", "text", "message", "msg", "notification_content", "body"],
    "timestamp": ["time", "timestamp", "date", "send_time", "when"],
    "from": ["from", "sender", "app_name", "device_name"],
}


class ZhangMengNotification:
    """归一化后的掌盟通知。"""

    def __init__(self):
        self.package: str = ""
        self.from_field: str = ""
        self.title: str = ""
        self.content: str = ""
        self.timestamp: Optional[float] = None
        self.receive_time: float = 0.0
        self.raw_body: dict[str, Any] | None = None
        self.raw_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "from": self.from_field,
            "title": self.title,
            "content": self.content,
            "timestamp": self.timestamp,
            "receive_time": self.receive_time,
            "raw_body": self.raw_body,
            "raw_text": self.raw_text,
        }


def _extract_field(body: dict[str, Any], candidates: list[str]) -> str:
    """从 body 中按候选字段名提取第一个非空值。"""
    for key in candidates:
        val = body.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _extract_timestamp(body: dict[str, Any]) -> Optional[float]:
    """提取时间戳，兼容秒级和毫秒级。"""
    for key in _KNOWN_FIELDS["timestamp"]:
        val = body.get(key)
        if val is None:
            continue
        try:
            ts = float(val)
            if ts > 1e12:  # 毫秒级
                ts = ts / 1000.0
            return ts
        except (ValueError, TypeError):
            continue
    return None


def parse_notification(body: dict[str, Any], raw_text: str) -> ZhangMengNotification:
    """从 SmsForwarder Webhook body 中提取归一化通知。

    不做语义解析，只做字段提取和归一化。语义解析由 Provider 层的可配置规则完成。
    """
    notif = ZhangMengNotification()
    notif.receive_time = time.time()
    notif.raw_body = body
    notif.raw_text = raw_text

    notif.package = _extract_field(body, _KNOWN_FIELDS["package"])
    notif.from_field = _extract_field(body, _KNOWN_FIELDS["from"])
    notif.title = _extract_field(body, _KNOWN_FIELDS["title"])
    notif.content = _extract_field(body, _KNOWN_FIELDS["content"])
    notif.timestamp = _extract_timestamp(body)

    return notif


def is_zhangmeng(notif: ZhangMengNotification) -> bool:
    """判断是否为掌盟通知。"""
    return notif.package == ZHANGMENG_PACKAGE


def verify_auth(headers: Any, expected_secret: str) -> bool:
    """验证 Bearer Token 鉴权。

    Args:
        headers: 请求头对象（支持 .get()）
        expected_secret: 期望的 secret（空字符串表示不鉴权）

    Returns:
        True 表示鉴权通过，False 表示拒绝。
    """
    if not expected_secret:
        return True
    auth_header = headers.get("authorization") or headers.get("Authorization") or ""
    if not auth_header:
        return False
    parts = auth_header.split(maxsplit=1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return parts[1].strip() == expected_secret.strip()


class ZhangMengWebhookLogger:
    """掌盟通知 JSONL 日志记录器。

    所有收到的通知（无论是否识别）都写入 JSONL 日志，方便后续根据真实样本补 Parser。
    """

    def __init__(self, log_dir: str, log_file: str = "zhangmeng_notifications.jsonl"):
        self.log_path = os.path.join(log_dir, log_file)
        os.makedirs(log_dir, exist_ok=True)

    def log(
        self,
        notif: ZhangMengNotification,
        status: str = "received",
        extra: dict | None = None,
    ) -> None:
        """写入一条 JSONL 记录。

        Args:
            notif: 归一化通知
            status: 处理状态（received / parsed / unparsed / rejected / error）
            extra: 额外信息（如解析结果、错误信息）
        """
        record = {
            "log_time": time.time(),
            "status": status,
            **notif.to_dict(),
        }
        if extra:
            record["extra"] = extra
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("[zhangmeng] 写入 JSONL 日志失败: %s", e)


class ZhangMengWebhookHandler:
    """掌盟 Webhook 请求处理器。

    职责：
    1. 鉴权（Bearer Token）
    2. 包名校验（只接受 com.tencent.qt.qtl）
    3. 完整记录原始请求到 JSONL
    4. 调用 Provider 进行语义解析和事件上抛
    """

    def __init__(
        self,
        provider,  # ZhangMengNotifyProvider（延迟引用，避免循环 import）
        secret: str = "",
        log_dir: str = "",
    ):
        self.provider = provider
        self.secret = secret
        self.logger = ZhangMengWebhookLogger(log_dir) if log_dir else None

    async def handle(self) -> dict[str, Any]:
        """处理 Webhook 请求。从 contextvars 中获取当前请求。

        Returns:
            JSON 响应 dict。
        """
        try:
            request = web_request
        except Exception as e:
            logger.error("[zhangmeng] 获取 request 失败: %s", e)
            return {"status": "error", "message": "internal error", "code": 500}

        # 1. 鉴权
        if not verify_auth(request.headers, self.secret):
            logger.warning("[zhangmeng] 鉴权失败，client=%s", request.client_host)
            return {"status": "error", "message": "unauthorized", "code": 401}

        # 2. 读取 body
        try:
            raw_bytes = await request.body()
            raw_text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error("[zhangmeng] 读取 body 失败: %s", e)
            return {"status": "error", "message": "failed to read body", "code": 400}

        # 尝试解析 JSON
        body: dict[str, Any] = {}
        if raw_text.strip():
            try:
                parsed = json.loads(raw_text)
                if isinstance(parsed, dict):
                    body = parsed
                else:
                    body = {"_raw": parsed}
            except json.JSONDecodeError:
                body = {"_raw_text": raw_text}

        # 3. 提取归一化通知
        notif = parse_notification(body, raw_text)

        # 4. 包名校验
        if not is_zhangmeng(notif):
            logger.info(
                "[zhangmeng] 非掌盟通知，拒绝。package=%s from=%s title=%s",
                notif.package,
                notif.from_field,
                notif.title[:50],
            )
            if self.logger:
                self.logger.log(
                    notif, status="rejected", extra={"reason": "wrong_package"}
                )
            return {
                "status": "rejected",
                "message": f"expected package {ZHANGMENG_PACKAGE}",
                "code": 200,
            }

        # 5. 记录原始通知
        if self.logger:
            self.logger.log(notif, status="received")

        # 6. 调用 Provider 解析
        result = {"parsed": False, "event": None, "player": None}
        if self.provider is not None:
            try:
                parse_result = await self.provider.handle_notification(notif)
                result.update(parse_result)
                if self.logger:
                    status = "parsed" if parse_result.get("parsed") else "unparsed"
                    self.logger.log(notif, status=status, extra=parse_result)
            except Exception as e:
                logger.exception("[zhangmeng] Provider 处理通知失败: %s", e)
                if self.logger:
                    self.logger.log(notif, status="error", extra={"error": str(e)})
                return {"status": "error", "message": "provider error", "code": 500}

        return {
            "status": "ok",
            "code": 200,
            "received": {
                "package": notif.package,
                "title": notif.title,
                "content": notif.content[:200],
                "timestamp": notif.timestamp,
            },
            "result": result,
        }
