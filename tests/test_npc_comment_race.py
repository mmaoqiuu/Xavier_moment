"""NPC 评论并发竞态的回归测试（离线，不依赖 AstrBot 运行时）。

线上现象：同一条动态下，同一个人出现了两条一模一样的评论。

成因：流程是「查重 -> 调模型（几秒）-> 落库」，三步之间都有 await。两个任务
同时到点时，都会在对方写入之前通过查重，于是各写一条；内容还雷同，因为喂
进去的是同一份上下文。core/npc_comment_guard.py 用一把按 (post_id, npc_name)
的锁把这三步串起来。

运行：python tests/test_npc_comment_race.py
"""
import asyncio
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.npc_comment_guard import NpcCommentGuard, lock_key  # noqa: E402


class FakeDB:
    """只保留这段流程用到的方法。"""

    def __init__(self):
        self.comments = []

    async def count_npc_comments(self, post_id, npc_name):
        return sum(1 for c in self.comments
                   if c["post_id"] == post_id and c["author_name"] == npc_name)

    async def create_comment(self, post_id, npc_name, content):
        await asyncio.sleep(0.005)  # 落库也有耗时
        c = {"id": len(self.comments) + 1, "post_id": post_id,
             "author_name": npc_name, "content": content}
        self.comments.append(c)
        return c


async def comment_flow(db, post_id, npc_name, guard=None, model_seconds=0.05):
    """复刻 _do_npc_comment 的骨架：查重 -> 生成 -> 落库。"""

    async def _body():
        if await db.count_npc_comments(post_id, npc_name) >= 1:
            return  # 已经评过，安静退出
        await asyncio.sleep(model_seconds)  # 模型生成
        await db.create_comment(post_id, npc_name, "过节嘛，到处都是人")

    if guard is None:
        await _body()
    else:
        async with guard.lock_for(post_id, npc_name):
            await _body()


def run(coro):
    return asyncio.run(coro)


class TestRaceReproduction(unittest.TestCase):
    def test_without_guard_reproduces_duplicate(self):
        """没守卫时会冒出两条 —— 这条用来证明测试本身抓得住这个 bug。"""
        db = FakeDB()

        async def go():
            await asyncio.gather(comment_flow(db, 1, "陶桃"),
                                 comment_flow(db, 1, "陶桃"))

        run(go())
        self.assertEqual(len(db.comments), 2, "对照组没能复现重复，测试就没意义了")
        self.assertEqual(db.comments[0]["content"], db.comments[1]["content"])

    def test_with_guard_only_one_comment(self):
        db, guard = FakeDB(), NpcCommentGuard()

        async def go():
            await asyncio.gather(comment_flow(db, 1, "陶桃", guard),
                                 comment_flow(db, 1, "陶桃", guard))

        run(go())
        self.assertEqual(len(db.comments), 1)

    def test_with_guard_three_tasks(self):
        db, guard = FakeDB(), NpcCommentGuard()

        async def go():
            await asyncio.gather(*[comment_flow(db, 1, "陶桃", guard) for _ in range(3)])

        run(go())
        self.assertEqual(len(db.comments), 1)

    def test_different_npcs_both_comment(self):
        """不同的人不该被互相挡住。"""
        db, guard = FakeDB(), NpcCommentGuard()

        async def go():
            await asyncio.gather(comment_flow(db, 1, "陶桃", guard),
                                 comment_flow(db, 1, "毛毛球", guard))

        run(go())
        self.assertEqual(len(db.comments), 2)
        self.assertEqual({c["author_name"] for c in db.comments}, {"陶桃", "毛毛球"})

    def test_same_npc_on_different_posts(self):
        """同一个人在两条动态下各评一条，互不影响。"""
        db, guard = FakeDB(), NpcCommentGuard()

        async def go():
            await asyncio.gather(comment_flow(db, 1, "陶桃", guard),
                                 comment_flow(db, 2, "陶桃", guard))

        run(go())
        self.assertEqual(len(db.comments), 2)
        self.assertEqual({c["post_id"] for c in db.comments}, {1, 2})


class TestGuardSemantics(unittest.TestCase):
    def test_lock_actually_serializes(self):
        """同一对键上的两个协程，执行区间不该交叠。"""
        guard = NpcCommentGuard()
        timeline = []

        async def worker(tag):
            async with guard.lock_for(7, "陶桃"):
                timeline.append(tag + "-enter")
                await asyncio.sleep(0.03)
                timeline.append(tag + "-exit")

        async def go():
            await asyncio.gather(worker("a"), worker("b"))

        run(go())
        self.assertIn(timeline, (
            ["a-enter", "a-exit", "b-enter", "b-exit"],
            ["b-enter", "b-exit", "a-enter", "a-exit"],
        ), f"出现交叠，说明没锁住: {timeline}")

    def test_same_key_same_lock(self):
        guard = NpcCommentGuard()
        self.assertIs(guard.lock_for(1, "陶桃"), guard.lock_for(1, "陶桃"))
        self.assertIsNot(guard.lock_for(1, "陶桃"), guard.lock_for(1, "毛毛球"))
        self.assertIsNot(guard.lock_for(1, "陶桃"), guard.lock_for(2, "陶桃"))

    def test_key_format(self):
        self.assertEqual(lock_key(3, "陶桃"), "3:陶桃")

    def test_idle_keys_reclaimed(self):
        """超过上限时清掉没人排队的锁，字典不无限长大。"""
        guard = NpcCommentGuard(max_keys=5)
        for i in range(20):
            guard.lock_for(i, "陶桃")
        self.assertLessEqual(guard.size, 6, "没人排队的锁没被回收")


if __name__ == "__main__":
    unittest.main()
