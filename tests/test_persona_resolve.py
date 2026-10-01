"""人设解析（三级回落）的离线测试。

覆盖：
  - 官方 API 优先：配置指定人格 / 留空跟随默认
  - 「默认人格」(值 'default') 视为跟随默认，而不是去查内置通用助手
  - API 不可用时回退数据库，且匹配列必须是 persona_id（不是自增主键 id）
  - 数据库兜底不再「盲取第一条」
  - 缓存命中与失效

不依赖 AstrBot 运行时，可以直接跑：

    python -m unittest discover -s tests -v
"""
import asyncio
import logging
import sqlite3
import sys
import tempfile
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

from core.material import MaterialCollector  # noqa: E402


class FakePersonaManager:
    """模拟 PersonaManager：by_id 同步、default 异步。"""

    def __init__(self, by_id=None, default=None, raise_on_by_id=False):
        self._by_id = by_id or {}
        self._default = default
        self.by_id_calls = []
        self.default_calls = 0
        self._raise = raise_on_by_id

    def get_persona_v3_by_id(self, persona_id):
        self.by_id_calls.append(persona_id)
        if self._raise:
            raise RuntimeError("boom")
        return self._by_id.get(persona_id)

    async def get_default_persona_v3(self, umo=None):
        self.default_calls += 1
        return self._default


class FakeContext:
    def __init__(self, manager=None):
        if manager is not None:
            self.persona_manager = manager


def _run(coro):
    return asyncio.run(coro)


class PersonaResolveTests(unittest.TestCase):
    def test_configured_persona_takes_priority(self):
        """配置指定了人格 → 用官方 API 精确取它。"""
        manager = FakePersonaManager(
            by_id={"sxh": {"prompt": "我是沈星回"}},
            default={"prompt": "默认人格"},
        )
        collector = MaterialCollector(FakeContext(manager), {"persona_id": "sxh"})
        self.assertEqual(_run(collector.get_persona()), "我是沈星回")
        self.assertEqual(manager.by_id_calls, ["sxh"])
        self.assertEqual(manager.default_calls, 0)

    def test_blank_follows_current_default(self):
        """留空 → 跟随 AstrBot 当前默认人格。"""
        manager = FakePersonaManager(default={"prompt": "默认人格"})
        collector = MaterialCollector(FakeContext(manager), {})
        self.assertEqual(_run(collector.get_persona()), "默认人格")
        self.assertEqual(manager.by_id_calls, [])
        self.assertEqual(manager.default_calls, 1)

    def test_default_keyword_follows_default(self):
        """选「默认人格」(值 'default') 时走默认人格，而不是查询内置通用助手。"""
        manager = FakePersonaManager(
            by_id={"default": {"prompt": "内置通用助手"}},
            default={"prompt": "用户当前人格"},
        )
        collector = MaterialCollector(FakeContext(manager), {"persona_id": "default"})
        self.assertEqual(_run(collector.get_persona()), "用户当前人格")
        # 关键：不应把 'default' 当成一个具体人格去查
        self.assertEqual(manager.by_id_calls, [])

    def test_unknown_configured_persona_falls_back_to_default(self):
        """配置的人格不存在 → 回退到当前默认人格，而不是返回空。"""
        manager = FakePersonaManager(by_id={}, default={"prompt": "默认人格"})
        collector = MaterialCollector(FakeContext(manager), {"persona_id": "不存在"})
        self.assertEqual(_run(collector.get_persona()), "默认人格")
        self.assertEqual(manager.by_id_calls, ["不存在"])

    def test_api_unavailable_falls_back_to_db(self):
        """没有 persona_manager 时，回退数据库兜底。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            self._make_db(data_dir / "data_v4.db")
            original = MaterialCollector._locate_data_dir
            MaterialCollector._locate_data_dir = staticmethod(lambda: data_dir)
            try:
                collector = MaterialCollector(FakeContext(None), {"persona_id": "Python"})
                self.assertEqual(_run(collector.get_persona()), "你是一个专业 Python 工程师")
            finally:
                MaterialCollector._locate_data_dir = original

    def test_db_matches_persona_id_not_autoincrement_id(self):
        """关键回归：必须按 persona_id 列匹配，不能撞上自增主键 id。

        旧实现把 'id' 排在探测顺序最前，会拿 id=1 的记录（沈星回）去顶替
        请求的 Python 人设，静默取错。
        """
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            self._make_db(data_dir / "data_v4.db")

            # 只有 id 列可用时的场景先排除：本表 persona_id 与 id 值故意不同
            conn = sqlite3.connect(str(data_dir / "data_v4.db"))
            rows = conn.execute("SELECT id, persona_id FROM personas ORDER BY id").fetchall()
            conn.close()
            self.assertEqual(rows, [(1, "sxh"), (2, "Python")])  # 前提校验

            original = MaterialCollector._locate_data_dir
            MaterialCollector._locate_data_dir = staticmethod(lambda: data_dir)
            try:
                collector = MaterialCollector(FakeContext(None), {"persona_id": "sxh"})
                self.assertEqual(_run(collector.get_persona()), "我是沈星回")

                # 换个 ID 必须取到另一条，证明真的按 persona_id 匹配
                collector2 = MaterialCollector(FakeContext(None), {"persona_id": "Python"})
                self.assertEqual(_run(collector2.get_persona()), "你是一个专业 Python 工程师")
            finally:
                MaterialCollector._locate_data_dir = original

    def test_db_never_blind_picks_first_row(self):
        """没有可用的 persona_id 时，数据库兜底应返回空，而不是盲取第一条。"""
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            self._make_db(data_dir / "data_v4.db")
            original = MaterialCollector._locate_data_dir
            MaterialCollector._locate_data_dir = staticmethod(lambda: data_dir)
            try:
                collector = MaterialCollector(FakeContext(None), {})
                self.assertEqual(_run(collector.get_persona()), "")
            finally:
                MaterialCollector._locate_data_dir = original

    def test_api_exception_does_not_break(self):
        """官方 API 抛异常 → 不崩，继续往下回退。"""
        manager = FakePersonaManager(default={"prompt": "默认人格"}, raise_on_by_id=True)
        collector = MaterialCollector(FakeContext(manager), {"persona_id": "sxh"})
        self.assertEqual(_run(collector.get_persona()), "默认人格")

    def test_cache_hit_and_invalidate(self):
        """缓存命中：TTL 内只解析一次；手动失效后重新解析。"""
        manager = FakePersonaManager(default={"prompt": "默认人格"})
        collector = MaterialCollector(FakeContext(manager), {})

        self.assertEqual(_run(collector.get_persona()), "默认人格")
        self.assertEqual(_run(collector.get_persona()), "默认人格")
        self.assertEqual(manager.default_calls, 1, "TTL 内应命中缓存，不该重复解析")

        collector.invalidate_persona_cache()
        self.assertEqual(_run(collector.get_persona()), "默认人格")
        self.assertEqual(manager.default_calls, 2, "失效后应重新解析")

    def test_switch_off_returns_empty(self):
        """关闭「发帖时读取人设」后，应完全不读取人设。"""
        manager = FakePersonaManager(default={"prompt": "默认人格"})
        collector = MaterialCollector(
            FakeContext(manager),
            {"use_persona_for_post": False, "persona_id": "sxh"},
        )
        self.assertEqual(_run(collector.get_persona()), "")
        self.assertEqual(manager.default_calls, 0)
        self.assertEqual(manager.by_id_calls, [])

    def test_extract_prompt_variants(self):
        """_extract_prompt 兼容 dict / 带 get 的对象 / None。"""
        self.assertEqual(MaterialCollector._extract_prompt({"prompt": "  a  "}), "a")
        self.assertEqual(
            MaterialCollector._extract_prompt(types.SimpleNamespace(prompt="b")),
            "b",
        )
        self.assertEqual(MaterialCollector._extract_prompt(None), "")
        self.assertEqual(MaterialCollector._extract_prompt({}), "")

    @staticmethod
    def _make_db(path: Path):
        conn = sqlite3.connect(str(path))
        conn.execute(
            "CREATE TABLE personas ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "persona_id TEXT, "
            "system_prompt TEXT)",
        )
        conn.execute(
            "INSERT INTO personas (id, persona_id, system_prompt) VALUES (1, 'sxh', '我是沈星回')",
        )
        conn.execute(
            "INSERT INTO personas (id, persona_id, system_prompt) "
            "VALUES (2, 'Python', '你是一个专业 Python 工程师')",
        )
        conn.commit()
        conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
