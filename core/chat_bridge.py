"""聊天上下文桥（单向：聊天 → 朋友圈）。

让他在朋友圈开口前，先看一眼你们最近的私聊，避免前言不搭后语。

- 只在他要发帖 / 评论 / 回复评论时取一次，不参与每轮对话，开销极低
- 只取最近若干条，总字数封顶
- 会话太久没动静就不再取，避免翻出陈年旧话当成"刚刚"
- NPC 侧完全不读取这里的内容
"""
from __future__ import annotations

import json
import time

from astrbot.api import logger

from .npc_engine import display_name


class ChatBridge:
    """私聊 → 朋友圈 的上下文取数器。"""

    SESSION_KEY = "chat_bridge_session"
    PER_MESSAGE_LIMIT = 200

    def __init__(self, context, config: dict, db):
        self.context = context
        self.config = config
        self.db = db
        self._session_cache = ""

    # ------------------------------------------------------------------
    # 开关与配置
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        return self._bool("chat_bridge_enabled", True)

    def _bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)

    def _int(self, key: str, default: int) -> int:
        try:
            return int(float(self.config.get(key, default) or default))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # 会话记忆
    # ------------------------------------------------------------------

    async def remember_session(self, session_id: str) -> None:
        """记下最近一次私聊会话，供朋友圈侧取用（落盘，重启不丢）。"""
        if not session_id or not self.db or session_id == self._session_cache:
            return
        self._session_cache = session_id
        try:
            await self.db.set_setting(self.SESSION_KEY, session_id)
        except Exception:
            logger.exception("[moment] 记录私聊会话失败")

    async def _resolve_session(self) -> str:
        session = str(self.config.get("chat_bridge_session", "") or "").strip()
        if session:
            return session
        if self._session_cache:
            return self._session_cache
        try:
            session = await self.db.get_setting(self.SESSION_KEY, "")
        except Exception:
            session = ""
        return str(session or "").strip()

    # ------------------------------------------------------------------
    # 取数
    # ------------------------------------------------------------------

    async def collect_context(self) -> str:
        """取最近的私聊，拼成一段可注入的文本；取不到时返回空串。"""
        if not self.enabled() or not self.db:
            return ""
        try:
            return await self._collect()
        except Exception:
            logger.exception("[moment] 取私聊上下文失败")
            return ""

    async def _collect(self) -> str:
        manager = getattr(self.context, "conversation_manager", None)
        if manager is None:
            return ""
        session = await self._resolve_session()
        if not session:
            return ""

        conversation_id = await manager.get_curr_conversation_id(session)
        if not conversation_id:
            return ""
        conversation = await manager.get_conversation(session, conversation_id)
        if not conversation:
            return ""

        # 会话太久没动静就不再取，避免把几小时前的旧话当成刚刚说的
        stale_minutes = self._int("chat_bridge_stale_minutes", 180)
        updated_at = getattr(conversation, "updated_at", 0) or 0
        if stale_minutes > 0 and updated_at:
            if time.time() - float(updated_at) > stale_minutes * 60:
                return ""

        history = getattr(conversation, "history", "") or "[]"
        try:
            messages = json.loads(history) if isinstance(history, str) else history
        except (TypeError, ValueError):
            logger.warning("[moment] 私聊历史解析失败，本次不注入")
            return ""
        if not isinstance(messages, list):
            return ""

        lines = self._format_messages(messages)
        if not lines:
            return ""

        return (
            "\n\n【你们最近的私聊】\n"
            + "\n".join(lines)
            + "\n【注意】以上是你们私聊里刚发生的事。你在朋友圈要说的话必须和它保持一致，"
            "不要编造与最近对话冲突的当下状态（例如刚说过在回家路上，就不要说自己刚睡醒）。"
        )

    def _format_messages(self, messages: list) -> list:
        """按条数、字数上限挑出最近几轮对话，返回带说话人的文本行。"""
        max_messages = max(1, min(30, self._int("chat_bridge_max_messages", 8)))
        max_chars = max(100, self._int("chat_bridge_max_chars", 1200))

        picked: list[tuple[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = self._extract_text(message.get("content"))
            if not text:
                continue
            if len(text) > self.PER_MESSAGE_LIMIT:
                text = text[: self.PER_MESSAGE_LIMIT] + "…"
            picked.append((role, text))

        picked = picked[-max_messages:]

        # 总字数封顶：超出时从最早的一条开始丢
        while picked and sum(len(text) for _, text in picked) > max_chars:
            picked.pop(0)

        lines = []
        for role, text in picked:
            who = display_name(self.config, "ai" if role == "assistant" else "user", "")
            if not who:
                who = "他" if role == "assistant" else "你"
            lines.append(f"{who}：{text}")
        return lines

    @staticmethod
    def _extract_text(content) -> str:
        """从 OpenAI 格式的消息内容里取纯文本；图片等非文本部分直接跳过。"""
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(p for p in parts if p).strip()
        return ""
