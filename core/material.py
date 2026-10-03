"""AstrBot Moment Plugin — 素材获取层

负责从多个来源收集 AI 发帖的灵感素材：
1. 当前角色的 system_prompt（人设）
2. AstrBot 知识库（knowledge_base）
3. ob_memory 插件的记忆（如果已安装）
"""
from __future__ import annotations

import asyncio
import random
import time
from astrbot.api import logger


class MaterialCollector:
    """从各种来源收集 AI 发帖的灵感素材。"""

    # 人设缓存有效期（秒）
    _PERSONA_CACHE_TTL = 60

    def __init__(self, context, config: dict, meal_bridge=None):
        self.context = context
        self.config = config
        # 三餐桥（只读 xavier_daily_meal 的菜单，按概率给个发帖话题）；可为 None
        self.meal_bridge = meal_bridge
        # 人设解析缓存，避免每次发帖/评论都读盘开库
        self._persona_cache: str | None = None
        self._persona_cache_at: float = 0.0

    async def get_persona(self) -> str:
        """获取当前人设 prompt（公开方法，供评论/回复时注入 system_prompt）。"""
        return await self._get_persona()

    def invalidate_persona_cache(self):
        """清空人设缓存。配置变更后调用，使新配置立即生效。"""
        self._persona_cache = None
        self._persona_cache_at = 0.0

    async def collect(self, db_instance=None, allow_meal: bool = False) -> dict:
        """收集所有可用素材，返回一个字典供 LLM 参考。

        Args:
            db_instance: 数据库实例 (MomentDatabase)，用于获取历史记录等
            allow_meal: 是否允许本次带上三餐话题（只在他自己主动发帖时为 True；
                被要求发帖时不该插一句「我今天吃了啥」）

        Returns:
            {
                "persona": str,           # 人设摘要
                "current_time": str,       # 当前时间信息
                "recent_posts": [],        # 近期发帖记录，用于防重复
                "topic": str,              # 随机抽取的发帖主题
                "meal_topic": str,         # 可选：今天/昨天的某一餐（抽样命中才有）
            }
        """
        import datetime
        now = datetime.datetime.now()

        # 简单判断下当前所处的时段
        hour = now.hour
        if 5 <= hour < 9:
            time_desc = "早上"
        elif 9 <= hour < 12:
            time_desc = "上午"
        elif 12 <= hour < 14:
            time_desc = "中午"
        elif 14 <= hour < 18:
            time_desc = "下午"
        elif 18 <= hour < 22:
            time_desc = "晚上"
        else:
            time_desc = "深夜/凌晨"

        current_time_str = f"现在是 {now.strftime('%Y-%m-%d %H:%M')}，{time_desc}。"

        # 随机抽取主题
        topics_str = self.config.get("random_post_topics", "日常,美食,心情,音乐,阅读,小发现")
        topics = [t.strip() for t in topics_str.split(",") if t.strip()]
        selected_topic = random.choice(topics) if topics else "日常"

        result = {
            "persona": "",
            "current_time": current_time_str,
            "recent_posts": [],
            "topic": selected_topic,
        }

        # 0. 读取历史发帖防重复
        if db_instance:
            try:
                posts = await db_instance.get_posts(limit=5)
                # 过滤出 AI 发的帖子
                ai_posts = [p["content"] for p in posts if p.get("author") == "ai"]
                result["recent_posts"] = ai_posts[:3]
            except Exception as e:
                logger.warning(f"[moment] 获取历史发帖失败: {e}")

        # 1. 读取人设 (system_prompt)
        result["persona"] = await self._get_persona()

        # 2. 三餐话题（只读本地菜单，抽样命中才有；失败不影响发帖）
        if allow_meal and self.meal_bridge is not None:
            try:
                meal_topic = await self.meal_bridge.maybe_topic()
                if meal_topic:
                    result["meal_topic"] = meal_topic
            except Exception:
                logger.exception("[moment] 取三餐话题失败，本次不带")

        return result

    # ==================== 人设解析 ====================

    async def _get_persona(self) -> str:
        """获取人设 prompt，带 60 秒缓存。

        解析优先级：
          1. 插件配置 persona_id  → 官方 API 按 ID 精确获取
          2. AstrBot 当前默认人格  → 官方 API get_default_persona_v3()
          3. 数据库兜底            → 按 persona_id 列精确匹配（不再盲取第一条）

        三级全部失败时返回空字符串并告警，行为等同未开启人设注入，
        不会中断发帖流程。
        """
        # 开关关闭时完全不读取人设（也省掉解析开销），行为等同未开启
        if not self.config.get("use_persona_for_post", True):
            return ""

        now = time.monotonic()
        if (
            self._persona_cache is not None
            and (now - self._persona_cache_at) < self._PERSONA_CACHE_TTL
        ):
            return self._persona_cache

        persona_id = self._configured_persona_id()

        prompt = ""
        # 1. 配置指定的人格（选「默认人格」时值为 'default'，视为跟随默认）
        if persona_id and persona_id != "default":
            prompt = await self._persona_via_api(persona_id)
            if not prompt:
                logger.warning(
                    f"[moment] 配置的人格 '{persona_id}' 未能解析，回退到默认人格",
                )

        # 2. AstrBot 当前默认人格（留空即走这里）
        if not prompt:
            prompt = await self._persona_via_api(None)

        # 3. 数据库兜底
        if not prompt:
            prompt = await self._persona_via_db(persona_id)

        if not prompt:
            logger.warning(
                "[moment] 未能获取到任何人设，本次不注入 system_prompt",
            )

        self._persona_cache = prompt
        self._persona_cache_at = now
        return prompt

    def _configured_persona_id(self) -> str:
        """读取插件配置中指定的 persona ID（留空表示跟随默认人格）。"""
        try:
            value = self.config.get("persona_id", "")
            if value is None:
                return ""
            return str(value).strip()
        except Exception:
            return ""

    async def _persona_via_api(self, persona_id: str | None) -> str:
        """通过 AstrBot 官方 API 获取人设。persona_id 为 None/空时取默认人格。"""
        manager = getattr(self.context, "persona_manager", None)
        if manager is None:
            return ""
        try:
            if persona_id:
                # 同步方法：按 persona name(=persona_id) 匹配
                persona = manager.get_persona_v3_by_id(persona_id)
            else:
                # 异步方法：取当前默认人格
                persona = await manager.get_default_persona_v3()
            prompt = self._extract_prompt(persona)
            if prompt:
                logger.debug(
                    f"[moment] 从官方 API 获取人设 ({len(prompt)} 字)"
                    f"{'，指定 ID=' + persona_id if persona_id else '，默认人格'}",
                )
            return prompt
        except Exception:
            logger.exception("[moment] 通过官方 API 获取人设失败")
            return ""

    @staticmethod
    def _extract_prompt(persona) -> str:
        """从 Personality 对象/dict 中取出 prompt 文本。"""
        if not persona:
            return ""
        try:
            if isinstance(persona, dict):
                value = persona.get("prompt", "")
            elif hasattr(persona, "get"):
                value = persona.get("prompt", "")
            else:
                value = getattr(persona, "prompt", "")
            if not value:
                return ""
            return str(value).strip()
        except Exception:
            return ""

    async def _persona_via_db(self, persona_id: str) -> str:
        """数据库兜底读取人设（官方 API 不可用时）。

        与旧实现的关键差异：
          - 匹配列优先使用 persona_id，而非自增主键 id
          - 不再使用 LIMIT 1 盲取第一条（避免静默取到无关人设）
          - 同步的 sqlite 操作放到线程池，避免阻塞事件循环
        """
        return await asyncio.to_thread(self._persona_via_db_sync, persona_id)

    def _persona_via_db_sync(self, persona_id: str) -> str:
        from pathlib import Path

        try:
            data_dir = self._locate_data_dir()
            if data_dir is None:
                return ""

            pid = persona_id or self._default_persona_id_from_config(data_dir)

            for search_dir in (data_dir, data_dir.parent):
                if not search_dir.exists():
                    continue
                for db_file in search_dir.glob("*.db"):
                    prompt = self._query_persona_table(db_file, pid)
                    if prompt:
                        logger.info(
                            f"[moment] 从数据库兜底获取人设 ({len(prompt)} 字)"
                            f"{'，ID=' + pid if pid else ''} [{db_file.name}]",
                        )
                        return prompt

            logger.warning("[moment] 数据库兜底未能取到人设")
        except Exception as e:
            logger.warning(f"[moment] 数据库兜底读取人设失败: {e}")
        return ""

    @staticmethod
    def _query_persona_table(db_file, persona_id: str) -> str:
        """在单个 db 文件的 personas 表中查询人设 prompt。查不到返回空。"""
        import sqlite3

        conn = None
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='personas';",
            )
            if not cursor.fetchone():
                return ""

            cursor.execute("PRAGMA table_info(personas);")
            columns = [row[1] for row in cursor.fetchall()]

            prompt_col = next(
                (c for c in ("system_prompt", "prompt", "content") if c in columns),
                None,
            )
            if not prompt_col:
                return ""

            # 关键修正：persona_id 优先于自增主键 id
            id_col = next(
                (c for c in ("persona_id", "name", "persona_name") if c in columns),
                None,
            )

            # 有明确 ID 且能找到 ID 列 → 精确匹配（绝不用 id 这种自增主键）
            if persona_id and id_col:
                cursor.execute(
                    f"SELECT {prompt_col} FROM personas WHERE {id_col} = ? LIMIT 1",
                    (persona_id,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()

            # 没有 ID 可匹配时不再盲取第一条，交由上层回退
            return ""
        except Exception:
            return ""
        finally:
            if conn is not None:
                conn.close()

    @staticmethod
    def _locate_data_dir():
        """定位 AstrBot 的 data 目录。"""
        from pathlib import Path

        candidates = [
            Path.cwd() / "data",
            Path(__file__).parent.parent.parent.parent,
        ]
        for candidate in candidates:
            try:
                if candidate.exists() and candidate.is_dir():
                    return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _default_persona_id_from_config(data_dir) -> str:
        """从 config.json 尝试读取默认 persona id（新版本可能已迁移到数据库）。"""
        import json

        config_path = data_dir / "config.json"
        if not config_path.exists():
            return ""
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)

            providers = config_data.get("provider_settings", {})
            for p_config in providers.values():
                if isinstance(p_config, dict) and p_config.get("persona"):
                    return str(p_config["persona"]).strip()

            for key in ("default_persona_id", "default_personality"):
                if config_data.get(key):
                    return str(config_data[key]).strip()

            persona_cfg = config_data.get("persona", {})
            if isinstance(persona_cfg, str):
                return persona_cfg.strip()
            if isinstance(persona_cfg, dict):
                return str(
                    persona_cfg.get("default", "") or persona_cfg.get("current", ""),
                ).strip()
        except Exception:
            return ""
        return ""
