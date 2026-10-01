"""AstrBot Moment Plugin — 自托管 HTTP 服务器

使用 aiohttp 提供朋友圈页面和全部 API 端点。
插件初始化时始终启动，不依赖 AstrBot 的 register_web_api。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from astrbot.api import logger

from .image_utils import shrink_to_jpeg


# 超过这个体积的上传图才会被重新压缩。
# 前端已经压到一两百 KB，正常发图不会触发；只有绕过前端、
# 或前端那条 canvas 路径失败时（HEIC 等）才会走到这里。
COMPRESS_MIN_BYTES = 300 * 1024

# 动图不能转 JPEG，原样保存
NON_COMPRESSIBLE_EXTS = {"gif"}


class MomentServer:
    """自托管 aiohttp 服务器，提供朋友圈页面和 API。"""

    def __init__(self, plugin):
        """
        Args:
            plugin: MomentPlugin 实例（用于访问 db、config、post_engine 等）
        """
        self.plugin = plugin
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._password: str = ""

        # 页面文件路径（插件源代码目录下）
        self._pages_dir = Path(__file__).parent.parent / "pages" / "timeline"

    async def start(self, host: str = "0.0.0.0", port: int = 2141, password: str = ""):
        """启动 HTTP 服务器。"""
        self._password = password.strip()

        # 手机直出的照片转成 base64 后轻松超过 1MB，aiohttp 的默认上限（1MB）会把
        # 上传直接顶回 413，而前端只往 console 里写一行，看起来就是「点了发布没反应」。
        # 这里放宽到 20MB 兜底，图片体积本身由前端压缩控制。
        app = web.Application(
            middlewares=[self._cors_middleware],
            client_max_size=20 * 1024 * 1024,
        )

        # 注册路由
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/posts", self._handle_get_posts)
        app.router.add_get("/api/posts/detail", self._handle_get_post_detail)
        app.router.add_post("/api/posts/create", self._handle_create_post)
        app.router.add_post("/api/posts/delete", self._handle_delete_post)
        app.router.add_get("/api/comments", self._handle_get_comments)
        app.router.add_post("/api/comments/create", self._handle_create_comment)
        app.router.add_post("/api/comments/delete", self._handle_delete_comment)
        app.router.add_get("/api/notifications", self._handle_get_notifications)
        app.router.add_post("/api/notifications/read", self._handle_mark_read)
        app.router.add_get("/api/stats", self._handle_get_stats)
        app.router.add_post("/api/reactions/toggle", self._handle_toggle_reaction)
        app.router.add_post("/api/likes/toggle", self._handle_toggle_like)
        app.router.add_post("/api/upload", self._handle_upload_image)
        app.router.add_post("/api/upload/cover", self._handle_upload_cover)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_post("/api/login", self._handle_login)
        app.router.add_get("/api/settings", self._handle_get_settings)
        app.router.add_post("/api/settings/update", self._handle_update_settings)
        app.router.add_get("/images/{filename}", self._handle_serve_image)

        # 图标等静态资源。仅供页面引用，不含用户数据，因此不受密码中间件拦截。
        assets_dir = self._pages_dir / "assets"
        if assets_dir.is_dir():
            app.router.add_static("/static/", assets_dir, name="static")
        # 浏览器与 iOS 会在根路径直接请求下面这些固定名字，做一层别名指向 assets。
        # apple-touch-icon-precomposed.png 是 iOS 6 及更早的旧名字，仍会被请求。
        app.router.add_get("/favicon.ico", self._handle_favicon)
        app.router.add_get("/favicon.png", self._handle_favicon)
        app.router.add_get("/apple-touch-icon.png", self._handle_apple_touch_icon)
        app.router.add_get("/apple-touch-icon-precomposed.png", self._handle_apple_touch_icon)
        app.router.add_get("/manifest.webmanifest", self._handle_manifest)

        # OPTIONS 预检请求（CORS）
        app.router.add_route("OPTIONS", "/{path_info:.*}", self._handle_options)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

        display_host = "localhost" if host in ("127.0.0.1", "::1") else host
        logger.info(f"[moment] HTTP 服务已启动: http://{display_host}:{port}/")

    async def stop(self):
        """停止服务器。"""
        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("[moment] HTTP 服务已停止")

    # ------------------------------------------------------------------
    # 中间件
    # ------------------------------------------------------------------

    @web.middleware
    async def _cors_middleware(self, request: web.Request, handler):
        """添加 CORS 头 + 可选密码校验。"""
        # 密码校验
        if self._password:
            # 跳过 OPTIONS 预检请求
            if request.method == "OPTIONS":
                return await handler(request)
            
            # 跳过登录接口本身
            if request.path == "/api/login":
                return await handler(request)
            
            # 对首页返回登录页面（如果没有 cookie 认证）
            if request.path == "/":
                auth_cookie = request.cookies.get("moment_auth", "")
                if auth_cookie != hashlib.md5(self._password.encode()).hexdigest():
                    return web.Response(
                        text=self._login_page_html(),
                        content_type="text/html",
                        headers=self._cors_headers(),
                    )
            
            # 对 API 请求校验密码（通过 cookie 或 header 或 query）
            if request.path.startswith("/api"):
                auth_cookie = request.cookies.get("moment_auth", "")
                expected_hash = hashlib.md5(self._password.encode()).hexdigest()
                pwd = request.query.get("pwd", "") or request.headers.get("X-Moment-Password", "")
                
                if auth_cookie != expected_hash and pwd != self._password:
                    return web.json_response(
                        {"error": "密码错误", "code": 401},
                        status=401,
                        headers=self._cors_headers(),
                    )

        response = await handler(request)
        response.headers.update(self._cors_headers())
        return response

    def _login_page_html(self) -> str:
        """返回一个简洁的密码登录页。"""
        return '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Moment - 登录</title>
<link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32-v2.png">
<link rel="apple-touch-icon" sizes="180x180" href="/static/icon-180-v2.png">
<meta name="theme-color" content="#1d3f66">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, system-ui, sans-serif; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); height: 100vh; display: flex; align-items: center; justify-content: center; }
.login-card { background: #fff; border-radius: 16px; padding: 40px 32px; width: 90%; max-width: 360px; box-shadow: 0 20px 60px rgba(0,0,0,0.2); text-align: center; }
.login-card h2 { font-size: 24px; margin-bottom: 8px; color: #333; }
.login-card p { font-size: 14px; color: #888; margin-bottom: 24px; }
.login-card input { width: 100%; padding: 12px 16px; border: 1px solid #e0e0e0; border-radius: 8px; font-size: 16px; outline: none; transition: border-color 0.2s; }
.login-card input:focus { border-color: #764ba2; }
.login-card button { width: 100%; margin-top: 16px; padding: 12px; background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; transition: opacity 0.2s; }
.login-card button:hover { opacity: 0.9; }
.login-card .error { color: #e74c3c; font-size: 13px; margin-top: 12px; display: none; }
</style>
</head>
<body>
<div class="login-card">
<h2>✨ Moment</h2>
<p>请输入访问密码</p>
<input type="password" id="pwdInput" placeholder="密码" autofocus>
<button onclick="doLogin()">进入</button>
<div class="error" id="errMsg">密码错误，请重试</div>
</div>
<script>
document.getElementById('pwdInput').addEventListener('keydown', e => { if(e.key==='Enter') doLogin(); });
async function doLogin() {
    const pwd = document.getElementById('pwdInput').value;
    const res = await fetch('/api/login', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({password: pwd}) });
    const data = await res.json();
    if (data.success) { location.reload(); }
    else { document.getElementById('errMsg').style.display = 'block'; }
}
</script>
</body>
</html>'''

    @staticmethod
    def _cors_headers() -> dict:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-Moment-Password",
        }

    async def _handle_options(self, request: web.Request) -> web.Response:
        """处理 CORS 预检请求。"""
        return web.Response(status=204, headers=self._cors_headers())

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        """返回主页面 HTML。"""
        html_path = self._pages_dir / "index.html"
        if not html_path.exists():
            return web.Response(text="页面文件缺失", status=500)

        html = html_path.read_text(encoding="utf-8")
        resp = web.Response(text=html, content_type="text/html")
        resp.headers.update(self._cors_headers())
        return resp

    async def _handle_apple_touch_icon(self, request: web.Request) -> web.Response:
        """返回 iOS 主屏幕图标。用 180x180 那份，是 iOS 的推荐尺寸。"""
        return self._serve_asset("icon-180-v2.png", "image/png")

    async def _handle_favicon(self, request: web.Request) -> web.Response:
        """返回 favicon.ico。名字由调用方写死，不接受外部传入。"""
        return self._serve_asset("favicon.ico", "image/vnd.microsoft.icon")

    async def _handle_manifest(self, request: web.Request) -> web.Response:
        """返回 Web App Manifest，供「添加到主屏幕」使用。"""
        return self._serve_asset("manifest.webmanifest", "application/manifest+json")

    def _serve_asset(self, filename: str, content_type: str) -> web.Response:
        """读取 assets 下的固定文件。文件名由调用方写死，无需做穿越校验。"""
        path = self._pages_dir / "assets" / filename
        if not path.is_file():
            return web.Response(text="资源缺失", status=404)
        resp = web.Response(body=path.read_bytes(), content_type=content_type)
        resp.headers.update(self._cors_headers())
        # 图标会被 iOS 与浏览器长期缓存，换素材后必须让它重新拉取。
        resp.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        return resp

    # ------------------------------------------------------------------
    # API: Posts
    # ------------------------------------------------------------------

    async def _handle_get_posts(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
        posts = await self.plugin.db.get_posts(limit=limit, offset=offset)
        total = await self.plugin.db.count_posts()
        await self._attach_engagement(posts)
        return web.json_response({"posts": posts, "total": total})

    # ------------------------------------------------------------------
    # 点赞 / 表情：批量挂到动态上，前端一次拉取就能画完
    # ------------------------------------------------------------------

    def _like_payload(self, rows: list[dict]) -> dict:
        """把点赞行整理成前端要的样子：总数、我赞没赞、其他人的名字。"""
        ai_name = self.plugin.config.get("ai_name", "") or "他"
        names: list[str] = []
        liked_by_me = False
        for row in rows:
            author = row.get("author", "")
            if author == "user":
                liked_by_me = True
            elif author == "npc":
                names.append(row.get("author_name") or "朋友")
            elif author == "ai":
                names.append(ai_name)
        return {"count": len(rows), "liked_by_me": liked_by_me, "names": names}

    async def _attach_engagement(self, posts: list[dict]) -> None:
        """给一批动态补上 likes / reactions 字段（在函数内就地修改）。"""
        if not posts:
            return
        post_ids = [p["id"] for p in posts if p.get("id") is not None]
        if not post_ids:
            return
        try:
            likes_map = await self.plugin.db.get_likes_for_posts(post_ids)
            reactions_map = await self.plugin.db.get_reactions_for_posts(post_ids)
        except Exception:
            logger.exception("[moment] 读取点赞/表情失败")
            return
        for post in posts:
            post["likes"] = self._like_payload(likes_map.get(post["id"], []))
            post["reactions"] = reactions_map.get(post["id"], [])

    async def _handle_get_post_detail(self, request: web.Request) -> web.Response:
        post_id = request.query.get("id")
        if not post_id:
            return web.json_response({"error": "缺少 id 参数"}, status=400)
        post = await self.plugin.db.get_post(int(post_id))
        if not post:
            return web.json_response({"error": "帖子不存在"}, status=404)
        comments = await self.plugin.db.get_comments(int(post_id))
        await self._attach_engagement([post])
        return web.json_response({"post": post, "comments": comments})

    async def _handle_create_post(self, request: web.Request) -> web.Response:
        data = await request.json()
        content = data.get("content", "").strip()
        if not content:
            return web.json_response({"error": "内容不能为空"}, status=400)
        mood = data.get("mood", "")
        images = data.get("images", "")
        post = await self.plugin.db.create_post(
            author="user", content=content, mood=mood, images=images
        )

        # 触发 AI 评论（延迟执行）
        self.plugin._trigger_ai_comment(post["id"], content)
        # 他发的动态和你的动态一样，都会有 NPC 来评论
        self.plugin._trigger_npc_comments(post["id"])
        # 点赞是另一条独立的线：他可能赞你，NPC 也可能赞
        await self.plugin._trigger_likes(post["id"], "user")

        return web.json_response({"post": post})

    async def _handle_delete_post(self, request: web.Request) -> web.Response:
        data = await request.json()
        post_id = data.get("id")
        if not post_id:
            return web.json_response({"error": "缺少 id"}, status=400)
        ok = await self.plugin.db.delete_post(post_id)
        return web.json_response({"success": ok})

    # ------------------------------------------------------------------
    # API: Comments
    # ------------------------------------------------------------------

    async def _handle_get_comments(self, request: web.Request) -> web.Response:
        post_id = request.query.get("post_id")
        comments = await self.plugin.db.get_comments(int(post_id)) if post_id else []
        return web.json_response({"comments": comments})

    async def _handle_create_comment(self, request: web.Request) -> web.Response:
        data = await request.json()
        post_id = data.get("post_id")
        content = data.get("content", "").strip()
        parent_id = data.get("parent_comment_id")

        if not post_id or not content:
            return web.json_response({"error": "post_id 和 content 不能为空"}, status=400)

        comment = await self.plugin.db.create_comment(
            post_id=post_id,
            author="user",
            content=content,
            parent_comment_id=parent_id,
        )

        # 触发 AI 回复
        post = await self.plugin.db.get_post(post_id)
        if post:
            self.plugin._trigger_ai_reply(post_id, post["content"], comment, parent_id)

        # 你在评论区说话，NPC 可能来接一句
        self.plugin._trigger_npc_reply(post_id, comment)

        return web.json_response({"comment": comment})

    async def _handle_delete_comment(self, request: web.Request) -> web.Response:
        """删除自己的评论（只有 author='user' 的能删，删掉后子回复一并消失）。"""
        data = await request.json()
        comment_id = data.get("comment_id")
        if not comment_id:
            return web.json_response({"error": "缺少 comment_id"}, status=400)

        comment = await self.plugin.db.get_comment(int(comment_id))
        if not comment:
            return web.json_response({"error": "评论不存在"}, status=404)
        if comment.get("author") != "user":
            return web.json_response({"error": "只能删除自己的评论"}, status=403)

        ok = await self.plugin.db.delete_comment(int(comment_id))
        return web.json_response({
            "success": bool(ok),
            "id": int(comment_id),
            "post_id": comment.get("post_id"),
        })

    # ------------------------------------------------------------------
    # API: Likes
    # ------------------------------------------------------------------

    async def _handle_toggle_like(self, request: web.Request) -> web.Response:
        """你点赞/取消赞。返回这条动态最新的点赞状态。"""
        data = await request.json()
        post_id = data.get("post_id")
        if not post_id:
            return web.json_response({"error": "缺少 post_id"}, status=400)

        try:
            await self.plugin.db.toggle_like(int(post_id), author="user")
            rows = await self.plugin.db.get_likes(int(post_id))
        except Exception:
            logger.exception("[moment] 点赞失败")
            return web.json_response({"error": "操作失败"}, status=500)

        return web.json_response({"likes": self._like_payload(rows)})

    # ------------------------------------------------------------------
    # API: Notifications
    # ------------------------------------------------------------------

    async def _handle_get_notifications(self, request: web.Request) -> web.Response:
        notifications = await self.plugin.db.get_unread_notifications()
        return web.json_response({"notifications": notifications})

    async def _handle_mark_read(self, request: web.Request) -> web.Response:
        await self.plugin.db.mark_notifications_read()
        return web.json_response({"success": True})

    # ------------------------------------------------------------------
    # API: Stats
    # ------------------------------------------------------------------

    async def _handle_get_stats(self, request: web.Request) -> web.Response:
        total = await self.plugin.db.count_posts()
        unread = await self.plugin.db.get_unread_notifications()
        return web.json_response({"total_posts": total, "unread_count": len(unread)})

    # ------------------------------------------------------------------
    # API: Reactions
    # ------------------------------------------------------------------

    async def _handle_toggle_reaction(self, request: web.Request) -> web.Response:
        data = await request.json()
        post_id = data.get("post_id")
        emoji = data.get("emoji", "")
        if not post_id or not emoji:
            return web.json_response({"error": "post_id 和 emoji 不能为空"}, status=400)

        existing = await self.plugin.db.get_reactions(post_id)
        user_reacted = any(
            r["emoji"] == emoji and "user" in r.get("authors", []) for r in existing
        )
        if user_reacted:
            await self.plugin.db.remove_reaction(post_id, emoji, "user")
        else:
            await self.plugin.db.add_reaction(post_id, emoji, "user")

        reactions = await self.plugin.db.get_reactions(post_id)
        return web.json_response({"reactions": reactions})

    # ------------------------------------------------------------------
    # API: Upload
    # ------------------------------------------------------------------

    async def _handle_upload_image(self, request: web.Request) -> web.Response:
        data = await request.json()
        image_data = data.get("image", "")
        if not image_data:
            return web.json_response({"error": "缺少图片数据"}, status=400)

        images_dir = self.plugin.data_dir / "images"
        images_dir.mkdir(exist_ok=True)

        # 从 data URI 中提取格式和内容
        ext = "png"
        if image_data.startswith("data:image/"):
            header = image_data.split(",")[0]
            if "jpeg" in header or "jpg" in header:
                ext = "jpg"
            elif "gif" in header:
                ext = "gif"
            elif "webp" in header:
                ext = "webp"
            image_data = image_data.split(",", 1)[1]

        filename = f"{uuid.uuid4().hex[:12]}.{ext}"
        filepath = images_dir / filename

        try:
            img_bytes = base64.b64decode(image_data)
        except Exception as e:
            return web.json_response({"error": f"图片保存失败: {e}"}, status=500)

        # 前端一般已经压过一道，这里只兜底：万一压缩失败（HEIC 之类解不开）
        # 或有人绕过前端直接打接口，也不该让几 MB 的原图落盘。
        # 小图直接原样存，避免无谓的重编码损失。
        filename, filepath = await self._maybe_compress(
            img_bytes, images_dir, filename, filepath, ext
        )

        return web.json_response({"filename": filename, "url": f"/images/{filename}"})

    async def _maybe_compress(
        self,
        img_bytes: bytes,
        images_dir: Path,
        filename: str,
        filepath: Path,
        ext: str,
    ) -> tuple:
        """体积超标才压缩，返回最终的 (filename, filepath)。

        压缩成功时扩展名会变成 jpg，文件名同步改掉，确保落盘内容与后缀一致
        （浏览器按后缀猜 Content-Type，后缀骗人会导致图片打不开）。
        """
        if len(img_bytes) <= COMPRESS_MIN_BYTES or ext in NON_COMPRESSIBLE_EXTS:
            filepath.write_bytes(img_bytes)
            return filename, filepath

        try:
            data = await asyncio.to_thread(
                shrink_to_jpeg, img_bytes, None, None, None, COMPRESS_MIN_BYTES
            )
        except Exception:
            logger.exception("[moment] 上传图片压缩异常，按原图保存")
            data = None

        if data:
            stem = Path(filename).stem
            filename = f"{stem}.jpg"
            filepath = images_dir / filename
            filepath.write_bytes(data)
            logger.info(
                f"[moment] 上传图片已压缩 {len(img_bytes) / 1024:.0f}KB -> {len(data) / 1024:.0f}KB"
            )
            return filename, filepath

        filepath.write_bytes(img_bytes)
        return filename, filepath

    async def _handle_upload_cover(self, request: web.Request) -> web.Response:
        """处理封面图片上传（multipart/form-data）。"""
        reader = await request.multipart()
        field = await reader.next()
        if field is None or field.name != 'file':
            return web.json_response({"error": "缺少文件"}, status=400)

        images_dir = self.plugin.data_dir / "images"
        images_dir.mkdir(exist_ok=True)

        # 确定扩展名
        content_type = field.headers.get(aiohttp.hdrs.CONTENT_TYPE, "image/png")
        ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp"}
        ext = ext_map.get(content_type, "png")

        filename = f"cover_{uuid.uuid4().hex[:8]}.{ext}"
        filepath = images_dir / filename

        # 读取并写入
        size = 0
        with open(filepath, 'wb') as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                size += len(chunk)
                if size > 10 * 1024 * 1024:  # 10MB 限制
                    filepath.unlink(missing_ok=True)
                    return web.json_response({"error": "文件太大，最大 10MB"}, status=400)
                f.write(chunk)

        # 封面每个用户每次进页面都要加载，之前这里完全没压，10MB 的原图也照存。
        # 体积超标就压成 JPEG；压不动（解不开的格式）保留原文件，不影响使用。
        if size > COMPRESS_MIN_BYTES and ext not in NON_COMPRESSIBLE_EXTS:
            try:
                data = await asyncio.to_thread(
                    shrink_to_jpeg, filepath, None, None, None, COMPRESS_MIN_BYTES
                )
            except Exception:
                logger.exception("[moment] 封面压缩异常，保留原图")
                data = None

            if data:
                filepath.unlink(missing_ok=True)
                filename = f"{Path(filename).stem}.jpg"
                filepath.parent.joinpath(filename).write_bytes(data)
                logger.info(
                    f"[moment] 封面上传已压缩 {size / 1024:.0f}KB -> {len(data) / 1024:.0f}KB"
                )

        return web.json_response({"url": f"/images/{filename}"})

    # ------------------------------------------------------------------
    # API: Config
    # ------------------------------------------------------------------

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """返回前端需要的配置信息（名称等）。"""
        npc_names = []
        if getattr(self.plugin, "npc_engine", None):
            try:
                npc_names = self.plugin.npc_engine.names()
            except Exception:
                npc_names = []
        return web.json_response({
            "npc_names": npc_names,
            "ai_name": self.plugin.config.get("ai_name", "") or "他",
            "user_name": self.plugin.config.get("user_name", "") or "我",
            "like_enabled": bool(self.plugin.config.get("like_enabled", True)),
        })

    async def _handle_login(self, request: web.Request) -> web.Response:
        """处理登录请求，验证密码并设置 cookie。"""
        data = await request.json()
        pwd = data.get("password", "")
        if pwd == self._password:
            resp = web.json_response({"success": True})
            # 设置认证 cookie（有效期 30 天）
            auth_hash = hashlib.md5(self._password.encode()).hexdigest()
            resp.set_cookie("moment_auth", auth_hash, max_age=30*24*3600, httponly=True)
            return resp
        return web.json_response({"success": False, "error": "密码错误"})

    # ------------------------------------------------------------------
    # API: Settings
    # ------------------------------------------------------------------

    async def _handle_get_settings(self, request: web.Request) -> web.Response:
        cover_image = await self.plugin.db.get_setting("cover_image", "")
        theme = await self.plugin.db.get_setting("theme", "light")
        signature = await self.plugin.db.get_setting("signature", "Moment")
        return web.json_response({
            "cover_image": cover_image,
            "theme": theme,
            "signature": signature,
        })

    async def _handle_update_settings(self, request: web.Request) -> web.Response:
        data = await request.json()
        allowed_keys = ("cover_image", "theme", "signature")
        for key, value in data.items():
            if key in allowed_keys:
                await self.plugin.db.set_setting(key, str(value))
        return web.json_response({"success": True})

    # ------------------------------------------------------------------
    # 静态文件: 图片
    # ------------------------------------------------------------------

    async def _handle_serve_image(self, request: web.Request) -> web.Response:
        filename = request.match_info.get("filename", "")
        if not filename:
            return web.json_response({"error": "缺少文件名"}, status=400)

        # 安全检查：防止路径穿越
        if ".." in filename or "/" in filename or "\\" in filename:
            return web.json_response({"error": "非法文件名"}, status=400)

        filepath = self.plugin.data_dir / "images" / filename
        if not filepath.exists():
            return web.json_response({"error": "图片不存在"}, status=404)

        content_type = mimetypes.guess_type(str(filepath))[0] or "image/png"
        return web.Response(
            body=filepath.read_bytes(),
            content_type=content_type,
            headers=self._cors_headers(),
        )
