"""三餐桥（只读 xavier_daily_meal 的 history.json）。

用途：偶尔把「今天吃了什么」当成朋友圈话题。他手里本来就有一份真实菜单，
但默认不会主动说（说多了就成了报菜名），所以这里做三件事：

1. 时段过滤：只在对应时段附近提对应那一餐，深夜不提吃。
2. 概率：按 meal_topic_ratio 抽签，命中才把菜单交给他当话题。
3. 冷却：命中后写一条 last_at，meal_topic_cooldown_hours 小时内不再提，
   免得连着两条动态都在讲吃的。
4. 时效：只看今天的菜单，今天没记录就放弃，不退回昨天。

只读文件、不写对方数据；菜单缺失或解析失败一律返回 None，不影响正常发帖。
对方删掉 history.json 也只会让这个功能静默失效。
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

FILE_NAME = "history.json"
SETTING_KEY = "meal_topic_last_at"

SLOT_LABELS = {
    "breakfast": "早饭",
    "lunch": "午饭",
    "dinner": "晚饭",
    "snack": "加餐",
}

# 允许提「吃」的时段：(起始小时, 结束小时, 当前该看哪一餐)
SLOT_WINDOWS = (
    (5, 10, "breakfast"),
    (10, 15, "lunch"),
    (15, 17, "snack"),
    (17, 22, "dinner"),
)


def _int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class MealBridge:
    """读三餐历史，按概率给出一个可用的发帖话题。"""

    def __init__(self, config: dict, data_dir: Path | None, db=None):
        self.config = config
        self.data_dir = data_dir
        self.db = db

    # ------------------------------------------------------------------
    # 开关与路径
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        value = self.config.get("meal_topic_enabled", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)

    def ratio(self) -> float:
        return max(0.0, min(1.0, _float(self.config.get("meal_topic_ratio", 0.1), 0.1)))

    def cooldown_hours(self) -> int:
        return max(0, _int(self.config.get("meal_topic_cooldown_hours", 12), 12))

    def path(self) -> Path | None:
        custom = str(self.config.get("meal_topic_data_dir", "") or "").strip()
        if custom:
            path = Path(custom)
            return path if path.name.lower() == FILE_NAME else path / FILE_NAME
        if self.data_dir is None:
            return None
        return self.data_dir.parent / "xavier_daily_meal" / FILE_NAME

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    async def maybe_topic(self, now: datetime | None = None) -> str | None:
        """抽中的话返回一句话题素材（含菜单原文），否则 None。"""
        if not self.enabled():
            return None
        now = now or datetime.now()

        slot = self._slot_for(now.hour)
        if not slot:
            return None  # 深夜/凌晨：不拿吃当话题

        if not await self._cooldown_passed(now):
            return None

        history = await self._load_history()
        if not history:
            return None

        text = self._pick_text(history, slot, now)
        if not text:
            return None

        # 抽签放在最后：先确认「确实有东西可说」，再花掉这次概率
        if random.random() >= self.ratio():
            return None

        await self._mark_used(now)
        logger.info(f"[moment] 这次发帖带上三餐话题：{text}")
        return text

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _slot_for(hour: int) -> str:
        for start, end, slot in SLOT_WINDOWS:
            if start <= hour < end:
                return slot
        return ""

    @staticmethod
    def _pick_text(history: dict, slot: str, now: datetime) -> str:
        """只看今天的这一餐；今天没有记录就放弃（不退回昨天）。"""
        menu = history.get(now.date().isoformat())
        if not isinstance(menu, dict):
            return ""
        content = str(menu.get(slot) or "").strip()
        if not content:
            return ""
        # 加餐可能有多样，逗号拼接也算正常内容
        return f"今天{SLOT_LABELS.get(slot, '这一餐')}：{content}"

    async def _cooldown_passed(self, now: datetime) -> bool:
        hours = self.cooldown_hours()
        if hours <= 0 or self.db is None:
            return True
        try:
            raw = await self.db.get_setting(SETTING_KEY, "")
        except Exception:
            logger.exception("[moment] 读取三餐话题冷却失败，按可提处理")
            return True
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return True
        return now - last >= timedelta(hours=hours)

    async def _mark_used(self, now: datetime) -> None:
        if self.db is None:
            return
        try:
            await self.db.set_setting(SETTING_KEY, now.isoformat())
        except Exception:
            logger.exception("[moment] 记录三餐话题时间失败")

    async def _load_history(self) -> dict:
        path = self.path()
        if path is None:
            return {}
        try:
            raw = await asyncio.to_thread(self._read_text, path)
        except OSError:
            return {}  # 没装三餐插件：安静跳过
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[moment] 三餐 history.json 解析失败，本次不带话题")
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _read_text(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8-sig")
