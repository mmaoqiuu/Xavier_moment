"""今日发帖计划的持久化测试（离线，不依赖 AstrBot 运行时）。

线上现象：每次重载插件，「今天几点发帖」都会被重新摇一遍，于是当天可能
多发或少发动态。

修法：摇好的计划存进 settings（daily_post_schedule），重载后沿用，跨天才重摇。

运行：python tests/test_schedule_persistence.py
"""
import asyncio
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:  # 真环境里有 astrbot，本地离线跑时给它个 stub
    from astrbot.api import logger  # noqa: F401
except Exception:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *a, **k):
            pass

        info = warning = error = exception = debug

    api.logger = _Logger()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from core.scheduler import Scheduler, decode_schedule, encode_schedule  # noqa: E402

NOW = datetime(2026, 10, 1, 12, 0, 0)


class TestCodec(unittest.TestCase):
    def test_round_trip(self):
        times = [NOW.replace(hour=14, minute=30), NOW.replace(hour=20, minute=0)]
        raw = encode_schedule(times, now=NOW)
        self.assertEqual(raw, "2026-10-01|14:30,20:00")
        back = decode_schedule(raw, now=NOW)
        self.assertEqual(back, times)

    def test_yesterday_is_discarded(self):
        """跨天了就该重新摇，而不是沿用昨天的时间点。"""
        raw = encode_schedule([NOW.replace(hour=9, minute=0)], now=datetime(2026, 9, 30, 9, 0))
        self.assertIsNone(decode_schedule(raw, now=NOW))

    def test_garbage_returns_none(self):
        for bad in ("", None, "不是计划", "2026-10-01|", "2026-10-01|还没想好"):
            self.assertIsNone(decode_schedule(bad, now=NOW), f"垃圾输入 {bad!r} 没被挡住")

    def test_partial_garbage_keeps_good_entries(self):
        out = decode_schedule("2026-10-01|14:30,乱写,20:00", now=NOW)
        self.assertEqual(out, [NOW.replace(hour=14, minute=30), NOW.replace(hour=20, minute=0)])


def make_scheduler(saved=None, load_raises=False, save_raises=False, plan=None):
    """搭一个调度器：把 _plan_today 换成固定值，方便断言「摇过几次」。"""
    calls = {"load": 0, "plan": 0, "save": []}

    async def load():
        calls["load"] += 1
        if load_raises:
            raise RuntimeError("读库炸了")
        return saved

    async def save(times):
        calls["save"].append(list(times))
        if save_raises:
            raise RuntimeError("写库炸了")

    sched = Scheduler({}, on_trigger=None, load_schedule=load, save_schedule=save)
    fixed = plan if plan is not None else [NOW.replace(hour=14, minute=30)]

    def fake_plan():
        calls["plan"] += 1
        return list(fixed)

    sched._plan_today = fake_plan
    return sched, calls


class TestTodayPlan(unittest.TestCase):
    def test_saved_plan_is_reused_without_rerolling(self):
        """核心：有存档就沿用，绝不能重摇。"""
        saved = [NOW.replace(hour=14, minute=30), NOW.replace(hour=20, minute=0)]
        sched, calls = make_scheduler(saved=saved)
        out = asyncio.run(sched._today_plan())
        self.assertEqual(out, saved)
        self.assertEqual(calls["plan"], 0, "有存档还重摇了一遍")
        self.assertEqual(calls["save"], [], "有存档不该再存一次")
        self.assertEqual(calls["load"], 1)

    def test_no_saved_plan_rolls_and_saves(self):
        sched, calls = make_scheduler(saved=None)
        out = asyncio.run(sched._today_plan())
        self.assertEqual(len(out), 1)
        self.assertEqual(calls["plan"], 1, "没存档却没摇")
        self.assertEqual(len(calls["save"]), 1, "摇完没落库，下次重载又要重摇")

    def test_reload_twice_keeps_same_plan(self):
        """模拟重载：第二次进来必须拿回第一次那批时间点。"""
        store = {"value": None}

        async def load():
            return store["value"]

        async def save(times):
            store["value"] = list(times)

        sched = Scheduler({}, on_trigger=None, load_schedule=load, save_schedule=save)
        rolled = []
        real_plan = sched._plan_today

        def counting_plan():
            rolled.append(1)
            return [NOW.replace(hour=14, minute=30)]

        sched._plan_today = counting_plan

        first = asyncio.run(sched._today_plan())
        second = asyncio.run(sched._today_plan())
        self.assertEqual(first, second)
        self.assertEqual(len(rolled), 1, "重载后重新摇了")
        self.assertTrue(real_plan is not None)

    def test_load_failure_falls_back_to_roll(self):
        sched, calls = make_scheduler(load_raises=True)
        out = asyncio.run(sched._today_plan())
        self.assertEqual(len(out), 1)
        self.assertEqual(calls["plan"], 1, "读存档失败后应该现摇，而不是罢工")

    def test_save_failure_still_returns_plan(self):
        sched, calls = make_scheduler(save_raises=True)
        out = asyncio.run(sched._today_plan())
        self.assertEqual(len(out), 1, "存不下来也得照常发帖")

    def test_works_without_store_at_all(self):
        """没注入存取回调时，退化成原来的行为，不该报错。"""
        sched = Scheduler({}, on_trigger=None)
        sched._plan_today = lambda: [NOW.replace(hour=14, minute=30)]
        out = asyncio.run(sched._today_plan())
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
