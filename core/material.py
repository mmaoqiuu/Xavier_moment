"""AstrBot Moment Plugin — 素材获取层

负责从多个来源收集 AI 发帖的灵感素材：
1. 当前角色的 system_prompt（人设）
2. AstrBot 知识库（knowledge_base）
3. ob_memory 插件的记忆（如果已安装）
"""
from __future__ import annotations

import random
from typing import Optional
from astrbot.api import logger


class MaterialCollector:
    """从各种来源收集 AI 发帖的灵感素材。"""

    def __init__(self, context, config: dict):
        self.context = context
        self.config = config

    async def get_persona(self) -> str:
        """获取当前人设 prompt（公开方法，供评论/回复时注入 system_prompt）。"""
        return await self._get_persona()

    async def collect(self, db_instance=None) -> dict:
        """收集所有可用素材，返回一个字典供 LLM 参考。

        Args:
            db_instance: 数据库实例 (MomentDatabase)，用于获取历史记录等

        Returns:
            {
                "persona": str,           # 人设摘要
                "current_time": str,       # 当前时间信息
                "recent_posts": [],        # 近期发帖记录，用于防重复
                "topic": str,              # 随机抽取的发帖主题
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

        return result

    async def _get_persona(self) -> str:
        """从 AstrBot 的 SQLite 数据库 personas 表中读取当前默认人设的 prompt。"""
        try:
            import sqlite3
            import json
            from pathlib import Path
            import sys
            
            # 使用更基础的方法获取 data_dir，避免 import StarTools 导致的元数据解析失败
            # AstrBot 的启动入口通常在根目录，所以 sys.path[0] 或者当前工作目录通常是 AstrBot-master
            base_dir = Path.cwd()
            data_dir = base_dir / "data"
            if not data_dir.exists() or not data_dir.is_dir():
                # 尝试从当前文件位置推断: plugins/astrbot_plugin_xavier_moment/core/material.py -> data/
                data_dir = Path(__file__).parent.parent.parent.parent
            
            if not data_dir.exists():
                logger.warning(f"[moment] 无法定位 data 目录: {data_dir}")
                return ""
            
            # ===== 第一步：从 config.json 读取当前启用的 persona_id =====
            config_path = data_dir / "config.json"
            persona_id = ""
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                    
                    # 尝试从 provider_settings 读取
                    providers = config_data.get("provider_settings", {})
                    for p_config in providers.values():
                        if isinstance(p_config, dict) and "persona" in p_config:
                            pid = p_config.get("persona", "")
                            if pid:
                                persona_id = pid
                                break
                                
                    # 尝试从全局 persona / default_persona_id 读取
                    if not persona_id:
                        persona_id = config_data.get("default_persona_id", "")
                    if not persona_id:
                        persona_cfg = config_data.get("persona", {})
                        if isinstance(persona_cfg, str):
                            persona_id = persona_cfg
                        elif isinstance(persona_cfg, dict):
                            persona_id = persona_cfg.get("default", "") or persona_cfg.get("current", "")
                            
            if persona_id:
                logger.debug(f"[moment] 当前启用的角色 ID: {persona_id}")
            
            # ===== 第二步：从 SQLite 数据库的 personas 表中查询 prompt =====
            # 扫描 data 目录下所有 .db 文件，找到含有 personas 表的那个
            db_search_dirs = [data_dir, data_dir.parent]
            found_prompt = ""
            
            for search_dir in db_search_dirs:
                if not search_dir.exists():
                    continue
                for db_file in search_dir.glob("*.db"):
                    try:
                        conn = sqlite3.connect(str(db_file))
                        cursor = conn.cursor()
                        
                        # 检查是否有 personas 表
                        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='personas';")
                        if not cursor.fetchone():
                            conn.close()
                            continue
                        
                        logger.debug(f"[moment] 在 {db_file.name} 中发现 personas 表")
                        
                        # 查询所有列名，确定 prompt 字段叫什么
                        cursor.execute("PRAGMA table_info(personas);")
                        columns = [row[1] for row in cursor.fetchall()]
                        
                        # 确定 prompt 字段名（可能是 prompt / system_prompt / content）
                        prompt_col = None
                        for candidate in ["prompt", "system_prompt", "content"]:
                            if candidate in columns:
                                prompt_col = candidate
                                break
                        
                        if not prompt_col:
                            conn.close()
                            continue
                        
                        # 确定 id / name 字段名
                        id_col = None
                        for candidate in ["id", "name", "persona_id", "persona_name"]:
                            if candidate in columns:
                                id_col = candidate
                                break
                        
                        # 如果有 persona_id，精确查询
                        if persona_id and id_col:
                            cursor.execute(f"SELECT {prompt_col} FROM personas WHERE {id_col} = ?", (persona_id,))
                            row = cursor.fetchone()
                            if row and row[0]:
                                found_prompt = row[0]
                                logger.info(f"[moment] 从数据库精确匹配到角色 ({persona_id}) 的人设 prompt ({len(found_prompt)} 字)")
                                conn.close()
                                return found_prompt
                        
                        # 如果精确匹配失败，取第一条记录作为兜底
                        cursor.execute(f"SELECT {prompt_col} FROM personas LIMIT 1")
                        row = cursor.fetchone()
                        if row and row[0]:
                            found_prompt = row[0]
                            logger.info(f"[moment] 兜底获取到人设 prompt ({len(found_prompt)} 字)")
                            conn.close()
                            return found_prompt
                            
                        conn.close()
                        
                    except Exception as db_err:
                        # 忽略无法读取的 db 文件
                        continue
            
            if not found_prompt:
                logger.warning("[moment] 未能从任何数据库中获取到人设 prompt")
                
        except Exception as e:
            logger.warning(f"[moment] 获取人设失败: {e}")
            
        return ""

