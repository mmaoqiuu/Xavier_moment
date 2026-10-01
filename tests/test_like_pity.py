"""NPC 点赞保底机制的单元测试。

这部分最容易悄悄坏掉：概率一旦永远掷中或永远掷不中，程序不报任何错，
只是评论区一直没人点赞。所以这里把边界都钉死。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.like_engine import LikeEngine  # noqa: E402


class FakeNpcEngine:
    """只提供 LikeEngine 需要的那一个方法。"""

    def __init__(self, names):
        self._names = list(names)

    def parse_list(self):
        return [{"name": n} for n in self._names]


def make_engine(**overrides):
    cfg = {
        "like_enabled": True,
        "npc_like_probability": 0.0,
        "npc_like_count_max": 2,
        "npc_like_pity": 2,
    }
    cfg.update(overrides)
    return LikeEngine(None, cfg, npc_engine=FakeNpcEngine(["陶桃", "蒋楠", "陈弦"]))


class TestNpcLikePity(unittest.TestCase):
    def test_probability_zero_never_hits_without_force(self):
        """概率为 0 时不保底就永远不中。"""
        eng = make_engine(npc_like_probability=0.0)
        for _ in range(50):
            plan = eng.npc_pick_plan()
            self.assertFalse(plan["hit"])
            self.assertEqual(plan["names"], [])

    def test_probability_one_always_hits(self):
        """概率为 1 时条条都有人来。"""
        eng = make_engine(npc_like_probability=1.0)
        for _ in range(50):
            plan = eng.npc_pick_plan()
            self.assertTrue(plan["hit"])
            self.assertTrue(plan["names"])

    def test_force_hits_even_when_probability_is_zero(self):
        """保底：概率为 0 也能硬给一个。"""
        eng = make_engine(npc_like_probability=0.0)
        plan = eng.npc_pick_plan(force=True)
        self.assertTrue(plan["hit"])
        self.assertTrue(plan["forced"])
        self.assertTrue(set(plan["names"]) <= {"陶桃", "蒋楠", "陈弦"})

    def test_names_unique_and_within_limit(self):
        """挑出来的人不重复、不超上限。"""
        eng = make_engine(npc_like_probability=1.0, npc_like_count_max=2)
        for _ in range(100):
            names = eng.npc_pick_plan()["names"]
            self.assertLessEqual(len(names), 2)
            self.assertEqual(len(names), len(set(names)))

    def test_should_force_only_after_limit(self):
        """连空到阈值才触发，之前不触发。"""
        eng = make_engine(npc_like_pity=2)
        self.assertFalse(eng.should_force(0))
        self.assertFalse(eng.should_force(1))
        self.assertTrue(eng.should_force(2))
        self.assertTrue(eng.should_force(5))

    def test_pity_zero_disables_force(self):
        """pity=0 时保底彻底关闭。"""
        eng = make_engine(npc_like_pity=0)
        self.assertFalse(eng.should_force(0))
        self.assertFalse(eng.should_force(99))
        self.assertFalse(eng.npc_pick_plan(force=False)["hit"])

    def test_force_without_candidates_fails_cleanly(self):
        """候选池为空时，保底也不该凭空造人。"""
        eng = LikeEngine(
            None,
            {"like_enabled": True, "npc_like_probability": 0.0,
             "npc_like_count_max": 2, "npc_like_pity": 2},
            npc_engine=FakeNpcEngine([]),
        )
        plan = eng.npc_pick_plan(force=True)
        self.assertFalse(plan["hit"])
        self.assertFalse(plan["forced"])
        self.assertEqual(plan["pool_size"], 0)

    def test_count_max_zero_means_nobody(self):
        """上限设 0 表示不许有人来赞，保底也不能违反。"""
        eng = make_engine(npc_like_count_max=0)
        self.assertFalse(eng.npc_pick_plan(force=True)["hit"])

    def test_disabled_engine_never_hits(self):
        """总开关关掉时，连保底都不动。"""
        eng = make_engine(like_enabled=False, npc_like_probability=1.0)
        self.assertFalse(eng.npc_pick_plan(force=True)["hit"])


if __name__ == "__main__":
    unittest.main()
