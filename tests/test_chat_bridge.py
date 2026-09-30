"""core/chat_bridge.py 的离线单测。

不依赖 AstrBot 运行时：先注入最小 stub，再加载被测模块。
运行：python tests/test_chat_bridge.py
"""
import json
import sys
import types
import unittest
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

# ---- 把 core 作为包挂上，并 stub 掉 npc_engine ----
pkg = types.ModuleType("moment_core")
pkg.__path__ = [str(ROOT / "core")]
sys.modules["moment_core"] = pkg

npc_stub = types.ModuleType("moment_core.npc_engine")


def _display_name(config, author, author_name=""):
    if author == "ai":
        return str(config.get("ai_name") or "他")
    return str(config.get("user_name") or "你")


npc_stub.display_name = _display_name
sys.modules["moment_core.npc_engine"] = npc_stub

from moment_core.chat_bridge import ChatBridge  # noqa: E402


class FakeConversation:
    def __init__(self, history, updated_at):
        self.history = json.dumps(history)
        self.updated_at = updated_at


class FakeManager:
    def __init__(self, history, updated_at):
        self._conv = FakeConversation(history, updated_at)
        self.calls = 0

    async def get_curr_conversation_id(self, session):
        self.calls += 1
        return "cid-1"

    async def get_conversation(self, session, conversation_id):
        return self._conv


class FakeDB:
    def __init__(self, session=""):
        self.store = {"chat_bridge_session": session}

    async def get_setting(self, key, default=None):
        return self.store.get(key, default)

    async def set_setting(self, key, value):
        self.store[key] = value


class FakeContext:
    def __init__(self, manager):
        self.conversation_manager = manager


def now() -> float:
    import time

    return time.time()


def build(history, config=None, session="sess-1", updated_at=None):
    cfg = {"ai_name": "沈星回", "user_name": "她"}
    cfg.update(config or {})
    manager = FakeManager(history, now() if updated_at is None else updated_at)
    bridge = ChatBridge(FakeContext(manager), cfg, FakeDB(session))
    return bridge, manager


class TestChatBridge(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_returns_empty(self):
        bridge, manager = build([{"role": "user", "content": "在回家路上"}], {"chat_bridge_enabled": False})
        self.assertEqual(await bridge.collect_context(), "")
        self.assertEqual(manager.calls, 0)

    async def test_stale_session_returns_empty(self):
        bridge, _ = build([{"role": "user", "content": "在回家路上"}], updated_at=now() - 3600 * 5)
        self.assertEqual(await bridge.collect_context(), "")

    async def test_normal_context_contains_both_sides(self):
        bridge, _ = build([
            {"role": "user", "content": "我在回家路上了"},
            {"role": "assistant", "content": "嗯，我等你"},
            {"role": "system", "content": "不应出现"},
        ])
        text = await bridge.collect_context()
        self.assertIn("【你们最近的私聊】", text)
        self.assertIn("她：我在回家路上了", text)
        self.assertIn("沈星回：嗯，我等你", text)
        self.assertNotIn("不应出现", text)
        self.assertIn("不要编造", text)

    async def test_max_messages_keeps_latest(self):
        history = [{"role": "user", "content": f"第{i}句"} for i in range(1, 11)]
        bridge, _ = build(history, {"chat_bridge_max_messages": 3})
        text = await bridge.collect_context()
        self.assertIn("第8句", text)
        self.assertIn("第10句", text)
        self.assertNotIn("第7句", text)

    async def test_max_chars_drops_oldest(self):
        history = [
            {"role": "user", "content": "旧" * 500},
            {"role": "assistant", "content": "新的一句话"},
        ]
        bridge, _ = build(history, {"chat_bridge_max_chars": 100})
        text = await bridge.collect_context()
        self.assertIn("新的一句话", text)
        self.assertNotIn("旧旧旧", text)

    async def test_non_text_content_is_skipped(self):
        history = [
            {"role": "user", "content": [{"type": "image", "url": "http://x/a.png"}]},
            {"role": "user", "content": [{"type": "text", "text": "看图"}]},
        ]
        bridge, _ = build(history)
        text = await bridge.collect_context()
        self.assertIn("看图", text)
        self.assertNotIn("a.png", text)

    async def test_broken_history_does_not_raise(self):
        bridge, manager = build([{"role": "user", "content": "正常"}])
        manager._conv.history = "{不是合法 JSON"
        self.assertEqual(await bridge.collect_context(), "")

    async def test_remember_session_persists(self):
        bridge, _ = build([{"role": "user", "content": "hi"}], session="")
        await bridge.remember_session("weixin:FriendMessage:abc")
        self.assertEqual(bridge.db.store["chat_bridge_session"], "weixin:FriendMessage:abc")

    async def test_config_session_overrides(self):
        bridge, _ = build([{"role": "user", "content": "hi"}], {"chat_bridge_session": "手动会话"})
        self.assertEqual(await bridge._resolve_session(), "手动会话")


if __name__ == "__main__":
    unittest.main(verbosity=2)
