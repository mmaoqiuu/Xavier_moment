"""NPC 评论输出解析的回归测试（离线，不依赖 AstrBot 运行时）。

线上现象：评论区挂出 {"reply_to": "沈星回", "content": "content"} 这种原始 JSON。

成因在 core/npc_engine.py 的 parse_output：解析失败时把「整段原文」当正文返回。
现在改成分级——标准 JSON -> 宽松抠字段 -> 认不出来就返回空（这条不发）。

运行：python tests/test_npc_output_parse.py
"""
import sys
import types
import unittest
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

from core.npc_engine import NpcEngine, _loose_json_fields  # noqa: E402

parse = NpcEngine.parse_output


class TestParseOutput(unittest.TestCase):
    def test_normal_json(self):
        out = parse('{"reply_to": "", "content": "过节嘛，到处都是人"}')
        self.assertEqual(out, {"reply_to": "", "content": "过节嘛，到处都是人"})

    def test_fenced_json(self):
        out = parse('```json\n{"reply_to": "沈星回", "content": "第一只给光猎"}\n```')
        self.assertEqual(out["content"], "第一只给光猎")
        self.assertEqual(out["reply_to"], "沈星回")

    def test_json_with_prose_around(self):
        out = parse('好的，我这样评论：\n{"reply_to": "沈星回", "content": "第一只给光猎"}')
        self.assertEqual(out["content"], "第一只给光猎")

    def test_plain_sentence_still_used(self):
        """不含花括号的输出不许被丢掉——那本来就是一句人话。"""
        self.assertEqual(parse("过节嘛，到处都是人")["content"], "过节嘛，到处都是人")

    # ↓↓↓ 回归：对着线上截图那一条
    def test_broken_json_never_reaches_comment_area(self):
        out = parse('{"reply_to": "沈星回", "content": }')
        self.assertNotIn("{", out["content"], "原始 JSON 又漏到评论区了")
        self.assertNotIn("reply_to", out["content"])
        self.assertNotIn("content", out["content"])

    def test_chinese_quote_json_fields_recovered(self):
        out = parse('{\u201creply_to\u201d: \u201c沈星回\u201d, \u201ccontent\u201d: \u201c第一只给光猎\u201d}')
        self.assertEqual(out["content"], "第一只给光猎")
        self.assertEqual(out["reply_to"], "沈星回")

    def test_trailing_comma_recovered(self):
        out = parse('{"reply_to": "沈星回", "content": "第一只给光猎",,}')
        self.assertEqual(out["content"], "第一只给光猎")

    def test_unrecognizable_returns_empty(self):
        """认不出来时返回空，交给上层丢弃——宁可不发，也不挂乱码。"""
        out = parse('{"whatever": 1, "nope": 2}')
        self.assertEqual(out["content"], "")
        self.assertEqual(out["reply_to"], "")

    def test_empty_input(self):
        for bad in ("", None, "   "):
            self.assertEqual(parse(bad)["content"], "")

    def test_loose_helper_on_garbage(self):
        self.assertEqual(_loose_json_fields("这里没有任何字段"), {})


if __name__ == "__main__":
    unittest.main()
