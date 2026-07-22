"""AstrBot Moment Plugin — 定时调度器

负责每天在配置的时间段内随机安排 AI 发帖任务。
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable
from astrbot.api import logger


class Scheduler:
    """简单的异步定时调度器，不依赖 apscheduler 以减少兼容性问题。"""

    def __init__(self, config: dict, on_trigger: Callable[[], Awaitable[None]]):
        """
        Args:
            config: 插件配置字典
            on_trigger: 触发时调用的异步回调（执行发帖逻辑）
        """
        self.config = config
        self.on_trigger = on_trigger
        self._task: Optional[asyncio.Task] = None
        self._today_schedule: list[datetime] = []
        self._running = False

    async def start(self):
        """启动调度器后台循环。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("[moment] 调度器已启动")

    async def stop(self):
        """停止调度器。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("[moment] 调度器已停止")

    async def _loop(self):
        """主循环：每天计算当日的发帖时间点，然后等待执行。"""
        while self._running:
            try:
                # 计算今天的发帖计划
                self._today_schedule = self._plan_today()
                if self._today_schedule:
                    times_str = [t.strftime("%H:%M") for t in self._today_schedule]
                    logger.info(f"[moment] 今日发帖计划: {times_str}")

                # 执行今天的计划
                for target_time in self._today_schedule:
                    if not self._running:
                        break

                    now = datetime.now()
                    if target_time <= now:
                        # 这个时间点已过，跳过
                        continue

                    # 等到目标时间
                    wait_seconds = (target_time - now).total_seconds()
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)

                    if not self._running:
                        break

                    # 执行发帖
                    try:
                        logger.info(f"[moment] 触发定时发帖任务 ({target_time.strftime('%H:%M')})")
                        await self.on_trigger()
                    except Exception as e:
                        logger.error(f"[moment] 定时发帖执行失败: {e}")

                # 等到明天 00:01 再重新规划
                now = datetime.now()
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
                wait = (tomorrow - now).total_seconds()
                await asyncio.sleep(max(wait, 60))  # 至少等 60 秒

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[moment] 调度器循环异常: {e}")
                await asyncio.sleep(300)  # 出错等 5 分钟再试

    def _plan_today(self) -> list[datetime]:
        """根据配置规划今天的发帖时间点。"""
        count = int(self.config.get("ai_post_count_per_day", 2))
        time_ranges_str = self.config.get("ai_post_time_ranges", "9:00-11:00,19:00-22:00")

        # 解析时间段
        ranges = self._parse_time_ranges(time_ranges_str)
        if not ranges:
            logger.warning("[moment] 未配置有效的发帖时间段")
            return []

        # 在时间段内随机生成时间点
        today = datetime.now().date()
        points = []
        for _ in range(count):
            # 随机选择一个时间段
            start_h, start_m, end_h, end_m = random.choice(ranges)
            start_dt = datetime(today.year, today.month, today.day, start_h, start_m)
            end_dt = datetime(today.year, today.month, today.day, end_h, end_m)

            if end_dt <= start_dt:
                continue

            # 在这个时间段内随机一个时刻
            delta = (end_dt - start_dt).total_seconds()
            random_offset = random.uniform(0, delta)
            target = start_dt + timedelta(seconds=random_offset)
            points.append(target)

        # 排序
        points.sort()
        return points

    def _parse_time_ranges(self, ranges_str: str) -> list[tuple[int, int, int, int]]:
        """解析 '9:00-11:00,19:00-22:00' 格式的时间段。"""
        result = []
        parts = ranges_str.split(",")
        for part in parts:
            part = part.strip()
            if "-" not in part:
                continue
            try:
                start_str, end_str = part.split("-", 1)
                start_parts = start_str.strip().split(":")
                end_parts = end_str.strip().split(":")
                start_h, start_m = int(start_parts[0]), int(start_parts[1]) if len(start_parts) > 1 else 0
                end_h, end_m = int(end_parts[0]), int(end_parts[1]) if len(end_parts) > 1 else 0
                if 0 <= start_h < 24 and 0 <= end_h < 24:
                    result.append((start_h, start_m, end_h, end_m))
            except (ValueError, IndexError):
                continue
        return result
