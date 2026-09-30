"""NPC 评论引擎 —— 让朋友圈里的其他角色也能出现在评论区。

设计要点：
- 名单与人设全部来自配置，代码里不写死任何角色；
- LLM 调用与文本清洗复用 PostEngine，不重复实现；
- 名单解析、挑人、解析模型返回这三段是纯逻辑（parse_list / pick / parse_output），
  不依赖 AstrBot 运行时，便于单独测试。
"""
from __future__ import annotations

import json
import random
import re
from typing import Optional

from astrbot.api import logger


def display_name(config: dict, author: str, author_name: str = "") -> str:
    """把 author 字段翻译成页面上的名字（帖子和评论共用同一套规则）。"""
    if author == "npc":
        return author_name or "朋友"
    if author == "ai":
        return (config.get("ai_name") or "").strip() or "他"
    return (config.get("user_name") or "").strip() or "我"


class NpcEngine:
    """按配置挑 NPC、生成评论内容。"""

    def __init__(self, context, config: dict, post_engine=None, material=None):
        self.context = context
        self.config = config
        self.post_engine = post_engine
        self.material = material
        self._list_cache_key: Optional[str] = None
        self._list_cache: list[dict] = []

    # ------------------------------------------------------------------
    # 名字与人设
    # ------------------------------------------------------------------

    def parse_list(self, raw: Optional[str] = None) -> list[dict]:
        """解析配置里的 NPC 名单，格式：每行「名字|人设」，行首的「-」会被忽略。

        解析结果按原始配置文本缓存，配置改了自动重建；坏行跳过并记一条 warning，
        不因为一行写错就让整个功能失效。
        """
        text = (self.config.get("npc_list", "") if raw is None else raw) or ""
        if text == self._list_cache_key:
            return self._list_cache

        result: list[dict] = []
        seen: set[str] = set()
        for line in text.splitlines():
            line = line.strip().lstrip("-").strip()
            if not line:
                continue
            if "|" not in line:
                logger.warning(f"[moment] NPC 名单缺少「|」分隔符，已跳过: {line[:20]}")
                continue
            name, _, persona = line.partition("|")
            name, persona = name.strip(), persona.strip()
            if not name or not persona:
                continue
            if name in seen:
                continue
            seen.add(name)
            result.append({"name": name, "persona": persona})

        self._list_cache_key = text
        self._list_cache = result
        return result

    def names(self) -> list[str]:
        return [n["name"] for n in self.parse_list()]

    def by_name(self, name: str) -> Optional[dict]:
        for npc in self.parse_list():
            if npc["name"] == name:
                return npc
        return None

    def enabled(self) -> bool:
        return bool(self.config.get("npc_enabled", True)) and bool(self.parse_list())

    # ------------------------------------------------------------------
    # 挑人
    # ------------------------------------------------------------------

    def pick(self, count: int, exclude: Optional[set] = None) -> list[dict]:
        """随机挑 count 个 NPC。exclude 里的名字优先避开；
        避开之后不够数就放宽（宁可多来一个人，也不要让评论区空着）。"""
        exclude = exclude or set()
        pool = [n for n in self.parse_list() if n["name"] not in exclude]
        if len(pool) < count:
            pool = self.parse_list()
        count = max(0, min(int(count), len(pool)))
        return random.sample(pool, count) if count else []

    def plan_batch(self, exclude: Optional[set] = None) -> list[dict]:
        """决定这条动态要叫几个 NPC 来评论。"""
        if not self.enabled():
            return []
        if random.random() > self._float("npc_comment_probability", 1.0):
            return []
        lo = self._int("npc_count_min", 1, minimum=1)
        hi = max(lo, self._int("npc_count_max", 3, minimum=1))
        return self.pick(random.randint(lo, hi), exclude=exclude)

    # ------------------------------------------------------------------
    # 生成评论
    # ------------------------------------------------------------------

    async def generate(
        self,
        post: dict,
        comments: list[dict],
        npc: dict,
        force_target: Optional[dict] = None,
    ) -> Optional[dict]:
        """让某个 NPC 写一条评论。

        Args:
            post: 帖子 dict
            comments: 该帖子下已有的评论（时间正序）
            npc: {"name": ..., "persona": ...}
            force_target: 指定要回复的那条评论（用户回复了 NPC，或 NPC 被点着接话时用）

        Returns:
            {"content": str, "parent_comment_id": int | None}；生成失败返回 None
        """
        if not self.post_engine:
            return None
        try:
            persona = await self._persona()
            post_author = display_name(self.config, post.get("author", ""), post.get("author_name", ""))
            body = (post.get("content") or "").strip().replace("\n", " ")[:200]

            # 别人的评论（不把自己之前的评论当作「要接的话」，避免自说自话）
            others = [
                c for c in comments
                if not (c.get("author") == "npc" and (c.get("author_name") or "") == npc["name"])
            ]

            target = force_target
            if target is None and others and random.random() < self._float("npc_chain_probability", 0.5):
                target = random.choice(others)

            lines = [
                f"你是「{npc['name']}」。你的人设：{npc['persona']}",
                "",
                "你现在在刷朋友圈，看到这样一条动态：",
                f"【{post_author}】{body}",
            ]
            if others:
                lines += ["", "这条动态下已有的评论（按时间顺序，最后一条最新）："]
                for c in others[-8:]:
                    who = display_name(self.config, c.get("author", ""), c.get("author_name", ""))
                    text = (c.get("content") or "").strip().replace("\n", " ")[:60]
                    lines.append(f"- {who}：{text}")

            if target is not None:
                target_name = display_name(self.config, target.get("author", ""), target.get("author_name", ""))
                target_text = (target.get("content") or "").strip()[:60]
                lines += [
                    "",
                    f"请你接着 {target_name} 的这句「{target_text}」说一句——是评论区里接话，不是单纯评论这条动态。",
                    f'输出的 "reply_to" 必须正好是「{target_name}」。',
                ]
            else:
                lines += [
                    "",
                    "请写一条你自己的评论。",
                    '输出的 "reply_to" 填空字符串 ""。',
                ]

            lines += [
                "",
                "要求：",
                '- 只输出一个 JSON 对象，形如 {"reply_to": "", "content": "……"}，不要输出任何解释或多余文字',
                "- content 是一句话，15~25 字以内，口语化，符合你的人设、说话习惯，以及你和发言者的交情远近",
                "- 绝对不要换行，不要自我介绍，不要用第三人称称呼自己",
                "- 不要复述动态原文，不要说「作为朋友」这类旁白",
                "- 拿不准的事就别提，不要编造具体的作品名、地名、人名",
            ]
            if not self.config.get("post_with_emoji", True):
                lines.append("- 不要使用 emoji")

            raw = await self.post_engine._call_llm("\n".join(lines), system_prompt=persona)
            if not raw:
                return None

            parsed = self.parse_output(raw)
            content = self._tidy(parsed.get("content", ""))
            if len(content) < 2:
                return None

            parent_id = None
            if target is not None:
                reply_to = (parsed.get("reply_to") or "").strip()
                target_name = display_name(self.config, target.get("author", ""), target.get("author_name", ""))
                if not reply_to or reply_to in target_name or target_name in reply_to:
                    parent_id = target.get("id")
                else:
                    logger.debug(f"[moment] {npc['name']} 的 reply_to（{reply_to}）对不上目标，按顶层评论处理")

            return {"content": content, "parent_comment_id": parent_id}

        except Exception:
            logger.exception("[moment] NPC 评论生成失败")
            return None

    @staticmethod
    def parse_output(raw: str) -> dict:
        """解析模型返回。优先按 JSON 解析；失败时退化成「整段当评论内容」。

        模型偶尔会把 JSON 包在 ``` 里或前后带一句解释，这里都尽量捞出来，
        捞不到也不报错——直接当纯文本用，总比这条评论丢掉好。
        """
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            try:
                data = json.loads(match.group(0))
                if isinstance(data, dict):
                    return {
                        "reply_to": str(data.get("reply_to") or "").strip(),
                        "content": str(data.get("content") or "").strip(),
                    }
            except Exception:
                pass
        return {"reply_to": "", "content": text}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    async def _persona(self) -> str:
        if self.material:
            try:
                return await self.material.get_persona()
            except Exception:
                logger.exception("[moment] 读取人设失败，NPC 评论将不带人设")
        return ""

    def _tidy(self, text: str) -> str:
        """收敛模型输出：去引号、去换行、去「回复某某：」前缀、按字数上限截断。"""
        text = (text or "").strip()
        if not text:
            return ""
        try:
            if self.post_engine is not None:
                text = self.post_engine._clean_llm_output(text)
        except Exception:
            pass
        text = text.replace("\r", " ").replace("\n", " ").strip()
        text = re.sub(r'^[「"\'“”]+|[」"\'“”]+$', "", text).strip()
        text = re.sub(r"^回复\s*@?[\u4e00-\u9fa5A-Za-z0-9]{1,6}\s*[：:]\s*", "", text)
        limit = self._int("npc_max_length", 30, minimum=6)
        if len(text) > limit:
            text = text[:limit].rstrip("，,。.、；;：: ")
        return text.strip()

    def _int(self, key: str, default: int, minimum: Optional[int] = None) -> int:
        try:
            value = int(float(self.config.get(key, default) or default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _float(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default) or 0)
        except (TypeError, ValueError):
            value = default
        if key.endswith("probability"):
            return max(0.0, min(1.0, value))
        return value
