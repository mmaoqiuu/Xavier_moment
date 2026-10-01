"""AstrBot Moment Plugin — 数据库层

管理 posts（帖子）和 comments（评论）的 SQLite 存储，
以及 jobs（延迟任务待办）——待办落库后，插件重载也丢不了。
"""
from __future__ import annotations

import json
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta
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

            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,                     -- 任务类型：npc_comment / ai_like ...
                post_id INTEGER DEFAULT NULL,           -- 关联动态（便于按动态查待办）
                payload TEXT NOT NULL DEFAULT '{}',     -- 任务参数（JSON）
                dedup_key TEXT NOT NULL DEFAULT '',     -- 同一件事的唯一键，防止重复排队
                due_at TEXT NOT NULL,                   -- 计划执行时间
                status TEXT NOT NULL DEFAULT 'pending', -- pending / done / failed / expired
                attempts INTEGER NOT NULL DEFAULT 0,    -- 已经尝试过几次（含被重载打断的）
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_unread ON notifications(is_read, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reactions_post ON reactions(post_id);
            CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_dedup ON jobs(dedup_key) WHERE dedup_key <> '';
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, due_at);
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

    async def count_npc_comments_total(self, post_id: int) -> int:
        """这条动态下一共有多少条 NPC 评论（重载补跑时判断「这波是不是已经来过了」）。"""
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ? AND author = 'npc'",
            (post_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_ai_replies_to_npc(self, post_id: int) -> int:
        """他在这条动态下回过 NPC 几条（只数回复 NPC 的，不含他自己开的楼）。"""
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM comments c
               JOIN comments p ON c.parent_comment_id = p.id
               WHERE c.post_id = ? AND c.author = 'ai' AND p.author = 'npc'""",
            (post_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def has_ai_comment(self, post_id: int) -> bool:
        """他是否已经在这条动态下评论过（不含他回复别人的那些）。"""
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM comments WHERE post_id = ? AND author = 'ai' AND parent_comment_id IS NULL",
            (post_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

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

    # ------------------------------------------------------------------
    # Jobs（延迟任务待办：重载后接着跑）
    # ------------------------------------------------------------------

    async def add_job(
        self,
        kind: str,
        payload: dict,
        due_at: str,
        dedup_key: str = "",
        post_id: Optional[int] = None,
    ) -> Optional[int]:
        """登记一条待办，返回任务 id；同一件事已经在排队时返回 None。

        dedup_key 相同表示「同一件事」：
          · 还在 pending → 不重复排队（返回 None）
          · 已经跑完 / 失败 / 过期 → 重置成新的待办（重新排一次）
        """
        now = datetime.now().isoformat()
        text = json.dumps(payload or {}, ensure_ascii=False)
        cursor = await self._conn.execute(
            """INSERT OR IGNORE INTO jobs
               (kind, post_id, payload, dedup_key, due_at, status, attempts, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?)""",
            (kind, post_id, text, dedup_key or "", due_at, now, now),
        )
        if cursor.rowcount > 0:
            await self._conn.commit()
            return cursor.lastrowid

        if not dedup_key:
            await self._conn.commit()
            return None

        cursor = await self._conn.execute(
            "SELECT id, status FROM jobs WHERE dedup_key = ?", (dedup_key,)
        )
        row = await cursor.fetchone()
        if row is None:
            await self._conn.commit()
            return None
        if row["status"] == "pending":
            await self._conn.commit()
            return None
        await self._conn.execute(
            """UPDATE jobs SET status = 'pending', due_at = ?, attempts = 0,
                      last_error = '', updated_at = ? WHERE id = ?""",
            (due_at, now, row["id"]),
        )
        await self._conn.commit()
        return row["id"]

    async def get_pending_jobs(self, limit: int = 500) -> list[dict]:
        """所有还没跑完的待办，按计划时间从早到晚。"""
        cursor = await self._conn.execute(
            "SELECT * FROM jobs WHERE status = 'pending' ORDER BY due_at ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def mark_job(self, job_id: int, status: str, error: str = "", bump_attempts: bool = False) -> None:
        """更新待办状态：done（跑完）/ failed（出错）/ expired（过期丢弃）。"""
        now = datetime.now().isoformat()
        await self._conn.execute(
            """UPDATE jobs SET status = ?, last_error = ?,
                      attempts = attempts + ?, updated_at = ? WHERE id = ?""",
            (status, (error or "")[:500], 1 if bump_attempts else 0, now, job_id),
        )
        await self._conn.commit()

    async def count_pending_jobs(self, kind: str = "", post_id: Optional[int] = None) -> int:
        """还没跑的待办数量，可按类型 / 动态过滤。"""
        sql = "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
        args: list = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if post_id is not None:
            sql += " AND post_id = ?"
            args.append(post_id)
        cursor = await self._conn.execute(sql, tuple(args))
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def expire_all_pending_jobs(self) -> int:
        """把所有没跑的待办标成过期（续跑关闭时用），返回处理条数。"""
        now = datetime.now().isoformat()
        cursor = await self._conn.execute(
            "UPDATE jobs SET status = 'expired', updated_at = ? WHERE status = 'pending'",
            (now,),
        )
        await self._conn.commit()
        return cursor.rowcount or 0

    async def purge_jobs(self, keep_days: int = 7) -> int:
        """清掉已经跑完/失败/过期的历史待办，只留最近 keep_days 天的。"""
        if keep_days < 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=keep_days)).isoformat()
        cursor = await self._conn.execute(
            "DELETE FROM jobs WHERE status <> 'pending' AND updated_at <= ?", (cutoff,)
        )
        await self._conn.commit()
        return cursor.rowcount or 0

