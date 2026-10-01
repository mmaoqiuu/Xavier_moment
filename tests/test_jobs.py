"""storage/db.py 中 jobs（延迟任务待办）的离线单测。

不依赖 AstrBot 运行时，也不联网：只验证「重载后不丢事」这件事的数据库底座。
运行：python tests/test_jobs.py
"""
import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ---- stub: astrbot.api.logger ----
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
sys.modules["astrbot.api"] = api

# ---- 把 storage 目录挂成一个包，方便直接 import db.py ----
pkg = types.ModuleType("moment_storage")
pkg.__path__ = [str(ROOT / "storage")]
sys.modules.setdefault("moment_storage", pkg)

from moment_storage import db as db_module  # noqa: E402


def _due(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).isoformat()


class JobsTestCase(unittest.TestCase):
    """待办表的增删查改：去重、重新排队、按时间排序、过期与清理。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = db_module.MomentDatabase(Path(self._tmp.name) / "test.db")
        asyncio.run(self.db.connect())

    def tearDown(self):
        asyncio.run(self.db.close())
        self._tmp.cleanup()

    # ------------------------------------------------------------
    def test_same_job_queued_once(self):
        """同一件事排两次，只留一条待办。"""
        async def scenario():
            first = await self.db.add_job("npc_like", {"name": "小雨"}, _due(60), dedup_key="npc_like:1:小雨", post_id=1)
            second = await self.db.add_job("npc_like", {"name": "小雨"}, _due(90), dedup_key="npc_like:1:小雨", post_id=1)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            self.assertEqual(await self.db.count_pending_jobs(kind="npc_like", post_id=1), 1)

        asyncio.run(scenario())

    def test_finished_job_can_be_queued_again(self):
        """跑完之后同一件事还能重新排，而且是同一条记录被复用。"""
        async def scenario():
            job_id = await self.db.add_job("ai_like", {"post_id": 5}, _due(1), dedup_key="ai_like:5", post_id=5)
            await self.db.mark_job(job_id, "done", bump_attempts=True)
            self.assertEqual(await self.db.count_pending_jobs(), 0)

            again = await self.db.add_job("ai_like", {"post_id": 5}, _due(1), dedup_key="ai_like:5", post_id=5)
            self.assertEqual(again, job_id)
            self.assertEqual(await self.db.count_pending_jobs(kind="ai_like"), 1)

        asyncio.run(scenario())

    def test_pending_jobs_sorted_and_resumable(self):
        """待办按计划时间从早到晚取出来，参数原样还在。"""
        async def scenario():
            await self.db.add_job("npc_comment", {"post_id": 2, "npc_name": "阿哲"}, _due(300), dedup_key="npc_comment:2:阿哲", post_id=2)
            await self.db.add_job("ai_comment", {"post_id": 3, "content": "今天也在"}, _due(30), dedup_key="ai_comment:3", post_id=3)

            rows = await self.db.get_pending_jobs()
            self.assertEqual([r["kind"] for r in rows], ["ai_comment", "npc_comment"])
            self.assertEqual(rows[0]["payload"], '{"post_id": 3, "content": "今天也在"}')
            self.assertEqual(rows[0]["post_id"], 3)

        asyncio.run(scenario())

    def test_expire_all_and_purge(self):
        """续跑关闭时整批过期；历史记录按天数清理，pending 不受影响。"""
        async def scenario():
            a = await self.db.add_job("npc_reply", {"post_id": 1, "comment_id": 7}, _due(60), dedup_key="npc_reply:7", post_id=1)
            b = await self.db.add_job("npc_like", {"name": "小雨"}, _due(120), dedup_key="npc_like:1:小雨", post_id=1)

            self.assertEqual(await self.db.expire_all_pending_jobs(), 2)
            self.assertEqual(await self.db.count_pending_jobs(), 0)

            fresh = await self.db.add_job("ai_reply", {"post_id": 1, "comment": {"id": 9}}, _due(60), dedup_key="ai_reply:9", post_id=1)
            await self.db.mark_job(fresh, "done", bump_attempts=True)

            removed = await self.db.purge_jobs(keep_days=0)
            self.assertEqual(removed, 3)
            self.assertEqual(await self.db.get_pending_jobs(), [])
            self.assertTrue(a and b and fresh)

        asyncio.run(scenario())

    def test_idempotency_helpers(self):
        """补跑前的幂等判断：他评没评过、这波 NPC 来没来。"""
        async def scenario():
            post = await self.db.create_post("user", "今天天气不错", mood="日常")
            post_id = post["id"]
            self.assertFalse(await self.db.has_ai_comment(post_id))
            await self.db.create_comment(post_id, "ai", "是挺好的")
            self.assertTrue(await self.db.has_ai_comment(post_id))

            self.assertEqual(await self.db.count_npc_comments_total(post_id), 0)
            await self.db.create_comment(post_id, "npc", "确实", None, "小雨")
            await self.db.create_comment(post_id, "npc", "你也在呀", None, "阿哲")
            self.assertEqual(await self.db.count_npc_comments_total(post_id), 2)

            # 这条 NPC 评论有没有被他回过：没有 → 补跑时才需要回
            comment = (await self.db.get_comments(post_id))[-1]
            self.assertFalse(await self.db.has_ai_reply_to(comment["id"]))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
