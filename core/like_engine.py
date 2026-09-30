"""点赞引擎 —— 决定一条新动态会收到谁的赞。

设计要点：
- 这里只做「概率 + 挑人」这两件纯逻辑，落库与排期交给 main.py，
  所以不依赖 AstrBot 运行时，可以单独跑测试；
- NPC 名单直接复用 NpcEngine 的名单，不另维护一份；
- 点赞不调用 LLM：它是个轻动作，不该为此多花一次模型调用。
"""
from __future__ import annotations

import random
from typing import Optional


class LikeEngine:
    """按配置决定他（AI）和 NPC 要不要给某条动态点赞。"""

    def __init__(self, context=None, config: Optional[dict] = None, npc_engine=None):
        self.context = context
        self.config = config or {}
        self.npc_engine = npc_engine

    # ------------------------------------------------------------------
    # 开关与概率
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        return bool(self.config.get("like_enabled", True))

    def ai_should_like(self, post_author: str) -> bool:
        """他给不给这条动态点赞。

        只赞对方发的动态，不给自己点赞——自己赞自己一眼假。
        """
        if not self.enabled() or post_author != "user":
            return False
        return self._roll(self._float("ai_like_probability", 0.6))

    def npc_picks(self, exclude: Optional[set] = None) -> list[str]:
        """这次来点赞的 NPC 名单：先掷一次概率，再随机挑 0~npc_like_count_max 人。

        exclude 里的名字优先避开（正在冷却的人不重复刷存在感）；
        避开之后没人了就把名单放宽，宁可来一个人也不留空。
        """
        if not self.enabled() or not self.npc_engine:
            return []
        if not self._roll(self._float("npc_like_probability", 0.5)):
            return []

        limit = self._int("npc_like_count_max", 2, minimum=0)
        if limit <= 0:
            return []

        exclude = exclude or set()
        all_names = [n["name"] for n in self.npc_engine.parse_list()]
        pool = [name for name in all_names if name not in exclude] or all_names
        if not pool:
            return []
        return random.sample(pool, min(limit, len(pool)))

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _roll(probability: float) -> bool:
        return random.random() < probability

    def _float(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default) or 0)
        except (TypeError, ValueError):
            value = default
        return max(0.0, min(1.0, value))

    def _int(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            value = int(float(self.config.get(key, default) or default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)
