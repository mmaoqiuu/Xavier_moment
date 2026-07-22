"""AstrBot Moment Plugin — 数据库层

管理 posts（帖子）和 comments（评论）的 SQLite 存储。
"""
from __future__ import annotations

import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional


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
                author TEXT NOT NULL,           -- 'ai' 或 'user'
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

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(is_read, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id);
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
        except Exception:
            pass

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
            "SELECT * FROM posts ORDER BY created_at DESC LIMIT ? OFFSET ?",
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

    async def create_comment(self, post_id: int, author: str, content: str, parent_comment_id: int = None) -> dict:
        now = datetime.now().isoformat()
        cursor = await self._conn.execute(
            "INSERT INTO comments (post_id, parent_comment_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (post_id, parent_comment_id, author, content, now),
        )
        await self._conn.commit()
        return {
            "id": cursor.lastrowid,
            "post_id": post_id,
            "parent_comment_id": parent_comment_id,
            "author": author,
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
