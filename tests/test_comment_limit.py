"""NPC 发言额度相关的计数方法单元测试。

额度算错不会抛异常，只会表现成「评论区突然变安静」或「完全没限住」，
所以把底层几个计数口径单独钉一遍。运行：python tests/test_comment_limit.py
"""
import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---- stub: astrbot.api.logger（db.py 会导入它）----
astrbot = types.ModuleType("astrbot")
api = types.ModuleType("astrbot.api")


class _Logger:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass

    def exception(self, *a, **k):
        pass


api.logger = _Logger()
astrbot.api = api
sys.modules.setdefault("astrbot", astrbot)
sys.modules.setdefault("astrbot.api", api)

sys.path.insert(0, str(ROOT))

from storage.db import MomentDatabase  # noqa: E402


class TestNpcCommentCounting(unittest.TestCase):
    """几种计数口径必须分得清，否则额度会提前用尽、或者根本限不住。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = MomentDatabase(Path(self.tmp) / "test.db")

    def test_total_counts_replies(self):
        """NPC 发言总数要把「接话」也算进去——它们同样占额度。"""
        async def scenario():
            await self.db.connect()
            try:
                post = await self.db.create_post("user", "今天的云很好看")
                c1 = await self.db.create_comment(
                    post["id"], "npc", "确实好看", author_name="陶桃")
                await self.db.create_comment(
                    post["id"], "npc", "我也看到了", parent_comment_id=c1["id"], author_name="蒋楠")
                await self.db.create_comment(post["id"], "user", "是吧")
                return await self.db.count_npc_comments_total(post["id"])
            finally:
                await self.db.close()
        self.assertEqual(asyncio.run(scenario()), 2)

    def test_total_ignores_human_comments(self):
        """你和他发的话不该占用 NPC 的额度。"""
        async def scenario():
            await self.db.connect()
            try:
                post = await self.db.create_post("user", "随便说说")
                await self.db.create_comment(post["id"], "user", "我自己说的")
                await self.db.create_comment(post["id"], "ai", "他插一句")
                return await self.db.count_npc_comments_total(post["id"])
            finally:
                await self.db.close()
        self.assertEqual(asyncio.run(scenario()), 0)

    def test_per_npc_count(self):
        """按人计数只数指定那个人。"""
        async def scenario():
            await self.db.connect()
            try:
                post = await self.db.create_post("user", "数一数")
                await self.db.create_comment(post["id"], "npc", "第一句", author_name="陶桃")
                await self.db.create_comment(post["id"], "npc", "第二句", author_name="陶桃")
                await self.db.create_comment(post["id"], "npc", "别人说的", author_name="蒋楠")
                same = await self.db.count_npc_comments(post["id"], "陶桃")
                other = await self.db.count_npc_comments(post["id"], "蒋楠")
                return same, other
            finally:
                await self.db.close()
        self.assertEqual(asyncio.run(scenario()), (2, 1))

    def test_ai_replies_to_npc_only_counts_npc_targets(self):
        """他回 NPC 才计数：自己开的楼、回你的话都不算。"""
        async def scenario():
            await self.db.connect()
            try:
                post = await self.db.create_post("user", "他回谁")
                npc_c = await self.db.create_comment(
                    post["id"], "npc", "陶桃说的", author_name="陶桃")
                user_c = await self.db.create_comment(post["id"], "user", "我说的")
                await self.db.create_comment(
                    post["id"], "ai", "接陶桃一句", parent_comment_id=npc_c["id"])
                await self.db.create_comment(post["id"], "ai", "我自己开一句")
                await self.db.create_comment(
                    post["id"], "ai", "回你一句", parent_comment_id=user_c["id"])
                return await self.db.count_ai_replies_to_npc(post["id"])
            finally:
                await self.db.close()
        self.assertEqual(asyncio.run(scenario()), 1)

    def test_ai_replies_count_grows_with_each_reply(self):
        """回了两条就是 2，上限判断才靠得住。"""
        async def scenario():
            await self.db.connect()
            try:
                post = await self.db.create_post("user", "连回几条")
                c1 = await self.db.create_comment(
                    post["id"], "npc", "起个头", author_name="陶桃")
                c2 = await self.db.create_comment(
                    post["id"], "npc", "再接一句", author_name="蒋楠")
                await self.db.create_comment(
                    post["id"], "ai", "回第一条", parent_comment_id=c1["id"])
                await self.db.create_comment(
                    post["id"], "ai", "回第二条", parent_comment_id=c2["id"])
                return await self.db.count_ai_replies_to_npc(post["id"])
            finally:
                await self.db.close()
        self.assertEqual(asyncio.run(scenario()), 2)


if __name__ == "__main__":
    unittest.main()
