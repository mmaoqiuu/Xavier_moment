"""聊天上下文桥（单向：聊天 → 朋友圈）。

让他在朋友圈开口前，先看一眼你们最近的私聊，避免前言不搭后语。

- 只在他要发帖 / 评论 / 回复评论时取一次，不参与每轮对话，开销极低
- 只取最近若干条，总字数封顶
- 会话太久没动静就不再取，避免翻出陈年旧话当成"刚刚"
- NPC 侧完全不读取这里的内容
- 他刚在私聊里说过的话会单独列出来，并要求朋友圈发言不要重复（见 repeat_hit）
"""
from __future__ import annotations

import json
import re
import time

from astrbot.api import logger

from .npc_engine import display_name


def _plain(text: str) -> str:
    """比对用：只留中文、字母、数字，忽略标点和空白。"""
    return re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]+", "", text or "")


def _ngrams(text: str, n: int = 2) -> set:
    clean = _plain(text)
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def text_overlap(a: str, b: str) -> float:
    """两句话的字符 2-gram 重合度（0~1，Dice 系数）。

    比 Jaccard 宽松一点：一句话的意思套在另一句里时更容易被抓出来，
    正是去重想拦的情况；完全不相干的两句仍然接近 0。
    中文短句用这个够了，不必为此再叫一次模型。
    """
    left, right = _ngrams(a), _ngrams(b)
    if not left or not right:
        return 0.0
    return 2 * len(left & right) / (len(left) + len(right))


def find_repeat(text: str, said: list, threshold: float = 0.6, min_chars: int = 8) -> str:
    """在「他说过的话」里找出与 text 最像、且超过阈值的那句；没有就返回空串。"""
    if len(_plain(text)) < min_chars:
        return ""
    best, best_score = "", 0.0
    for line in said or []:
        if len(_plain(line)) < min_chars:
            continue
        score = text_overlap(text, line)
        if score > best_score:
            best, best_score = line, score
    return best if best and best_score >= threshold else ""


class ChatBridge:
    """私聊 → 朋友圈 的上下文取数器。"""

    SESSION_KEY = "chat_bridge_session"
    PER_MESSAGE_LIMIT = 200
    DEDUP_SAID_LIMIT = 4

    def __init__(self, context, config: dict, db):
        self.context = context
        self.config = config
        self.db = db
        self._session_cache = ""
        # 最近一次取数时「他说过的话」，供生成侧做重复检测（见 repeat_hit）
        self._said_snapshot: list = []

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

    def _float(self, key: str, default: float) -> float:
        try:
            value = self.config.get(key, default)
            return float(value) if value not in (None, "") else default
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

    async def resolve_session(self) -> str:
        """对外只读接口：当前取上下文用的会话（评论区回灌要拿它比对）。"""
        return await self._resolve_session()

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

        picked = self._pick_messages(messages)
        if not picked:
            return ""

        # 他说过的话留一份快照，生成评论后用它做重复检测
        self._said_snapshot = [text for role, text in picked if role == "assistant"]

        block = "\n\n【你们最近的私聊】\n" + "\n".join(self._format_lines(picked))

        said = self._dedup_said()
        if said:
            block += (
                "\n\n【你刚在私聊里已经说过的话（这些别再在朋友圈说一遍）】\n"
                + "\n".join(f"- {line}" for line in said)
                + "\n朋友圈里换个角度、换个说法，或者只回应这条动态带来的新信息。"
            )

        return (
            block
            + "\n【注意】以上是你们私聊里刚发生的事。你在朋友圈要说的话必须和它保持一致，"
            "不要编造与最近对话冲突的当下状态（例如刚说过在回家路上，就不要说自己刚睡醒）。"
        )

    def _pick_messages(self, messages: list) -> list:
        """按条数、字数上限挑出最近几轮对话，返回 (role, text) 列表。"""
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
        return picked

    def _format_lines(self, picked: list) -> list:
        """把 (role, text) 渲染成「谁：说了什么」。"""
        lines = []
        for role, text in picked:
            who = display_name(self.config, "ai" if role == "assistant" else "user", "")
            if not who:
                who = "他" if role == "assistant" else "你"
            lines.append(f"{who}：{text}")
        return lines

    def _dedup_said(self) -> list:
        """拎出「他刚在私聊里说过、值得提醒别重复」的话；开关关掉时返回空。"""
        if not self._bool("chat_bridge_dedup_enabled", True):
            return []
        min_chars = max(4, self._int("chat_bridge_dedup_min_chars", 8))
        seen, out = set(), []
        for text in self._said_snapshot:
            clean = _plain(text)
            if len(clean) < min_chars or clean in seen:
                continue
            seen.add(clean)
            out.append(text)
        return out[-self.DEDUP_SAID_LIMIT:]

    def repeat_hit(self, text: str) -> str:
        """这段草稿是不是在重复他刚在私聊里说过的话？命中则返回那句原话。"""
        if not self._bool("chat_bridge_dedup_enabled", True):
            return ""
        threshold = self._float("chat_bridge_dedup_threshold", 0.6)
        min_chars = max(4, self._int("chat_bridge_dedup_min_chars", 8))
        return find_repeat(text, self._said_snapshot, threshold, min_chars)

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
