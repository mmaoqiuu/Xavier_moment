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

    def npc_pick_plan(self, exclude: Optional[set] = None, force: bool = False) -> dict:
        """这次来点赞的完整结果：掷一次概率，命中后随机挑 1~npc_like_count_max 人。

        返回 {"hit": 有没有命中, "names": [...], "probability": 概率,
              "pool_size": 候选人数, "forced": 是不是保底硬给的}。
        把「有没有命中」一并交出去，调用方才能写进日志——否则页面上一片空白时，
        分不清是这次没抽中，还是功能坏了。

        exclude 里的名字优先避开（正在冷却的人不重复刷存在感）；
        避开之后没人了就把名单放宽，宁可来一个人也不留空。

        force=True 时跳过概率这一掷，直接挑人。这是保底用的：连着好几条动态
        都没人赞之后，下一条无论如何都要来一个，免得骰子一直空转。
        """
        probability = self._float("npc_like_probability", 0.5)
        plan = {
            "hit": False,
            "names": [],
            "probability": probability,
            "pool_size": 0,
            "forced": bool(force),
        }
        if not self.enabled() or not self.npc_engine:
            return plan

        limit = self._int("npc_like_count_max", 2, minimum=0)
        exclude = exclude or set()
        all_names = [n["name"] for n in self.npc_engine.parse_list()]
        pool = [name for name in all_names if name not in exclude] or all_names
        plan["pool_size"] = len(pool)
        if limit <= 0 or not pool:
            # 没人可挑，保底也无从谈起
            plan["forced"] = False
            return plan

        if not force and not self._roll(probability):
            return plan

        # 人数也随机：每次都整整齐齐来同一批人，一眼就假
        count = random.randint(1, min(limit, len(pool)))
        plan["hit"] = True
        plan["names"] = random.sample(pool, count)
        return plan

    def npc_picks(self, exclude: Optional[set] = None) -> list[str]:
        """兼容旧调用：只要名单，不要抽签详情。"""
        return self.npc_pick_plan(exclude=exclude)["names"]

    # ------------------------------------------------------------------
    # 保底：连着几条没人赞，就强制来一次
    # ------------------------------------------------------------------

    def pity_limit(self) -> int:
        """连着多少条动态没有 NPC 点赞后，下一条强制来一次。0 = 关闭保底。"""
        return self._int("npc_like_pity", 2, minimum=0)

    def should_force(self, streak: int) -> bool:
        """按「已连续落空多少条」判断这一条要不要走保底。"""
        limit = self.pity_limit()
        return limit > 0 and streak >= limit

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _roll(probability: float) -> bool:
        return random.random() < probability

    def _float(self, key: str, default: float) -> float:
        raw = self.config.get(key, None)
        if raw is None or raw == "":
            raw = default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
        return max(0.0, min(1.0, value))

    def _int(self, key: str, default: int, minimum: int = 0) -> int:
        raw = self.config.get(key, None)
        if raw is None or raw == "":
            raw = default
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)
