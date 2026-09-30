"""NPC 引擎的纯逻辑测试（不联网、不依赖 AstrBot 运行时）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.npc_engine import NpcEngine, display_name


RAW = """- 邱诺亚|花店老板，黑客技术很强
- 贝佳妍|甜品店老板娘，喜欢金盏花
这一行没有分隔符，应该被跳过
-
- 陶桃|八卦与占卜达人
- 邱诺亚|重复的名字，应去重
"""


def make_engine(**overrides):
    config = {"npc_list": RAW, "npc_enabled": True}
    config.update(overrides)
    return NpcEngine(context=None, config=config)


def test_parse_list_skips_bad_lines_and_dedupes():
    names = make_engine().names()
    assert names == ["邱诺亚", "贝佳妍", "陶桃"]


def test_parse_list_handles_empty_config():
    assert make_engine(npc_list="").names() == []


def test_parse_list_cache_rebuilds_when_config_changes():
    engine = make_engine()
    assert len(engine.names()) == 3
    engine.config["npc_list"] = "新朋友|测试用"
    assert engine.names() == ["新朋友"]


def test_pick_avoids_excluded_then_falls_back():
    engine = make_engine(npc_list="甲|a\n乙|b")
    assert [n["name"] for n in engine.pick(1, exclude={"甲"})] == ["乙"]
    # 全部被排除时放宽，避免评论区空着
    assert len(engine.pick(1, exclude={"甲", "乙"})) == 1


def test_pick_never_exceeds_pool():
    engine = make_engine()
    assert len(engine.pick(10)) == 3


def test_plan_batch_respects_probability_zero():
    assert make_engine(npc_comment_probability=0).plan_batch() == []


def test_plan_batch_count_within_range():
    engine = make_engine(npc_comment_probability=1, npc_count_min=2, npc_count_max=2)
    picks = engine.plan_batch()
    assert len(picks) == 2
    assert len(set(p["name"] for p in picks)) == 2


def test_plan_batch_disabled():
    assert make_engine(npc_enabled=False).plan_batch() == []


def test_parse_output_json():
    assert NpcEngine.parse_output('{"reply_to": "陶桃", "content": "你们俩又熬夜了？"}') == {
        "reply_to": "陶桃",
        "content": "你们俩又熬夜了？",
    }


def test_parse_output_fenced_json_with_noise():
    raw = '好的，这是结果：\n```json\n{"reply_to": "", "content": "花店新到了金盏花"}\n```'
    assert NpcEngine.parse_output(raw)["content"] == "花店新到了金盏花"


def test_parse_output_falls_back_to_plain_text():
    assert NpcEngine.parse_output("直接说了句话") == {"reply_to": "", "content": "直接说了句话"}


def test_display_name_mapping():
    config = {"ai_name": "沈星回", "user_name": "毛毛球"}
    assert display_name(config, "ai") == "沈星回"
    assert display_name(config, "user") == "毛毛球"
    assert display_name(config, "npc", "陶桃") == "陶桃"
    assert display_name({}, "user") == "我"
