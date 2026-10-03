"""v0.15.0 新模块自测（摘要构建 / life_state 对齐 / 三餐话题）

离线可跑（astrbot 不可用时自动打桩），不写任何真实数据：python tests/test_v015_bridges.py

原说明：（不依赖正在运行的 AstrBot）。

跑法：python tmp_test_v015.py
覆盖：摘要构建、life_state 对齐与跳过、三餐抽样/时段/冷却。
"""
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PLUGIN = Path(r"C:\Users\Administrator\Desktop\AstrBot-master\data\plugins\astrbot_plugin_xavier_moment")
sys.path.insert(0, str(PLUGIN))

# astrbot.api.logger 依赖：离线环境用假 logger 顶上
try:
    import astrbot.api  # noqa: F401
except Exception:
    import types

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        exception=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    sys.modules.setdefault("astrbot", astrbot)
    sys.modules.setdefault("astrbot.api", api)

from core.comment_digest import build_digest_text  # noqa: E402
from core.life_bridge import LifeBridge  # noqa: E402
from core.meal_bridge import MealBridge  # noqa: E402

FAILED = []


def check(name, ok, extra=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not ok:
        FAILED.append(name)


# ---------- 1. 摘要构建 ----------
post = {"id": 7, "content": "天台的风很舒服，要是你在就好了", "created_at": "2026-10-01T20:10:00"}
comments = [
    {"id": 1, "author": "user", "author_name": "她", "content": "你怎么又吹风", "created_at": "2026-10-01T20:11:00", "parent_comment_id": None},
    {"id": 2, "author": "npc", "author_name": "邱诺亚", "content": "又一个人上天台？", "created_at": "2026-10-01T20:12:00", "parent_comment_id": None},
    {"id": 3, "author": "npc", "author_name": "邱诺亚", "content": "下次带我", "created_at": "2026-10-01T20:20:00", "parent_comment_id": None},
    {"id": 4, "author": "ai", "author_name": "他", "content": "带你你又不来", "created_at": "2026-10-01T20:21:00", "parent_comment_id": 3},
]
text = build_digest_text(post, comments)
check("摘要：生成", bool(text))
check("摘要：不含用户评论", "你怎么又吹风" not in (text or ""))
check("摘要：同一 NPC 只留最后一条", (text or "").count("- 邱诺亚：") == 1)
check("摘要：标出他在回谁", "你回了邱诺亚" in (text or ""))
check("摘要：只有用户发言时返回 None", build_digest_text(post, comments[:1]) is None)
check("摘要：空评论返回 None", build_digest_text(post, []) is None)

# ---------- 2. life_state 对齐 ----------
now = datetime(2026, 10, 1, 14, 30)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    ls_dir = root / "xavier_life_state"
    ls_dir.mkdir()
    today = {
        "date": "2026-10-01",
        "timeline": [
            {"time": "08:00", "schedule": "晨跑"},
            {"time": "14:00", "schedule": "在公司开会"},
            {"time": "19:00", "schedule": "回家做饭"},
        ],
    }
    (ls_dir / "life_state.json").write_text(json.dumps(today, ensure_ascii=False), encoding="utf-8")
    line = asyncio.run(LifeBridge(ls_dir).current_line(now))
    check("life：对齐到今天当前时段", "14:00" in line and "开会" in line, line)

    (ls_dir / "life_state.json").write_text(
        json.dumps({**today, "date": "2026-09-30"}, ensure_ascii=False), encoding="utf-8")
    check("life：日期不是今天则跳过", asyncio.run(LifeBridge(ls_dir).current_line(now)) == "")

    (ls_dir / "life_state.json").write_text(
        json.dumps({"date": "2026-10-01", "timeline": [{"time": "19:00", "schedule": "做饭"}]}, ensure_ascii=False),
        encoding="utf-8")
    check("life：日程全在将来则跳过", asyncio.run(LifeBridge(ls_dir).current_line(now)) == "")

    (ls_dir / "life_state.json").write_text("{坏掉的 json", encoding="utf-8")
    check("life：解析失败则跳过", asyncio.run(LifeBridge(ls_dir).current_line(now)) == "")

    check("life：文件不存在则跳过", asyncio.run(LifeBridge(root / "nope").current_line(now)) == "")

# ---------- 3. 三餐抽样 ----------
class FakeDB:
    def __init__(self):
        self.store = {}

    async def get_setting(self, key, default=""):
        return self.store.get(key, default)

    async def set_setting(self, key, value):
        self.store[key] = value


async def meal_cases():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        md = root / "xavier_daily_meal"
        md.mkdir()
        (md / "history.json").write_text(json.dumps({
            "2026-10-01": {"breakfast": "豌杂面", "lunch": "过桥米线", "dinner": "饺子", "snack": ""},
            "2026-09-30": {"breakfast": "热干面", "lunch": "五花肉", "dinner": "蹄花煲", "snack": "蛋挞"},
        }, ensure_ascii=False), encoding="utf-8")

        db = FakeDB()
        cfg = {"meal_topic_enabled": True, "meal_topic_ratio": 1.0, "meal_topic_cooldown_hours": 12}
        host = root / "astrbot_plugin_xavier_moment"
        bridge = MealBridge(cfg, host, db=db)
        noon = datetime(2026, 10, 1, 12, 30)
        first = await bridge.maybe_topic(noon)
        check("三餐：午饭时段命中", bool(first) and "过桥米线" in first, first)
        second = await bridge.maybe_topic(noon)
        check("三餐：冷却期内不再提", second is None)

        check("三餐：深夜不提", await bridge.maybe_topic(datetime(2026, 10, 1, 23, 30)) is None)

        db2 = FakeDB()
        bridge2 = MealBridge({**cfg, "meal_topic_ratio": 0.0}, host, db=db2)
        check("三餐：概率 0 不命中", await bridge2.maybe_topic(noon) is None)

        # 只看今天：今天这一餐没记录就不提，不退回昨天
        (md / "history.json").write_text(json.dumps({
            "2026-10-01": {"breakfast": "豌杂面", "lunch": "", "dinner": "饺子", "snack": ""},
            "2026-09-30": {"breakfast": "热干面", "lunch": "五花肉", "dinner": "蹄花煲", "snack": "蛋挞"},
        }, ensure_ascii=False), encoding="utf-8")
        bridge3 = MealBridge({**cfg, "meal_topic_cooldown_hours": 0}, host, db=FakeDB())
        check("三餐：今天没记录则不退回昨天", await bridge3.maybe_topic(noon) is None)

        bridge3 = MealBridge({**cfg, "meal_topic_enabled": False}, host, db=FakeDB())
        check("三餐：总开关关闭", await bridge3.maybe_topic(noon) is None)

        empty = root / "empty"
        empty.mkdir()
        bridge4 = MealBridge(cfg, root / "nowhere" / "astrbot_plugin_xavier_moment", db=FakeDB())
        check("三餐：没装三餐插件时安静返回", await bridge4.maybe_topic(noon) is None)


asyncio.run(meal_cases())

# ---------- 3.5 生活状态附加冷却（只做避冲突背景，低频） ----------
async def life_throttle_cases():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp) / "xavier_life_state"
        d.mkdir()
        (d / "life_state.json").write_text(json.dumps({
            "date": "2026-10-01",
            "timeline": [{"time": "14:00", "schedule": "在公司开会"}],
        }, ensure_ascii=False), encoding="utf-8")
        t0 = datetime(2026, 10, 1, 14, 30)

        db = FakeDB()
        bridge = LifeBridge(d, db=db)
        first = await bridge.current_line_throttled(60, t0)
        check("life：首次可附加", "开会" in first, first)
        second = await bridge.current_line_throttled(60, t0 + timedelta(minutes=10))
        check("life：冷却期内不再附加", second == "", second)
        third = await bridge.current_line_throttled(60, t0 + timedelta(minutes=61))
        check("life：冷却过后可再附加", "开会" in third, third)
        every = await LifeBridge(d, db=FakeDB()).current_line_throttled(0, t0)
        check("life：冷却 0 表示每次都附加", "开会" in every, every)


asyncio.run(life_throttle_cases())

# ---------- 4. 中文时段（真实数据格式） ----------
with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp) / "xavier_life_state"
    d.mkdir()
    (d / "life_state.json").write_text(json.dumps({
        "date": "2026-10-01",
        "timeline": [
            {"time": "上午", "schedule": "窝在沙发上看电视", "mood": "放空"},
            {"time": "下午", "schedule": "下楼取快递", "mood": "懒得凑热闹"},
            {"time": "晚上", "schedule": "把椅子拖到阳台坐着"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    line = asyncio.run(LifeBridge(d).current_line(now))
    check("life：中文时段对齐", "下午" in line and "取快递" in line and "懒得凑热闹" in line, line)
    late = asyncio.run(LifeBridge(d).current_line(datetime(2026, 10, 1, 23, 30)))
    check("life：深夜取晚饭那一段", "晚上" in late, late)

# ---------- 5. 真实数据只读核对 ----------
real_life = Path(r"C:\Users\Administrator\Desktop\AstrBot-master\data\plugin_data\xavier_life_state")
print("real life now:", asyncio.run(LifeBridge(real_life).current_line()))
real_host = Path(r"C:\Users\Administrator\Desktop\AstrBot-master\data\plugin_data\astrbot_plugin_xavier_moment")
real_cfg = {"meal_topic_enabled": True, "meal_topic_ratio": 1.0, "meal_topic_cooldown_hours": 0}
print("real meal topic (深夜应为 None):", asyncio.run(MealBridge(real_cfg, real_host, db=FakeDB()).maybe_topic()))
print("real meal topic (午后):", asyncio.run(MealBridge(real_cfg, real_host, db=FakeDB()).maybe_topic(datetime(2026, 10, 1, 12, 30))))


if __name__ == "__main__" and FAILED:
    raise SystemExit("failed: " + ", ".join(FAILED))
