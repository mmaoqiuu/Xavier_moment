"""AstrBot Moment Plugin — 数据库层

管理 posts（帖子）和 comments（评论）的 SQLite 存储。
"""
from __future__ import annotations

import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional

from astrbot.api import logger


class MomentDatabase:
    """异步 SQLite 数据库管理器。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(str(self.db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()
        await self._migrate()

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def _create_tables(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                author TEXT NOT NULL,           -- 'ai' 或 'user'
                content TEXT NOT NULL,
                mood TEXT DEFAULT '',           -- 心情标签（开心/感慨/日常等）
                images TEXT DEFAULT '',         -- 逗号分隔的图片文件名
                source_hint TEXT DEFAULT '',    -- 灵感来源提示（不展示给用户，内部用）
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                parent_comment_id INTEGER DEFAULT NULL,  -- 回复某条评论时填写
                author TEXT NOT NULL,           -- 'ai' / 'user' / 'npc'
                author_name TEXT DEFAULT '',    -- author='npc' 时的昵称（如「邱诺亚」）
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_comment_id) REFERENCES comments(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,             -- 'new_post' / 'new_comment' / 'new_reply'
                ref_post_id INTEGER,
                ref_comment_id INTEGER,
                message TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                author TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS likes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id INTEGER NOT NULL,
                author TEXT NOT NULL DEFAULT 'user',    -- 'user' / 'ai' / 'npc'
                author_name TEXT NOT NULL DEFAULT '',   -- author='npc' 时的昵称（如「陶桃」）
                created_at TEXT NOT NULL,
                FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE,
                UNIQUE (post_id, author, author_name)   -- 同一人对同一条动态只算一次
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(is_read, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id);
            CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
        """)
        await self._conn.commit()

    async def _migrate(self):
        """数据库迁移：为旧版本数据库添加缺失的列。"""
        try:
            # 检查 posts 表是否有 images 列
            cursor = await self._conn.execute("PRAGMA table_info(posts)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "images" not in columns:
                await self._conn.execute("ALTER TABLE posts ADD COLUMN images TEXT DEFAULT ''")
                await self._conn.commit()

            # 检查 comments 表是否有 author_name 列（v0.2.0 起支持 NPC 评论）
            cursor = await self._conn.execute("PRAGMA table_info(comments)")
            comment_columns = [row[1] for row in await cursor.fetchall()]
            if "author_name" not in comment_columns:
                await self._conn.execute("ALTER TABLE comments ADD COLUMN author_name TEXT DEFAULT ''")
                await self._conn.commit()
                logger.info("[moment] 数据库迁移：comments 表已添加 author_name 列")
        except Exception as e:
            logger.warning(f"[moment] 数据库迁移失败（不影响启动）: {e}")

    # ------------------------------------------------------------------
    # Posts CRUD
    # ------------------------------------------------------------------

    async def create_post(self, author: str, content: str, mood: str = "", source_hint: str = "", images: str = "") -> dict:
        now = datetime.now().isoformat()
        cursor = await self._conn.execute(
            "INSERT INTO posts (author, content, mood, images, source_hint, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (author, content, mood, images, source_hint, now, now),
        )
        await self._conn.commit()
        return {
            "id": cursor.lastrowid,
            "author": author,
            "content": content,
            "mood": mood,
            "images": images,
            "created_at": now,
        }

    async def get_posts(self, limit: int = 50, offset: int = 0) -> list[dict]:
        cursor = await self._conn.execute(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count
            FROM posts p
            ORDER BY p.created_at DESC LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_post(self, post_id: int) -> Optional[dict]:
        cursor = await self._conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def delete_post(self, post_id: int) -> bool:
        cursor = await self._conn.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    async def count_posts(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM posts")
        row = await cursor.fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Comments CRUD
    # ------------------------------------------------------------------

    async def create_comment(
        self,
        post_id: int,
        author: str,
        content: str,
        parent_comment_id: int = None,
        author_name: str = "",
    ) -> dict:
        now = datetime.now().isoformat()
        cursor = await self._conn.execute(
            "INSERT INTO comments (post_id, parent_comment_id, author, author_name, content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (post_id, parent_comment_id, author, author_name or "", content, now),
        )
        await self._conn.commit()
        return {
            "id": cursor.lastrowid,
            "post_id": post_id,
            "parent_comment_id": parent_comment_id,
            "author": author,
            "author_name": author_name or "",
            "content": content,
            "created_at": now,
        }

    async def get_comments(self, post_id: int) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM comments WHERE post_id = ? ORDER BY created_at ASC",
            (post_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_comment(self, comment_id: int) -> bool:
        cursor = await self._conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        await self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------

    async def create_notification(self, ntype: str, message: str, ref_post_id: int = None, ref_comment_id: int = None) -> dict:
        now = datetime.now().isoformat()
        cursor = await self._conn.execute(
            "INSERT INTO notifications (type, ref_post_id, ref_comment_id, message, is_read, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (ntype, ref_post_id, ref_comment_id, message, now),
        )
        await self._conn.commit()
        return {"id": cursor.lastrowid, "type": ntype, "message": message, "created_at": now}

    async def get_unread_notifications(self) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM notifications WHERE is_read = 0 ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_notifications_read(self):
        await self._conn.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
        await self._conn.commit()

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    async def add_reaction(self, post_id: int, emoji: str, author: str = "user"):
        now = datetime.now().isoformat()
        await self._conn.execute(
            "INSERT INTO reactions (post_id, emoji, author, created_at) VALUES (?, ?, ?, ?)",
            (post_id, emoji, author, now),
        )
        await self._conn.commit()

    async def remove_reaction(self, post_id: int, emoji: str, author: str = "user"):
        await self._conn.execute(
            "DELETE FROM reactions WHERE post_id = ? AND emoji = ? AND author = ?",
            (post_id, emoji, author),
        )
        await self._conn.commit()

    async def get_reactions(self, post_id: int) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT emoji, author FROM reactions WHERE post_id = ?", (post_id,)
        )
        rows = await cursor.fetchall()
        # 按 emoji 分组
        grouped = {}
        for row in rows:
            emoji = row[0]
            author = row[1]
            if emoji not in grouped:
                grouped[emoji] = {"emoji": emoji, "count": 0, "authors": []}
            grouped[emoji]["count"] += 1
            grouped[emoji]["authors"].append(author)
        return list(grouped.values())

    async def get_reactions_for_posts(self, post_ids: list[int]) -> dict:
        """批量取多帖的表情，避免时间线一条一条查。返回 {post_id: [分组后的表情]}。"""
        if not post_ids:
            return {}
        placeholders = ",".join("?" for _ in post_ids)
        cursor = await self._conn.execute(
            f"SELECT post_id, emoji, author FROM reactions WHERE post_id IN ({placeholders})",
            tuple(post_ids),
        )
        rows = await cursor.fetchall()
        grouped: dict[int, dict] = {}
        for row in rows:
            per_post = grouped.setdefault(row["post_id"], {})
            item = per_post.setdefault(row["emoji"], {"emoji": row["emoji"], "count": 0, "authors": []})
            item["count"] += 1
            item["authors"].append(row["author"])
        return {post_id: list(items.values()) for post_id, items in grouped.items()}

    # ------------------------------------------------------------------
    # Likes（点赞：「谁赞了」，与表情贴纸是两回事）
    # ------------------------------------------------------------------

    async def add_like(self, post_id: int, author: str = "user", author_name: str = "") -> bool:
        """点赞。重复点赞会被唯一索引挡掉（INSERT OR IGNORE），返回是否新插入。"""
        now = datetime.now().isoformat()
        cursor = await self._conn.execute(
            "INSERT OR IGNORE INTO likes (post_id, author, author_name, created_at) VALUES (?, ?, ?, ?)",
            (post_id, author, author_name or "", now),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def remove_like(self, post_id: int, author: str = "user", author_name: str = "") -> bool:
        cursor = await self._conn.execute(
            "DELETE FROM likes WHERE post_id = ? AND author = ? AND author_name = ?",
            (post_id, author, author_name or ""),
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    async def has_like(self, post_id: int, author: str = "user", author_name: str = "") -> bool:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM likes WHERE post_id = ? AND author = ? AND author_name = ?",
            (post_id, author, author_name or ""),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def toggle_like(self, post_id: int, author: str = "user", author_name: str = "") -> bool:
        """切换点赞状态，返回切换之后是否处于已赞。"""
        if await self.has_like(post_id, author, author_name):
            await self.remove_like(post_id, author, author_name)
            return False
        await self.add_like(post_id, author, author_name)
        return True

    async def get_likes(self, post_id: int) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT author, author_name, created_at FROM likes WHERE post_id = ? ORDER BY created_at ASC",
            (post_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_likes_for_posts(self, post_ids: list[int]) -> dict:
        """批量取多帖的点赞记录。返回 {post_id: [点赞行]}。"""
        if not post_ids:
            return {}
        placeholders = ",".join("?" for _ in post_ids)
        cursor = await self._conn.execute(
            f"SELECT post_id, author, author_name, created_at FROM likes WHERE post_id IN ({placeholders}) ORDER BY created_at ASC",
            tuple(post_ids),
        )
        rows = await cursor.fetchall()
        grouped: dict[int, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row["post_id"], []).append(dict(row))
        return grouped

    async def get_comment(self, comment_id: int) -> Optional[dict]:
        cursor = await self._conn.execute("SELECT * FROM comments WHERE id = ?", (comment_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_npc_comments(self, post_id: int, author_name: str) -> int:
        """某个 NPC 在这条动态下已经评论过几次（用于防止同一人反复刷）。"""
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ? AND author = 'npc' AND author_name = ?",
            (post_id, author_name),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def has_ai_reply_to(self, comment_id: int) -> bool:
        """某条评论是否已经被回复过（防止同一条被回两遍）。"""
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM comments WHERE parent_comment_id = ? AND author = 'ai'",
            (comment_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def get_activity_since(self, since_iso: str, limit: int = 20) -> list[dict]:
        """按时间正序返回 since 之后的新动态与新评论，供「朋友圈 → 聊天」桥使用。

        一次查询把帖子和评论合并成统一的事件流，避免两表分别取再合并的复杂度。
        """
        cursor = await self._conn.execute(
            """
            SELECT 'post' AS kind, p.id AS id, p.author AS author, '' AS author_name,
                   p.content AS content, p.created_at AS created_at,
                   NULL AS post_id, NULL AS parent_comment_id
            FROM posts p
            WHERE p.created_at > ?
            UNION ALL
            SELECT 'comment' AS kind, c.id AS id, c.author AS author,
                   COALESCE(c.author_name, '') AS author_name, c.content AS content,
                   c.created_at AS created_at, c.post_id AS post_id,
                   c.parent_comment_id AS parent_comment_id
            FROM comments c
            WHERE c.created_at > ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (since_iso, since_iso, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def get_setting(self, key: str, default: str = "") -> str:
        cursor = await self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else default

    async def set_setting(self, key: str, value: str):
        await self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self._conn.commit()
