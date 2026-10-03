"""只读 life_state 桥（xavier_life_state 插件的数据，绝不写回）。

用途：评论区回灌私聊时，顺带确认「他此刻正在做什么」，免得摘要里的内容
和他当下的作息打架（例：评论里说在爬山，而他此刻其实窝在家）。
定位是背景自查，不是话题来源：默认一小时最多附一次（life_align_cooldown_minutes），
发帖链路上完全不使用本模块。

对齐规则（任何一条不满足就整段跳过，返回空串）：
1. life_state.json 存在且能解析出 dict
2. 文件里的 date 正好是今天（跨天没续上 → 视为对齐失败）
3. timeline 里能找到「已经开始」的最后一条，且它的 schedule 不为空

时间是中文时段（上午/中午/下午/晚上）和 "14:30" 两种写法都认；
认不出来的条目直接跳过，不会硬塞一个错的状态给他。

这里只读文件，不 import 对方插件、不调用对方 API、不落任何数据。
对方换了存储格式也只会让这段静默失效，不会影响正常注入。
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

FILE_NAME = "life_state.json"
SETTING_KEY = "life_align_last_at"
SCHEDULE_CHARS = 60

# 模糊时段 → 该时段的起始分钟（真实数据里 time 大多是这类词）
FUZZY_ANCHORS = (
    ("凌晨", 0),
    ("清晨", 5 * 60),
    ("早上", 6 * 60 + 30),
    ("早晨", 6 * 60 + 30),
    ("上午", 8 * 60),
    ("中午", 12 * 60),
    ("午间", 12 * 60),
    ("下午", 13 * 60 + 30),
    ("傍晚", 17 * 60 + 30),
    ("晚上", 19 * 60),
    ("夜里", 22 * 60),
    ("深夜", 23 * 60),
    ("半夜", 0),
)


def _start_minute(value) -> int | None:
    """'14:30' / '上午' / '晚上' → 时段起始分钟；认不出来返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        hour, minute = text.split(":")[:2]
        hour_i, minute_i = int(hour), int(minute)
        if 0 <= hour_i < 24 and 0 <= minute_i < 60:
            return hour_i * 60 + minute_i
    except (TypeError, ValueError):
        pass
    for word, minute in FUZZY_ANCHORS:
        if word in text:
            return minute
    return None


class LifeBridge:
    """读 xavier_life_state 的当日日程，给出「此刻」那一行。"""

    def __init__(self, data_dir: Path | None, db=None):
        self.data_dir = data_dir
        self.db = db

    def path(self) -> Path | None:
        return (self.data_dir / FILE_NAME) if self.data_dir else None

    async def current_line_throttled(
        self,
        cooldown_minutes: int = 60,
        now: datetime | None = None,
    ) -> str:
        """带冷却的取用：生活状态只是低频的「避冲突」背景，不该每轮回灌都出现。"""
        now = now or datetime.now()
        if cooldown_minutes > 0 and not await self._cooldown_passed(now, cooldown_minutes):
            return ""
        line = await self.current_line(now)
        if line:
            await self._mark_used(now)
        return line

    async def _cooldown_passed(self, now: datetime, minutes: int) -> bool:
        if self.db is None:
            return True
        try:
            raw = await self.db.get_setting(SETTING_KEY, "")
        except Exception:
            logger.exception("[moment] 读取生活状态冷却失败，按可附加处理")
            return True
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return True
        return now - last >= timedelta(minutes=minutes)

    async def _mark_used(self, now: datetime) -> None:
        if self.db is None:
            return
        try:
            await self.db.set_setting(SETTING_KEY, now.isoformat())
        except Exception:
            logger.exception("[moment] 记录生活状态附加时间失败")

    async def current_line(self, now: datetime | None = None) -> str:
        """返回形如「【你此刻的状态】下午：下楼取快递…（这会儿懒得凑热闹）」的一行。

        对不上（没装插件 / 不是今天 / 时间认不出来）时返回空串，静默跳过。
        """
        path = self.path()
        if path is None:
            return ""
        try:
            raw = await asyncio.to_thread(self._read_text, path)
        except OSError:
            return ""  # 没装 life_state 插件 / 文件还没生成：安静跳过
        if not raw:
            return ""
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[moment] life_state.json 解析失败，跳过生活状态对齐")
            return ""
        if not isinstance(data, dict):
            return ""
        return self._line_from(data, now or datetime.now())

    @staticmethod
    def _read_text(path: Path) -> str:
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8-sig")

    @staticmethod
    def _line_from(data: dict, now: datetime) -> str:
        # 对齐 1：日期必须是今天
        if str(data.get("date") or "") != now.date().isoformat():
            logger.info("[moment] life_state 不是今天的，跳过生活状态对齐")
            return ""

        timeline = data.get("timeline")
        if not isinstance(timeline, list) or not timeline:
            return ""

        # 对齐 2：取「已经开始」里最靠后的那一条
        now_minute = now.hour * 60 + now.minute
        best: tuple[int, dict] | None = None
        for entry in timeline:
            if not isinstance(entry, dict):
                continue
            start = _start_minute(entry.get("time"))
            if start is None or start > now_minute:
                continue
            if best is None or start >= best[0]:
                best = (start, entry)
        if best is None:
            return ""  # 今天的日程还没开始，或时间都认不出来

        entry = best[1]
        schedule = str(entry.get("schedule") or "").strip()
        if not schedule:
            return ""
        if len(schedule) > SCHEDULE_CHARS:
            schedule = schedule[:SCHEDULE_CHARS] + "…"

        label = str(entry.get("time") or "").strip()
        mood = str(entry.get("mood") or "").strip()
        line = f"【你此刻的状态】{label}：{schedule}"
        if mood:
            line += f"（这会儿{mood}）"
        return line
