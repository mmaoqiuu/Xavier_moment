"""NPC 关系约束的纯逻辑测试。

覆盖两段纯函数：从人设里认出「跟他生疏」的人、解析手动名单。
不依赖 AstrBot 运行时，可以直接跑：

    python -m unittest discover -s tests -v
"""
import logging
import sys
import types
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))
if "astrbot" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from core.npc_engine import (
    NpcEngine,
    filter_chain_candidates,
    parse_stranger_names,
    persona_says_stranger,
)

BEIJIAYAN = "女，邱诺亚的女朋友，也是 user 的朋友之一，与沈星回并不熟识。经营一家甜品店。喜欢金盏花。"
TAOTAO = "女，沈星回和 user 在猎人协会的同事，user 的好朋友。娃娃脸，性格像名字一样可爱。对沈星回很有边界感和分寸感。"


class TestPersonaSaysStranger(unittest.TestCase):
    def test_persona_says_unfamiliar(self):
        """贝佳妍的人设里写了「与沈星回并不熟识」，要认得出来。"""
        self.assertTrue(persona_says_stranger(BEIJIAYAN, "沈星回"))

    def test_mere_boundary_is_not_unfamiliar(self):
        """陶桃只是「有边界感」，不算生疏，别误伤。"""
        self.assertFalse(persona_says_stranger(TAOTAO, "沈星回"))

    def test_unrelated_clause_ignored(self):
        """「给不熟的客人打折」里没有他的名字，不算数。"""
        self.assertFalse(persona_says_stranger("经营一家甜品店，常给不熟的客人打折。", "沈星回"))

    def test_relation_differs_per_person(self):
        """同一段人设里，对不同的人可以亲疏不同。"""
        persona = "与沈星回不熟，但与 user 是很好的朋友。"
        self.assertTrue(persona_says_stranger(persona, "沈星回"))
        self.assertFalse(persona_says_stranger(persona, "user"))

    def test_empty_inputs(self):
        self.assertFalse(persona_says_stranger("", "沈星回"))
        self.assertFalse(persona_says_stranger(BEIJIAYAN, ""))

    def test_parse_stranger_names(self):
        self.assertEqual(parse_stranger_names("贝佳妍" + chr(10) + "邱诺亚，陶桃"), {"贝佳妍", "邱诺亚", "陶桃"})
        self.assertEqual(parse_stranger_names("  伊澄 、 陈弦  "), {"伊澄", "陈弦"})
        self.assertEqual(parse_stranger_names(""), set())
        self.assertEqual(parse_stranger_names(None), set())


class TestAiStrangers(unittest.TestCase):
    def _engine(self, npc_list: str) -> NpcEngine:
        return NpcEngine(None, {"ai_name": "沈星回", "npc_list": npc_list})

    def test_auto_detect_from_persona(self):
        engine = self._engine("贝佳妍|" + BEIJIAYAN + chr(10) + "陶桃|" + TAOTAO)
        self.assertEqual(engine.ai_strangers(), {"贝佳妍"})
        self.assertTrue(engine.is_stranger_to_ai("贝佳妍"))
        self.assertFalse(engine.is_stranger_to_ai("陶桃"))

    def test_manual_list_is_merged(self):
        engine = self._engine("陶桃|" + TAOTAO)
        engine.config["npc_ai_strangers"] = "邱诺亚" + chr(10) + "伊澄，陶桃"
        self.assertEqual(engine.ai_strangers(), {"邱诺亚", "伊澄", "陶桃"})

    def test_chain_candidates_exclude_his_comment(self):
        """跟他生疏的人链式接话时，候选里不能有他的话，别人不受影响。"""
        others = [
            {"author": "ai", "content": "他说的"},
            {"author": "user", "content": "你说的"},
            {"author": "npc", "author_name": "陶桃", "content": "别人说的"},
        ]
        filtered = filter_chain_candidates(others, is_stranger=True)
        self.assertEqual([c["content"] for c in filtered], ["你说的", "别人说的"])
        self.assertEqual(len(filter_chain_candidates(others, is_stranger=False)), 3)

    def test_chain_candidates_can_end_up_empty(self):
        """池子里只剩他的话，就没人可接，返回空。"""
        others = [{"author": "ai", "content": "只有他"}]
        self.assertEqual(filter_chain_candidates(others, is_stranger=True), [])

    def test_no_ai_name_means_no_auto_detect(self):
        engine = NpcEngine(None, {"npc_list": "贝佳妍|" + BEIJIAYAN})
        self.assertEqual(engine.ai_strangers(), set())


if __name__ == "__main__":
    unittest.main()
