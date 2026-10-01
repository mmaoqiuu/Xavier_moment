"""AstrBot Moment Plugin — 主入口

注册插件生命周期、自托管 HTTP 服务、AI 发帖/评论逻辑，以及：
- NPC 评论链（新动态 → 若干 NPC 陆续来评论 → 他可能接一句）
- 朋友圈 → 聊天的上下文桥（让私聊里的他知道朋友圈发生了什么）
"""
from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .storage.db import MomentDatabase
from .core.chat_bridge import ChatBridge
from .core.like_engine import LikeEngine
from .core.material import MaterialCollector
from .core.npc_comment_guard import NpcCommentGuard
from .core.npc_engine import NpcEngine
from .core.post_engine import PostEngine
from .core.scheduler import Scheduler, decode_schedule, encode_schedule
from .core.standalone_server import MomentServer
from .core.image_bridge import ImageBridge

PLUGIN_NAME = "astrbot_plugin_xavier_moment"


@register(
    PLUGIN_NAME,
    "YuanYuan",
    "让 AI 和你都能发布动态、互相评论，像真实朋友一样分享日常生活。",
    "v0.6.0",
    "https://github.com/mmaoqiuu/Xavier_moment",
)
class MomentPlugin(Star):
    """Moment 朋友圈插件 — 主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config: dict = dict(config) if config else {}

        # 数据目录
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path: Path = self.data_dir / "moment.db"

        # 组件
        self.db: MomentDatabase | None = None
        self.material: MaterialCollector | None = None
        self.post_engine: PostEngine | None = None
        self.scheduler: Scheduler | None = None
        self.server: MomentServer | None = None
        self.npc_engine: NpcEngine | None = None
        self.like_engine: LikeEngine | None = None
        self.chat_bridge: ChatBridge | None = None
        self.image_bridge: ImageBridge | None = None

        # 延迟任务池：本次运行里挂起的 asyncio 定时器，插件卸载时统一取消。
        # 取消只影响「本次运行还跑不跑得到」，事情本身已经落进 jobs 表，
        # 下次启动会按原计划续跑，所以重载不会把「该发生的评论/回复」一起吞掉。
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化数据库、素材收集器、发帖引擎、调度器、HTTP 服务。"""
        try:
            # 数据库
            self.db = MomentDatabase(self.db_path)
            await self.db.connect()
            logger.info(f"[moment] 数据库就绪: {self.db_path}")

            # 素材收集器
            self.material = MaterialCollector(self.context, self.config)

            # 发帖引擎
            self.post_engine = PostEngine(self.context, self.config)

            # 配图桥：发帖时借「小回相机」拍一张。
            # 它没装 / 没开 / 版本不对，都只会在内部安静跳过，不影响发帖。
            self.image_bridge = ImageBridge(
                self.context,
                self.config,
                self.data_dir,
                llm_caller=self.post_engine._call_llm,
            )

            # NPC 评论引擎
            self.npc_engine = NpcEngine(
                self.context, self.config,
                post_engine=self.post_engine, material=self.material,
            )
            npc_count = len(self.npc_engine.names())
            logger.info(f"[moment] NPC 名单已加载：{npc_count} 人" if npc_count
                        else "[moment] NPC 名单为空，NPC 评论不会触发")

            # 点赞引擎（复用上面的 NPC 名单）
            self.like_engine = LikeEngine(
                self.context, self.config, npc_engine=self.npc_engine,
            )
            logger.info("[moment] 点赞功能已启用" if self.like_engine.enabled()
                        else "[moment] 点赞功能已关闭")
            if self.like_engine.enabled():
                ai_p = max(0.0, min(1.0, self._float_config("ai_like_probability", 0.6)))
                npc_p = max(0.0, min(1.0, self._float_config("npc_like_probability", 0.5)))
                pity = self._int_config("npc_like_pity", 2, minimum=0)
                logger.info(
                    f"[moment] 点赞配置：他 {ai_p:.0%}、NPC {npc_p:.0%}，"
                    f"抽中时 1~{self._int_config('npc_like_count_max', 2)} 人随机来赞"
                    + (f"（连着 {pity} 条没人赞就保底来一次）" if pity > 0
                       else "（保底已关闭，完全看概率）")
                )

            # 朋友圈 → 聊天 上下文桥
            self.chat_bridge = ChatBridge(self.context, self.config, self.db)

            # 调度器
            if self.config.get("ai_post_enabled", True):
                self.scheduler = Scheduler(
            self.config,
            self._do_ai_post,
            load_schedule=self._load_daily_schedule,
            save_schedule=self._save_daily_schedule,
        )
                await self.scheduler.start()

            # 启动 HTTP 服务
            host = str(self.config.get("server_host", "0.0.0.0"))
            port = int(self.config.get("server_port", 2141))
            password = str(self.config.get("server_password", "")).strip()
            self.server = MomentServer(self)
            await self.server.start(host=host, port=port, password=password)

            # 重载会取消内存里所有还没到点的延迟任务，先落库的待办在这一步接回来
            await self._resume_jobs()

            # 第二道兜底：最近漏掉签的 NPC 点赞重新抽一次（只在没有排队待办时才补）
            catchup_minutes = self._int_config("npc_like_catchup_minutes", 60, minimum=0)
            if catchup_minutes > 0 and self.like_engine.enabled():
                self._schedule_job(
                    "npc_like_catchup", {"minutes": catchup_minutes}, 10,
                    persist=False,
                )

            logger.info("[moment] 插件初始化完成 ✓")

        except Exception as e:
            logger.error(f"[moment] 初始化失败: {e}")
            await self._safe_terminate()
            raise

    async def terminate(self) -> None:
        await self._safe_terminate()

    async def _safe_terminate(self) -> None:
        # 先取消挂起的延迟任务，再关服务与数据库（否则任务醒来时数据库已经关了）
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        if self.server:
            try:
                await self.server.stop()
            except Exception as e:
                logger.warning(f"[moment] server.stop 异常: {e}")
            self.server = None
        if self.scheduler:
            try:
                await self.scheduler.stop()
            except Exception as e:
                logger.warning(f"[moment] scheduler.stop 异常: {e}")
            self.scheduler = None
        if self.db:
            try:
                await self.db.close()
            except Exception as e:
                logger.warning(f"[moment] db.close 异常: {e}")
            self.db = None
        self.material = None
        self.post_engine = None
        self.npc_engine = None
        self.like_engine = None
        self.chat_bridge = None

    # ------------------------------------------------------------------
    # 延迟任务调度
    # ------------------------------------------------------------------

    async def _chat_context(self) -> str:
        """取最近的私聊上下文，用于朋友圈发言前对齐（取不到返回空串）。"""
        if not self.chat_bridge:
            return ""
        try:
            return await self.chat_bridge.collect_context()
        except Exception:
            logger.exception("[moment] 取私聊上下文失败")
            return ""

    async def _ai_line(self, prompt: str, persona_prompt: str) -> str:
        """让他在朋友圈说一句话；与私聊里刚说过的内容重复时，换个说法重写一次。

        只重写一次：改写后仍然像，就照原样返回——宁可稍微像一点，
        也别因为他「没话找话」失败而让这条评论凭空消失。
        """
        response = await self.post_engine._call_llm(prompt, system_prompt=persona_prompt)
        if not response:
            return ""

        text = self.post_engine._clean_llm_output(response)
        if len(text) < 2:
            return ""

        hit = self._repeat_hit(text)
        if not hit:
            return text

        retry_prompt = (
            prompt
            + f"\n\n【改写要求】你刚才写的是「{text}」，但这句话的意思你刚在私聊里已经说过了"
            f"（「{hit}」）。请换个说法重写：不要重复同样的意思和措辞，"
            "换成别的角度或别的具体细节，同样只回一句话。"
        )
        retry = await self.post_engine._call_llm(retry_prompt, system_prompt=persona_prompt)
        if not retry:
            return text
        retry_text = self.post_engine._clean_llm_output(retry)
        if len(retry_text) < 2:
            return text

        if self._repeat_hit(retry_text):
            logger.info(f"[moment] 改写后仍与私聊重复，按原句发出: {retry_text[:20]}")
        else:
            logger.info(f"[moment] 检测到与私聊重复，已换个说法: {text[:20]} → {retry_text[:20]}")
        return retry_text

    def _repeat_hit(self, text: str) -> str:
        """这段草稿是否在重复他刚在私聊里说过的话（命中返回那句原话）。"""
        if not self.chat_bridge:
            return ""
        try:
            return self.chat_bridge.repeat_hit(text)
        except Exception:
            logger.exception("[moment] 重复检测失败，按原文发出")
            return ""

    # 延迟任务：kind → 本类里对应的处理函数
    _JOB_HANDLERS = {
        "ai_comment": "_job_ai_comment",
        "ai_reply": "_job_ai_reply",
        "ai_like": "_job_ai_like",
        "npc_batch": "_job_npc_batch",
        "npc_comment": "_job_npc_comment",
        "npc_reply": "_job_npc_reply",
        "npc_like": "_job_npc_like",
        "ai_reply_npc": "_job_ai_reply_npc",
        "npc_like_catchup": "_job_npc_like_catchup",
    }

    def _schedule_job(
        self,
        kind: str,
        payload: dict,
        delay_seconds: float,
        dedup_key: str = "",
        post_id: int | None = None,
        persist: bool = True,
        job_id: int | None = None,
    ) -> None:
        """排一件「等一会儿再做」的事。

        顺序是「先落库 → 再等待 → 做完打勾」：待办写进 jobs 表以后，
        就算插件中途重载、内存里的等待被取消，数据库里的待办还在，
        下次启动会接着跑（见 _resume_jobs）。

        persist=False 表示这件事不值得记（比如启动扫描本身）；
        job_id 是重载接回来的那条待办，跑完同样要打勾。
        """
        payload = payload or {}
        if post_id is None:
            post_id = payload.get("post_id")
        should_persist = bool(persist and job_id is None and self.db is not None)

        async def _runner():
            nonlocal job_id
            try:
                duplicate = False
                if should_persist:
                    try:
                        job_id = await self.db.add_job(
                            kind=kind,
                            payload=payload,
                            due_at=(
                                datetime.now() + timedelta(seconds=max(0.0, delay_seconds))
                            ).isoformat(),
                            dedup_key=dedup_key,
                            post_id=post_id,
                        )
                        duplicate = job_id is None
                    except Exception:
                        logger.exception(f"[moment] 待办落库失败（这次仍会执行）: {kind}")
                        job_id = None
                if duplicate:
                    logger.info(f"[moment] 同一件事已在队列里，跳过重复排期: {dedup_key or kind}")
                    return

                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                await self._run_job(kind, payload)

                if job_id is not None and self.db is not None:
                    await self.db.mark_job(job_id, "done", bump_attempts=True)
            except asyncio.CancelledError:
                # 重载 / 停用：待办仍是 pending，下次启动接着跑
                raise
            except Exception as e:
                logger.exception(f"[moment] 延迟任务 {kind} 执行失败")
                if job_id is not None and self.db is not None:
                    try:
                        await self.db.mark_job(job_id, "failed", str(e), bump_attempts=True)
                    except Exception:
                        logger.exception("[moment] 记录待办失败状态时出错")
            finally:
                self._tasks.discard(asyncio.current_task())

        try:
            task = asyncio.create_task(_runner(), name=f"moment-job-{kind}")
        except RuntimeError as e:
            logger.warning(f"[moment] 无法创建延迟任务（事件循环未运行？）: {e}")
            return
        self._tasks.add(task)

    async def _run_job(self, kind: str, payload: dict) -> None:
        """按类型把待办交给对应处理函数。"""
        name = self._JOB_HANDLERS.get(kind)
        handler = getattr(self, name, None) if name else None
        if handler is None:
            logger.warning(f"[moment] 未知的待办类型，已跳过: {kind}")
            return
        await handler(payload)

    async def _resume_jobs(self) -> None:
        """启动时把上次没跑完的待办接回来。

        到点的立刻跑（错开几秒，免得一拥而上），还没到点的按原计划继续等；
        超过续跑时间窗的算过期丢弃——不然三天前的评论会突然冒出来，
        比没有更吓人。
        """
        if not self.db:
            return

        window_minutes = self._int_config("job_resume_minutes", 180, minimum=0)
        if window_minutes <= 0:
            stale = await self.db.expire_all_pending_jobs()
            if stale:
                logger.info(f"[moment] 续跑已关闭，清掉 {stale} 条上回没跑完的待办")
            return

        try:
            rows = await self.db.get_pending_jobs()
        except Exception:
            logger.exception("[moment] 读取待办失败，本次不续跑")
            return
        if not rows:
            return

        now = datetime.now()
        resumed = expired = 0
        for row in rows:
            try:
                due = datetime.fromisoformat(row["due_at"])
            except (TypeError, ValueError):
                await self.db.mark_job(row["id"], "expired", "计划时间无法解析")
                expired += 1
                continue

            if (now - due).total_seconds() > window_minutes * 60:
                await self.db.mark_job(row["id"], "expired", f"超过 {window_minutes} 分钟未执行")
                expired += 1
                continue

            delay = (due - now).total_seconds()
            if delay <= 0:
                delay = random.uniform(3, 30)  # 已经到点的错开跑，别一起冒出来
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            self._schedule_job(
                kind=row["kind"],
                payload=payload,
                delay_seconds=delay,
                dedup_key=row["dedup_key"],
                post_id=row["post_id"],
                persist=False,
                job_id=row["id"],
            )
            resumed += 1

        logger.info(
            f"[moment] 接回 {resumed} 条上回没跑完的待办"
            + (f"，丢弃过期 {expired} 条" if expired else "")
        )
        try:
            await self.db.purge_jobs(keep_days=7)
        except Exception:
            logger.exception("[moment] 清理历史待办失败")

    # ---- 待办执行：每种类型一层薄壳，负责补跑时的幂等判断 ----

    async def _job_ai_comment(self, payload: dict) -> None:
        post_id = int(payload.get("post_id") or 0)
        content = payload.get("content") or ""
        if not self.db or not post_id or not content:
            return
        if await self.db.has_ai_comment(post_id):
            return  # 已经评过了（可能上回跑了一半），不重复
        await self._do_ai_comment(post_id, content)

    async def _job_ai_reply(self, payload: dict) -> None:
        post_id = int(payload.get("post_id") or 0)
        comment = payload.get("comment") or {}
        parent_id = payload.get("parent_id")
        if not self.db or not post_id or not comment.get("id"):
            return
        if await self.db.has_ai_reply_to(comment["id"]):
            return  # 这条评论已经回过了
        post = await self.db.get_post(post_id)
        if not post:
            return
        should_reply = post["author"] == "ai"
        if not should_reply and parent_id:
            parent = await self.db.get_comment(parent_id)
            should_reply = bool(parent and parent["author"] == "ai")
        if should_reply:
            await self._do_ai_reply_comment(post_id, payload.get("post_content") or "", comment)

    async def _job_ai_like(self, payload: dict) -> None:
        await self._do_ai_like(int(payload.get("post_id") or 0))

    async def _job_npc_like(self, payload: dict) -> None:
        await self._do_npc_like(int(payload.get("post_id") or 0), payload.get("name") or "")

    async def _job_npc_batch(self, payload: dict) -> None:
        post_id = int(payload.get("post_id") or 0)
        if not self.db or not post_id:
            return
        if await self.db.count_npc_comments_total(post_id) > 0:
            return  # 这波已经来过了，别再补一波
        await self._do_npc_batch(post_id)

    async def _job_npc_comment(self, payload: dict) -> None:
        await self._do_npc_comment(
            int(payload.get("post_id") or 0), payload.get("npc_name") or "",
        )

    async def _job_npc_reply(self, payload: dict) -> None:
        await self._do_npc_reply(
            int(payload.get("post_id") or 0), int(payload.get("comment_id") or 0),
        )

    async def _job_ai_reply_npc(self, payload: dict) -> None:
        comment = payload.get("comment") or {}
        if not comment.get("id"):
            return
        await self._do_ai_reply_npc(
            int(payload.get("post_id") or 0),
            payload.get("post_content") or "",
            comment,
            payload.get("npc_name") or "",
        )

    async def _job_npc_like_catchup(self, payload: dict) -> None:
        await self._catch_up_npc_likes(int(payload.get("minutes") or 0))

    def _int_config(self, key: str, default: int, minimum: int = 0) -> int:
        raw = self.config.get(key, None)
        if raw is None or raw == "":
            raw = default
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _float_config(self, key: str, default: float) -> float:
        raw = self.config.get(key, None)
        if raw is None or raw == "":
            raw = default
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = default
        return value

    def _random_delay(self, extra_index: int = 0) -> float:
        """NPC 评论的延迟：区间内随机，再按顺序错开，让评论有先后而不是一起冒出来。"""
        lo = self._int_config("npc_delay_min_minutes", 3, minimum=0)
        hi = max(lo, self._int_config("npc_delay_max_minutes", 20, minimum=0))
        return random.uniform(lo, hi) * 60 + extra_index * random.uniform(60, 180)

    # ------------------------------------------------------------------
    # 会话级隔离 + 聊天上下文注入
    # ------------------------------------------------------------------

    async def _is_session_enabled(self, event: AstrMessageEvent) -> bool:
        """被会话禁用的会话里完全不执行（判断不出来按启用处理，宁可多记一笔）。"""
        try:
            from astrbot.core.plugin.session_plugin_manager import SessionPluginManager

            return await SessionPluginManager.is_plugin_enabled_for_session(
                session_id=event.get_session_id(),
                plugin_name=getattr(self, "name", "") or PLUGIN_NAME,
            )
        except Exception:
            return True

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """只做一件事：记住最近一次私聊的会话，供朋友圈侧取上下文用。

        这里不往 prompt 里写任何东西，所以不产生 token 开销。
        """
        if not await self._is_session_enabled(event):
            return
        if not self.chat_bridge:
            return
        try:
            session = getattr(event, "unified_msg_origin", "") or event.get_session_id()
            await self.chat_bridge.remember_session(session)
        except Exception:
            logger.exception("[moment] 记录私聊会话失败")

    # ------------------------------------------------------------------
    # AI 触发接口（供 server 调用）
    # ------------------------------------------------------------------

    def _trigger_ai_comment(self, post_id: int, content: str):
        """调度 AI 对用户帖子的延迟评论。"""
        if not self.config.get("ai_comment_enabled", True):
            return
        delay = self._int_config("ai_comment_delay_minutes", 3) * 60
        self._schedule_job(
            "ai_comment",
            {"post_id": post_id, "content": content},
            delay,
            dedup_key=f"ai_comment:{post_id}",
            post_id=post_id,
        )

    def _trigger_ai_reply(self, post_id: int, post_content: str, comment: dict, parent_id=None):
        """调度 AI 对用户评论的延迟回复。"""
        if not self.config.get("ai_comment_enabled", True):
            return
        if not comment or comment.get("id") is None:
            return

        delay = self._int_config("ai_reply_delay_minutes", 2) * 60
        self._schedule_job(
            "ai_reply",
            {
                "post_id": post_id,
                "post_content": post_content,
                "comment": comment,
                "parent_id": parent_id,
            },
            delay,
            dedup_key=f"ai_reply:{comment['id']}",
            post_id=post_id,
        )

    # ------------------------------------------------------------------
    # NPC 评论链
    # ------------------------------------------------------------------

    def _trigger_npc_comments(self, post_id: int):
        """新动态产生后，安排若干 NPC 陆续来评论。"""
        if not self.npc_engine or not self.npc_engine.enabled():
            return
        # 挑人本身很快，不用等；每个人各自的延迟在 _do_npc_batch 里排
        self._schedule_job(
            "npc_batch", {"post_id": post_id}, 0,
            dedup_key=f"npc_batch:{post_id}", post_id=post_id,
        )

    def _trigger_npc_reply(self, post_id: int, comment: dict):
        """有人（你或他）在评论区发言后，按概率安排一个 NPC 来接话。

        NPC 之间的接话不在这里触发——那会变成无限套娃。NPC 之间互相接话是在
        各自评论时按 npc_chain_probability 决定的，一条动态里每人只评论一次，天然有界。
        """
        if not self.npc_engine or not self.npc_engine.enabled():
            return
        if not comment or comment.get("author") == "npc":
            return
        prob = self._float_config("npc_chain_probability", 0.5)
        if prob <= 0 or random.random() >= prob:
            return
        self._schedule_job(
            "npc_reply",
            {"post_id": post_id, "comment_id": comment["id"]},
            self._random_delay(),
            dedup_key=f"npc_reply:{comment['id']}",
            post_id=post_id,
        )

    # ------------------------------------------------------------------
    # 点赞
    # ------------------------------------------------------------------

    async def _plan_npc_likes(self) -> dict:
        """抽一次 NPC 点赞名单，并维护「连续落空」计数（保底用）。

        命中就把计数清零，落空就 +1。计数写在 settings 里，插件重载也不会忘——
        否则每次重载都从零开始，保底永远等不到。

        返回的 plan 里额外带上 streak_before / streak_after，方便日志说清
        「这是第几条没中了」。
        """
        streak = 0
        if self.db:
            try:
                streak = int(await self.db.get_setting("npc_like_miss_streak", "0") or 0)
            except (TypeError, ValueError):
                streak = 0

        force = self.like_engine.should_force(streak)
        plan = self.like_engine.npc_pick_plan(force=force)
        hit = bool(plan["hit"] and plan["names"])

        if self.db:
            try:
                await self.db.set_setting(
                    "npc_like_miss_streak", "0" if hit else str(streak + 1)
                )
            except Exception:
                logger.exception("[moment] 记录 NPC 点赞落空次数失败")

        plan["streak_before"] = streak
        plan["streak_after"] = 0 if hit else streak + 1
        return plan

    async def _trigger_likes(self, post_id: int, post_author: str):
        """新动态产生后安排点赞：他可能赞你，NPC 也可能赞（他发的动态同理）。

        点赞不写进通知：真实朋友圈的赞不单独提醒，只在页面里看得到。
        """
        if not self.like_engine or not self.like_engine.enabled():
            return

        # 他赞你：一条动态只赞一次，延迟沿用他评论的延迟（刷到就顺手赞）
        if self.like_engine.ai_should_like(post_author):
            delay = self._int_config("ai_comment_delay_minutes", 3) * 60
            self._schedule_job(
                "ai_like", {"post_id": post_id}, delay,
                dedup_key=f"ai_like:{post_id}", post_id=post_id,
            )

        # NPC 赞：复用评论的延迟区间，逐个往后错开，像陆续刷到。
        # 抽签结果无论中没中都记一笔，否则页面上看不出是「没抽中」还是「坏了」。
        plan = await self._plan_npc_likes()
        if plan["hit"] and plan["names"]:
            logger.info(
                f"[moment] 动态 #{post_id}：NPC 点赞抽中 {len(plan['names'])} 人 → "
                + "、".join(plan["names"])
                + ("（连着落空，这次保底硬给）" if plan.get("forced") else "")
            )
            for index, name in enumerate(plan["names"]):
                self._schedule_job(
                    "npc_like",
                    {"post_id": post_id, "name": name},
                    self._random_delay(index),
                    dedup_key=f"npc_like:{post_id}:{name}",
                    post_id=post_id,
                )
        else:
            pity = self.like_engine.pity_limit()
            nxt = "，下一条保底必来" if pity > 0 and plan["streak_after"] >= pity else ""
            logger.info(
                f"[moment] 动态 #{post_id}：这次没抽中 NPC 点赞，本条不会有人来赞"
                f"（概率 {plan['probability']:.0%}，候选 {plan['pool_size']} 人，"
                f"已连空 {plan['streak_after']} 条{nxt}）"
            )

    async def _do_ai_like(self, post_id: int) -> None:
        """他给用户的动态点个赞（落库前再确认一次作者，避免中途被删或被换人）。"""
        if not self.db:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post or post["author"] != "user":
                return
            created = await self.db.add_like(post_id, author="ai")
            if created:
                logger.info(f"[moment] 他点赞了动态 #{post_id}")
        except Exception:
            logger.exception("[moment] AI 点赞失败")

    async def _do_npc_like(self, post_id: int, name: str) -> None:
        """某个 NPC 给动态点个赞。"""
        if not self.db or not name:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return
            created = await self.db.add_like(post_id, author="npc", author_name=name)
            if created:
                logger.info(f"[moment] NPC「{name}」点赞了动态 #{post_id}")
        except Exception:
            logger.exception("[moment] NPC 点赞失败")

    async def _catch_up_npc_likes(self, minutes: int) -> None:
        """补扫：重载会取消所有还没执行的延迟任务，最近发过的动态可能因此一条 NPC 赞都没轮到。

        启动时回头看一眼最近 minutes 分钟内、还没有任何 NPC 点过赞的动态，重新抽一次签补上；
        已经有人赞过的不动，同一个窗口内补过的也不重复补——所以一条动态最多补一次，
        不会因为频繁重载攒出一堆赞。补不补得到仍然看概率，抽不中就是真的没有，这是朋友圈该有的样子。
        """
        if not self.db or not self.like_engine or not self.npc_engine:
            return
        try:
            cutoff = datetime.now() - timedelta(minutes=minutes)
            posts = await self.db.get_posts(limit=30, offset=0)

            pending: list[int] = []
            for post in posts:
                try:
                    if datetime.fromisoformat(post["created_at"]) < cutoff:
                        continue
                except (KeyError, TypeError, ValueError):
                    continue
                # 窗口内已经补过的不再补（标记写在 settings 里，重启也认）
                marker = await self.db.get_setting(f"npc_like_catchup_{post['id']}", "")
                if marker:
                    try:
                        if datetime.fromisoformat(marker) > cutoff:
                            continue
                    except ValueError:
                        pass
                if any(lk.get("author") == "npc" for lk in await self.db.get_likes(post["id"])):
                    continue
                # 队列里还排着 NPC 点赞的就别插手了，等它自己跑，免得补出一堆重复
                if await self.db.count_pending_jobs(kind="npc_like", post_id=post["id"]) > 0:
                    continue
                pending.append(post["id"])

            if not pending:
                return

            logger.info(f"[moment] 补扫：最近 {minutes} 分钟内有 {len(pending)} 条动态还没有 NPC 点赞，重新抽签")

            for post_id in pending:
                plan = await self._plan_npc_likes()
                await self.db.set_setting(
                    f"npc_like_catchup_{post_id}", datetime.now().isoformat()
                )
                if not plan["hit"] or not plan["names"]:
                    logger.info(f"[moment] 补扫：动态 #{post_id} 这次也没抽中，跳过")
                    continue
                logger.info(
                    f"[moment] 补扫：动态 #{post_id} 抽中 {len(plan['names'])} 人 → "
                    + "、".join(plan["names"])
                )
                for index, name in enumerate(plan["names"]):
                    self._schedule_job(
                        "npc_like",
                        {"post_id": post_id, "name": name},
                        random.uniform(30, 180) * (index + 1),
                        dedup_key=f"npc_like:{post_id}:{name}",
                        post_id=post_id,
                    )
        except Exception:
            logger.exception("[moment] 补扫 NPC 点赞失败")

    async def _recent_npc_names(self, cooldown_minutes: int) -> set:
        """冷却期内刚评论过的 NPC，交给挑人时优先避开。"""
        if cooldown_minutes <= 0 or not self.npc_engine or not self.db:
            return set()
        cutoff = datetime.now() - timedelta(minutes=cooldown_minutes)
        blocked = set()
        for name in self.npc_engine.names():
            raw = await self.db.get_setting(f"npc_last_comment_{name}", "")
            if not raw:
                continue
            try:
                if datetime.fromisoformat(raw) > cutoff:
                    blocked.add(name)
            except ValueError:
                continue
        return blocked

    async def _npc_comment_quota_left(self, post_id: int):
        """这条动态下还能再来几条 NPC 发言（评论 + 接话）。

        返回 None 表示不限制（配置填 0）。设上限是为了防止评论区被 NPC
        自己刷满：初始挑 2~5 人，加上接话和他来回回复，很容易膨胀成十几条，
        真人反而插不进去话。
        """
        limit = self._int_config("npc_post_comment_limit", 8, minimum=0)
        if limit <= 0 or not self.db:
            return None
        try:
            used = await self.db.count_npc_comments_total(post_id)
        except Exception:
            logger.exception("[moment] 统计 NPC 评论条数失败")
            return None
        return max(0, limit - used)

    async def _do_npc_batch(self, post_id: int):
        """挑人并排期：每个人都带自己的延迟，像真的有人陆陆续续刷到这条动态。"""
        if not self.db or not self.npc_engine:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return
            cooldown = self._int_config("npc_cooldown_minutes", 30)
            blocked = await self._recent_npc_names(cooldown)
            picks = self.npc_engine.plan_batch(exclude=blocked)
            if post.get("author") == "ai":
                # 跟他生疏的人不会专门跑到他的动态下面留言
                strangers = self.npc_engine.ai_strangers()
                picks = [p for p in picks if p.get("name") not in strangers]
            if not picks:
                return
            quota = await self._npc_comment_quota_left(post_id)
            if quota is not None:
                if quota <= 0:
                    logger.info(f"[moment] 帖子 #{post_id} 的 NPC 发言已达上限，不再安排新人")
                    return
                if len(picks) > quota:
                    logger.info(f"[moment] 帖子 #{post_id} 只剩 {quota} 条 NPC 发言额度，裁掉 {len(picks) - quota} 人")
                    picks = picks[:quota]
            logger.info(f"[moment] 帖子 #{post_id} 将迎来 {len(picks)} 个 NPC 评论：{[p['name'] for p in picks]}")
            for index, npc in enumerate(picks):
                self._schedule_job(
                    "npc_comment",
                    {"post_id": post_id, "npc_name": npc["name"]},
                    self._random_delay(extra_index=index),
                    dedup_key=f"npc_comment:{post_id}:{npc['name']}",
                    post_id=post_id,
                )
        except Exception:
            logger.exception("[moment] 安排 NPC 评论失败")

    async def _do_npc_comment(self, post_id: int, npc_name: str, allow_repeat: bool = False):
        """某个 NPC 对某条动态发表一条评论。

        进来先拿锁（按「动态 + NPC」，见 core/npc_comment_guard.py），
        再交给 _do_npc_comment_impl 干活。
        查重和落库之间夹着一整段模型调用（好几秒），两个任务同时到点时，
        双方都会在对方写入前通过查重，于是同一个人连出两条一模一样的话。
        锁上以后后到的那条一进来，前一条已经写进库了，查重自然拦得住。
        """
        if not self.db or not self.npc_engine:
            return
        guard = getattr(self, "_npc_comment_guard", None)
        if guard is None:
            guard = self._npc_comment_guard = NpcCommentGuard()
        async with guard.lock_for(post_id, npc_name):
            await self._do_npc_comment_impl(post_id, npc_name, allow_repeat)

    async def _do_npc_comment_impl(self, post_id: int, npc_name: str, allow_repeat: bool = False):
        """发表评论的实际逻辑（调用方已按 post_id + npc_name 加锁）。"""
        if not self.db or not self.npc_engine:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return  # 帖子被删了，安静收场
            quota = await self._npc_comment_quota_left(post_id)
            if quota is not None and quota <= 0:
                logger.info(f"[moment] 帖子 #{post_id} 的 NPC 发言已达上限，{npc_name} 这条就不发了")
                return
            npc = self.npc_engine.by_name(npc_name)
            if not npc:
                return  # 配置被改过了
            if not allow_repeat and await self.db.count_npc_comments(post_id, npc_name) >= 1:
                return

            comments = await self.db.get_comments(post_id)
            data = await self.npc_engine.generate(post, comments, npc)
            if not data:
                return

            comment = await self.db.create_comment(
                post_id=post_id,
                author="npc",
                author_name=npc_name,
                content=data["content"],
                parent_comment_id=data.get("parent_comment_id"),
            )
            await self.db.set_setting(f"npc_last_comment_{npc_name}", datetime.now().isoformat())
            logger.info(f"[moment] {npc_name} 评论了帖子 #{post_id}: {data['content'][:30]}...")

            # 只有他自己发的动态才通知她，免得噪声太多
            if post.get("author") == "user" and self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 {npc_name}评论了你的动态：\n\n「{data['content'][:80]}」",
                    post_id=post_id,
                    ntype="new_comment",
                )

            # 他可能接着接一句（单条动态下回 NPC 的次数有上限，免得他一个人刷满评论区）
            reply_limit = self._int_config("ai_npc_reply_limit", 2, minimum=0)
            replied = await self.db.count_ai_replies_to_npc(post_id) if reply_limit > 0 else 0
            if reply_limit > 0 and replied >= reply_limit:
                logger.info(f"[moment] 帖子 #{post_id} 他回 NPC 的次数已达上限（{reply_limit}），这条不接了")
            elif random.random() < self._float_config("npc_xavier_reply_probability", 0.5):
                delay = self._int_config("ai_reply_delay_minutes", 2) * 60
                self._schedule_job(
                    "ai_reply_npc",
                    {
                        "post_id": post_id,
                        "post_content": post["content"],
                        "comment": comment,
                        "npc_name": npc_name,
                    },
                    delay,
                    dedup_key=f"ai_reply_npc:{comment['id']}",
                    post_id=post_id,
                )

        except Exception:
            logger.exception(f"[moment] NPC {npc_name} 评论失败")

    async def _do_npc_reply(self, post_id: int, target_comment_id: int):
        """NPC 在评论区接话：被点名的评论是谁说的，就尽量让谁来接。"""
        if not self.db or not self.npc_engine:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return
            target = await self.db.get_comment(target_comment_id)
            if not target:
                return
            quota = await self._npc_comment_quota_left(post_id)
            if quota is not None and quota <= 0:
                logger.info(f"[moment] 帖子 #{post_id} 的 NPC 发言已达上限，这次不接话")
                return
            comments = await self.db.get_comments(post_id)

            # 被接的是他的话时，跟他生疏的人不掺和——不熟的人不会主动去接他的话
            strangers = self.npc_engine.ai_strangers() if target.get("author") == "ai" else set()

            npc = None
            if target.get("author") == "npc":
                npc = self.npc_engine.by_name(target.get("author_name") or "")
            else:
                # 优先让已经在这条动态里露过面的 NPC 接话，看起来像真的在聊天
                spoken = [
                    c.get("author_name") for c in comments
                    if c.get("author") == "npc" and c.get("author_name")
                ]
                spoken = [n for n in spoken if n not in strangers]
                if spoken:
                    npc = self.npc_engine.by_name(random.choice(spoken))
            if npc is None:
                blocked = await self._recent_npc_names(self._int_config("npc_cooldown_minutes", 30))
                picked = self.npc_engine.pick(1, exclude=blocked | strangers)
                npc = picked[0] if picked else None
            # pick 在人数不够时会放宽排除条件，这里兜一道，别让不熟的人被捞回来
            if npc is not None and npc.get("name") in strangers:
                npc = None
            if not npc:
                return
            # 同一个人在同一条动态下最多 3 次，够一来一回，不会没完没了
            if await self.db.count_npc_comments(post_id, npc["name"]) >= 3:
                return

            data = await self.npc_engine.generate(post, comments, npc, force_target=target)
            if not data:
                return

            await self.db.create_comment(
                post_id=post_id,
                author="npc",
                author_name=npc["name"],
                content=data["content"],
                parent_comment_id=data.get("parent_comment_id") or target_comment_id,
            )
            await self.db.set_setting(f"npc_last_comment_{npc['name']}", datetime.now().isoformat())
            logger.info(f"[moment] {npc['name']} 回复了评论 #{target_comment_id}: {data['content'][:30]}...")

        except Exception:
            logger.exception("[moment] NPC 接话失败")

    # ------------------------------------------------------------------
    # AI 发帖 / 评论核心逻辑
    # ------------------------------------------------------------------
    async def _load_daily_schedule(self):
        """取今天已经摇好的发帖计划；没有、或不是今天的，返回 None。

        重载插件不该重新摇时间点，否则当天会被多塞一条动态。
        """
        if not self.db:
            return None
        try:
            raw = await self.db.get_setting("daily_post_schedule", "")
        except Exception:
            logger.exception("[moment] 读取今日发帖计划失败")
            return None
        return decode_schedule(raw)

    async def _save_daily_schedule(self, times):
        """把今天摇好的发帖计划存起来，重载后接着用。"""
        if not self.db:
            return
        try:
            await self.db.set_setting("daily_post_schedule", encode_schedule(times))
        except Exception:
            logger.exception("[moment] 保存今日发帖计划失败")


    async def _do_ai_post(self):
        """执行一次 AI 发帖。"""
        if not self.db or not self.material or not self.post_engine:
            return

        # 1. 收集素材
        materials = await self.material.collect(db_instance=self.db)

        # 2. 生成动态
        # 带上最近的私聊，避免和刚在私聊里说过的话打架
        chat_context = await self._chat_context()
        result = await self.post_engine.generate_post(materials, extra_context=chat_context)
        if not result:
            logger.warning("[moment] AI 发帖生成失败，本次跳过")
            return

        # 3. 配图（可选）：请「小回相机」拍一张。
        #    没装 / 没开 / 超时 / 出图失败，都只返回空字符串，不影响下面发帖。
        images = ""
        if self.image_bridge:
            images = await self.image_bridge.try_generate_for_post(
                result["content"], result.get("mood", "")
            )

        # 4. 存入数据库
        post = await self.db.create_post(
            author="ai",
            content=result["content"],
            mood=result.get("mood", ""),
            source_hint="auto_scheduled",
            images=images,
        )
        logger.info(f"[moment] AI 发布了新动态 #{post['id']}: {result['content'][:30]}...")

        # 5. 推送通知
        if self.config.get("notify_on_ai_post", True):
            await self._send_notification(
                f"📢 您关注的用户发布了一条新动态：\n\n「{result['content'][:100]}」",
                post_id=post["id"],
                ntype="new_post",
            )

        # 6. 他发的动态同样会有人来评论，也会有人点赞
        self._trigger_npc_comments(post["id"])
        await self._trigger_likes(post["id"], "ai")

    async def _do_ai_comment(self, post_id: int, post_content: str):
        """AI 对用户的帖子发表评论。"""
        if not self.db or not self.post_engine:
            return

        try:
            # 获取人设作为 system_prompt，保持角色一致性
            persona_prompt = ""
            if self.material:
                persona_prompt = await self.material.get_persona()

            prompt = f"""以下是你最亲近的人发的一条朋友圈动态：

「{post_content}」

请以你自己的身份、用符合你性格的方式，自然地回应这条动态。

要求：
- 你和发这条动态的人关系非常亲密
- 用你自己的说话习惯和语气回应
- 可以是：关心、追问、分享感受、调侃、撒娇、吐槽——取决于你的性格和内容
- 只能回复一句话，字数严格控制在 20 字以内
- 绝对不允许使用任何换行符
- 不要自我介绍，不要解释你是谁
- 不要以任何第三人称提到你自己（比如不要说"作为xxx"）
- 直接输出评论内容"""

            context_block = await self._chat_context()
            if context_block:
                prompt = context_block + "\n\n" + prompt

            comment_text = await self._ai_line(prompt, persona_prompt)
            if not comment_text:
                return

            comment = await self.db.create_comment(
                post_id=post_id,
                author="ai",
                content=comment_text,
            )
            logger.info(f"[moment] AI 评论了帖子 #{post_id}: {comment_text[:30]}...")

            # 通知用户
            if self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 您发布的动态有新评论：\n\n「{comment_text[:80]}」",
                    post_id=post_id,
                    ntype="new_comment",
                )

            # 评论区里他也在，NPC 可能会接他的话
            self._trigger_npc_reply(post_id, comment)

        except Exception as e:
            logger.error(f"[moment] AI 评论失败: {e}")

    async def _do_ai_reply_comment(self, post_id: int, post_content: str, user_comment: dict):
        """AI 回复用户对 AI 帖子的评论。"""
        if not self.db or not self.post_engine:
            return

        try:
            # 获取人设作为 system_prompt，保持角色一致性
            persona_prompt = ""
            if self.material:
                persona_prompt = await self.material.get_persona()

            prompt = f"""你之前发了一条朋友圈动态：
「{post_content[:200]}」

你最亲近的人在下面评论了：
「{user_comment['content']}」

请以你自己的身份、用符合你性格的方式回复这条评论。

要求：
- 用你平时的说话习惯和语气
- 只能回复一句话，字数严格控制在 20 字以内
- 绝对不允许使用任何换行符
- 像真人情侣/挚友之间在评论区的互动
- 直接输出回复内容"""

            context_block = await self._chat_context()
            if context_block:
                prompt = context_block + "\n\n" + prompt

            reply_text = await self._ai_line(prompt, persona_prompt)
            if not reply_text:
                return

            await self.db.create_comment(
                post_id=post_id,
                author="ai",
                content=reply_text,
                parent_comment_id=user_comment["id"],
            )

            if self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 您的评论有新回复：\n\n「{reply_text[:80]}」",
                    post_id=post_id,
                    ntype="new_reply",
                )

        except Exception as e:
            logger.error(f"[moment] AI 回复评论失败: {e}")

    async def _do_ai_reply_npc(self, post_id: int, post_content: str, npc_comment: dict, npc_name: str):
        """他在评论区回应某个 NPC 的话。"""
        if not self.db or not self.post_engine:
            return

        try:
            post = await self.db.get_post(post_id)
            if not post:
                return  # 帖子被删了就算了
            if await self.db.has_ai_reply_to(npc_comment["id"]):
                return  # 已经回过了，不重复

            persona_prompt = ""
            if self.material:
                persona_prompt = await self.material.get_persona()

            # 跟他生疏的人，回一句客气话就够了，别自来熟
            unfamiliar = bool(self.npc_engine) and self.npc_engine.is_stranger_to_ai(npc_name)
            tone_hint = (
                f"你和 {npc_name} 并不熟，客气、简短地回一句就好——别接梗、别调侃、别自来熟。"
                if unfamiliar
                else "熟人之间的接话：可以接梗、调侃、回怼、顺着聊。"
            )

            prompt = f"""你之前在朋友圈发了一条动态：
「{post_content[:200]}」

{npc_name} 在这条动态下面评论了：
「{npc_comment['content']}」

请以你自己的身份、用符合你性格的方式回应这句评论（{tone_hint}）。

要求：
- 用你平时的说话习惯和语气，注意你和 {npc_name} 的关系远近，别越界
- 只能回复一句话，字数严格控制在 20 字以内
- 绝对不允许使用任何换行符
- 不要自我介绍，不要说「作为朋友」这类旁白
- 直接输出回复内容"""

            context_block = await self._chat_context()
            if context_block:
                prompt = context_block + "\n\n" + prompt

            reply_text = await self._ai_line(prompt, persona_prompt)
            if not reply_text:
                return

            await self.db.create_comment(
                post_id=post_id,
                author="ai",
                content=reply_text,
                parent_comment_id=npc_comment["id"],
            )
            logger.info(f"[moment] AI 回复了 {npc_name} 的评论 #{npc_comment['id']}: {reply_text[:30]}...")

            if self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 他在你的动态下回复了{npc_name}：\n\n「{reply_text[:80]}」",
                    post_id=post_id,
                    ntype="new_reply",
                )

        except Exception as e:
            logger.error(f"[moment] AI 回复 NPC 评论失败: {e}")

    # ------------------------------------------------------------------
    # 通知推送
    # ------------------------------------------------------------------

    async def _send_notification(self, message: str, post_id: int = None, ntype: str = "new_post"):
        """存储通知到数据库（仅用于网页通知面板，不推送到聊天）。"""
        # (旧版本用来控制是否推送到当前群聊，现已屏蔽消息推送逻辑)
        if self.db:
            await self.db.create_notification(ntype, message, ref_post_id=post_id)
