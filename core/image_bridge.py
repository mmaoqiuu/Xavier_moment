"""AstrBot Moment Plugin — 小回相机桥接

朋友圈想让「他」发动态时带上图，出图这件事不自建，
直接借「小回相机」的现成管线——它已经调好了手机随手拍质感、
人物建模质感锁定、锁脸参考图、主备接口切换和超时控制。

这一层只做五件事：
  1. 找到小回相机的插件实例（拿不到就当没有，安静跳过）
  2. 把一条动态改写成一句「拍什么」的拍摄指令
  3. 借它自己的参考图检索，挑出这次要喂的参考图（拿不到就不喂）
  4. 请它出图
  5. 把图拷进朋友圈自己的 images 目录，返回文件名

参考图这一步只「调用」它的方法（_find_reference_images /
_search_reference_by_text / _build_prompt / _make_grouped_reference_sheets），
不复制它的评分与提示词逻辑；对方版本对不上就退回「无参考图」的旧路径。

对外只有一个方法 try_generate_for_post()。
它不抛异常 —— 配图是锦上添花，失败绝不能连累发帖。
"""
from __future__ import annotations

import asyncio
import random
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from astrbot.api import logger

from .image_utils import (
    DEFAULT_MAX_SIDE,
    DEFAULT_QUALITY,
    clamp_max_side,
    clamp_quality,
    shrink_to_jpeg,
)


# 小回相机的插件名（AstrBot 里注册的名字，不是显示名）
CAMERA_PLUGIN_NAME = "astrbot_plugin_xiao_hui_camera"

# 允许拷进朋友圈的图片扩展名
ALLOWED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# 出图超时的硬上限：小回相机自己限制在 85 秒内，这里再兜一层
MAX_TIMEOUT = 90

# 带参考图时走它的 edits 接口，比纯文生图慢，另给一套更宽的区间
MAX_REF_TIMEOUT = 180
MIN_REF_TIMEOUT = 30
DEFAULT_REF_TIMEOUT = 150

# 参考库文件夹名去掉这些后缀就是关键词（「露台参考」→「露台」）
REF_FOLDER_SUFFIXES = ("参考图", "参考", "图库", "文件夹", "库")
# 参考库目录名缓存：目录结构极少变，别每发一条就读一次盘
REF_FOLDER_CACHE_TTL = 300

# 图片压缩默认值（可在配置里改）
DEFAULT_MAX_SIDE = 1280   # 长边像素上限
DEFAULT_QUALITY = 85      # JPEG 质量
MIN_MAX_SIDE = 480

SHOT_SYSTEM_PROMPT = (
    "你是摄影指导，负责把一段生活化的文字转成一句可执行的拍摄指令。"
    "你只输出那一句指令，不解释、不寒暄。"
)

SHOT_PROMPT_TEMPLATE = """他刚发了一条朋友圈动态：

「{content}」
（心情：{mood}）

请你根据这条动态，想一个他此刻会顺手拍下来的画面。

输出要求：
- 只输出一句拍摄指令，10~40 字
- 说清画面里有什么、什么光线、什么氛围
- 画面里如果出现他家里的东西（家具、玩偶、植物、宠物、灯之类），
  写清那是什么、在哪个房间或位置
- 画面里不要出现人，也不要出现人脸和手
- 不要以「照片」「图片」「拍摄」「画面」这类词开头
- 不要解释、不要编号、不要引号、不要 markdown
"""


class ImageBridge:
    """朋友圈 ↔ 小回相机 的桥。"""

    def __init__(self, context, config: dict, data_dir: Path, llm_caller=None):
        """
        Args:
            context: AstrBot 的 Context，用来查询别的插件
            config: 插件配置字典
            data_dir: 朋友圈插件的数据目录
            llm_caller: 用来把动态改写成拍摄指令的调用函数
                        （复用 PostEngine._call_llm，签名 (prompt, system_prompt=)）
        """
        self.context = context
        self.config = config
        self.data_dir = Path(data_dir)
        self.llm_caller = llm_caller
        self.images_dir = self.data_dir / "images"
        # 参考库文件夹名缓存（惰性填充）
        self._ref_folder_cache: Optional[list] = None
        self._ref_folder_cache_at = 0.0
        try:
            self.images_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            logger.exception("[moment] 配图目录创建失败")

    # ------------------------------------------------------------------
    # 对外唯一入口
    # ------------------------------------------------------------------

    async def try_generate_for_post(self, content: str, mood: str = "") -> str:
        """为一条动态尝试配一张图。

        Returns:
            图片文件名（可直接写进 posts.images）；
            任何一步失败都返回空字符串，调用方照常发纯文字动态。
        """
        try:
            if not self._is_enabled():
                return ""

            if random.random() > self._probability():
                logger.debug("[moment] 配图抽签未中，本条动态不带图")
                return ""

            camera = self._get_camera()
            if camera is None:
                return ""

            shot = await self._make_shot_prompt(content, mood)
            if not shot:
                return ""

            path = await self._call_camera(camera, shot)
            if path is None:
                return ""

            return await self._store_image(path)
        except Exception:
            logger.exception("[moment] 配图流程异常，本条动态不带图")
            return ""

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _is_enabled(self) -> bool:
        return bool(self.config.get("image_generate_enabled", False))

    def _probability(self) -> float:
        try:
            value = float(self.config.get("image_generate_probability", 0.4))
        except (TypeError, ValueError):
            value = 0.4
        return max(0.0, min(1.0, value))

    def _timeout(self) -> float:
        try:
            value = int(self.config.get("image_generate_timeout", MAX_TIMEOUT))
        except (TypeError, ValueError):
            value = MAX_TIMEOUT
        return float(max(5, min(MAX_TIMEOUT, value)))

    def _use_reference(self) -> bool:
        """是否在出图时读小回相机的参考图（默认开）。"""
        return bool(self.config.get("image_use_reference", True))

    def _reference_hint(self) -> str:
        """用户手动指定的参考图关键词，逗号分隔，可留空。"""
        return str(self.config.get("image_reference_hint", "") or "").strip()

    def _ref_timeout(self) -> float:
        """带参考图时走 edits 接口，给一套更宽的超时。"""
        try:
            value = int(self.config.get("image_reference_timeout", DEFAULT_REF_TIMEOUT))
        except (TypeError, ValueError):
            value = DEFAULT_REF_TIMEOUT
        return float(max(MIN_REF_TIMEOUT, min(MAX_REF_TIMEOUT, value)))

    def _compress_enabled(self) -> bool:
        return bool(self.config.get("image_compress_enabled", True))

    def _max_side(self) -> int:
        return clamp_max_side(self.config.get("image_max_side", DEFAULT_MAX_SIDE))

    def _quality(self) -> int:
        return clamp_quality(self.config.get("image_jpeg_quality", DEFAULT_QUALITY))

    # ------------------------------------------------------------------
    # 找小回相机
    # ------------------------------------------------------------------

    def _get_camera(self):
        """拿到小回相机的插件实例；任何情况拿不到都返回 None。"""
        try:
            getter = getattr(self.context, "get_registered_star", None)
            if not callable(getter):
                logger.debug("[moment] 当前 AstrBot 不支持查询插件，跳过配图")
                return None

            meta = getter(CAMERA_PLUGIN_NAME)
            if meta is None:
                logger.info("[moment] 没找到「小回相机」，本条动态不带图")
                return None

            # 插件未激活时 star_cls 可能为 None，必须先判
            if not getattr(meta, "activated", True):
                logger.info("[moment] 「小回相机」未启用，本条动态不带图")
                return None

            camera = getattr(meta, "star_cls", None)
            if camera is None:
                logger.info("[moment] 「小回相机」实例尚未就绪，本条动态不带图")
                return None

            # 对方以后改版本可能动这个方法，对不上就安静放弃
            if not hasattr(camera, "_generate_image"):
                version = getattr(meta, "version", "?")
                logger.warning(
                    f"[moment] 「小回相机」({version}) 没有 _generate_image，"
                    "版本不兼容，本条动态不带图"
                )
                return None

            return camera
        except Exception:
            logger.exception("[moment] 查询「小回相机」失败，本条动态不带图")
            return None

    # ------------------------------------------------------------------
    # 动态 → 拍摄指令
    # ------------------------------------------------------------------

    async def _make_shot_prompt(self, content: str, mood: str) -> str:
        """把一条动态改写成一句拍摄指令。"""
        if self.llm_caller is None:
            return ""

        prompt = SHOT_PROMPT_TEMPLATE.format(
            content=(content or "")[:300],
            mood=(mood or "").strip() or "日常",
        )
        try:
            raw = await self.llm_caller(prompt, system_prompt=SHOT_SYSTEM_PROMPT)
        except Exception:
            logger.exception("[moment] 生成拍摄指令失败，本条动态不带图")
            return ""

        shot = self._clean_line(raw)
        if not shot:
            logger.info("[moment] 拍摄指令为空，本条动态不带图")
            return ""

        # 可选：用户想额外收一收风格
        extra = str(self.config.get("image_generate_style_hint", "") or "").strip()
        if extra:
            shot = f"{shot}，{extra}"

        return shot[:200]

    @staticmethod
    def _clean_line(raw) -> str:
        """清理 LLM 输出：去引号、压成一行。"""
        text = str(raw or "").strip()
        if not text:
            return ""
        # 常见的中英文包裹符号
        for left, right in (('"', '"'), ("'", "'"), ("“", "”"), ("「", "」"), ("『", "』")):
            if len(text) > 2 and text.startswith(left) and text.endswith(right):
                text = text[1:-1].strip()
                break
        text = " ".join(text.split())
        # 去掉模型爱加的编号前缀
        for prefix in ("1.", "1、", "- ", "• "):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        return text

    # ------------------------------------------------------------------
    # 参考图：借小回相机自己的检索挑图
    # ------------------------------------------------------------------

    async def _prepare_shot_and_reference(self, camera, shot: str, ratio: str):
        """借它的参考图检索 + prompt 构建，返回 (prompt, ratio, ref_path, used_ref)。

        任何一步不对劲都退回旧行为：shot 原文 + 不带参考图。
        """
        if not self._use_reference():
            return shot, ratio, None, False

        refs = await self._pick_reference(camera, shot, self._reference_hint())
        if not refs:
            logger.info("[moment] 这次没匹配到参考图，按纯文生图出片")
            return shot, ratio, None, False

        builder = getattr(camera, "_build_prompt", None)
        if not callable(builder):
            logger.info("[moment] 「小回相机」没有 _build_prompt，退回纯拍摄指令")
            return shot, ratio, None, False

        try:
            scene = self._camera_scene(camera, shot)
            objects = []
            detector = getattr(camera, "_detect_requested_objects", None)
            if callable(detector):
                objects = list(detector(shot) or [])

            prompt, final_ratio, _final_scene = builder(
                want=shot,
                ratio=ratio,
                scene=scene,
                requested_objects=objects,
                has_reference=True,
                ref_path=refs[0],
                ref_paths=refs,
            )
        except Exception:
            logger.exception("[moment] 借小回相机构建提示词失败，退回纯拍摄指令")
            return shot, ratio, None, False

        ref_to_send = refs[0]
        if len(refs) > 1:
            ref_to_send = await self._merge_references(camera, refs)
        logger.info(
            "[moment] 配图带上参考图："
            + "、".join(f"{r.parent.name}/{r.name}" for r in refs)
        )
        return (
            str(prompt or "").strip() or shot,
            str(final_ratio or ratio),
            ref_to_send,
            True,
        )

    async def _merge_references(self, camera, refs: list) -> Path:
        """多张参考图：借它的拼图方法压成一张 sheet，失败就用第一张。"""
        maker = getattr(camera, "_make_grouped_reference_sheets", None)
        if not callable(maker):
            return refs[0]
        try:
            sheets = list(maker(refs) or [])
        except Exception:
            logger.exception("[moment] 参考图拼合失败，只用第一张")
            return refs[0]
        if not sheets:
            return refs[0]
        try:
            first = Path(sheets[0])
            return first if first.exists() else refs[0]
        except Exception:
            return refs[0]

    async def _pick_reference(self, camera, shot: str, hint: str) -> list:
        """用它的方法挑参考图；拿不到就返回空列表。"""
        try:
            scene = self._camera_scene(camera, shot)
            finder = getattr(camera, "_find_reference_images", None)
            refs = []
            if callable(finder):
                found = await asyncio.to_thread(finder, shot, scene, hint)
                refs = [Path(r) for r in (found or []) if r]
            if refs:
                return refs
            return await self._search_reference_fallback(camera, shot, hint)
        except Exception:
            logger.exception("[moment] 参考图检索失败，改为不带参考图出图")
            return []

    def _camera_scene(self, camera, shot: str) -> str:
        """问它的场景判断；拿不到就按「日常不露脸」。"""
        infer = getattr(camera, "_infer_scene", None)
        if callable(infer):
            try:
                scene = str(infer(shot) or "").strip()
                if scene:
                    return scene
            except Exception:
                logger.debug("[moment] 调用「小回相机」场景判断失败，用默认场景")
        return "daily_no_face"

    async def _search_reference_fallback(self, camera, shot: str, hint: str) -> list:
        """第一家检索没命中时，用它的全库搜索兜一次。

        关键词来自两处：配置里手填的 hint，以及参考库文件夹名
        （去掉「参考/图/库」后缀）——文件夹叫「露台参考」而拍摄指令里提到
        「露台」时，这一步能把图捞出来。
        """
        searcher = getattr(camera, "_search_reference_by_text", None)
        root = self._camera_reference_dir(camera)
        if not callable(searcher) or root is None:
            return []

        for keyword in self._hint_keywords(hint) + await self._auto_keywords(camera, shot):
            try:
                found = await asyncio.to_thread(searcher, root, keyword, True)
            except Exception:
                logger.debug(f"[moment] 参考图搜索失败：{keyword}")
                continue
            if found:
                logger.info(f"[moment] 参考图兜底命中：{keyword} → {Path(found).name}")
                return [Path(found)]
        return []

    @staticmethod
    def _hint_keywords(hint: str) -> list:
        """把配置里的关键词串拆成列表。"""
        return [x for x in re.split(r"[,，、;；\s]+", hint or "") if x.strip()]

    async def _auto_keywords(self, camera, shot: str) -> list:
        """从拍摄指令里找出能和参考库文件夹名对上的关键词。"""
        if not shot:
            return []
        hits = []
        for name in await self._camera_folder_names(camera):
            for keyword in self._keywords_of(name):
                if keyword in shot and keyword not in hits:
                    hits.append(keyword)
        return hits

    async def _camera_folder_names(self, camera) -> list:
        """读参考库目录名，带缓存（目录结构极少变）。"""
        now = time.monotonic()
        if (
            self._ref_folder_cache is not None
            and now - self._ref_folder_cache_at < REF_FOLDER_CACHE_TTL
        ):
            return list(self._ref_folder_cache)

        names: list = []
        root = self._camera_reference_dir(camera)
        if root is not None:
            try:
                names = await asyncio.to_thread(
                    lambda: [d.name for d in root.iterdir() if d.is_dir()]
                )
            except Exception:
                logger.debug("[moment] 读取参考库目录失败，跳过自动参考图")
        self._ref_folder_cache = list(names)
        self._ref_folder_cache_at = now
        return list(names)

    @staticmethod
    def _keywords_of(folder_name: str) -> list:
        """「露台参考」→「露台」；去掉后缀后为空则不用。"""
        name = str(folder_name or "").strip()
        if not name or name in REF_FOLDER_SUFFIXES:
            return []
        for suffix in REF_FOLDER_SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                name = name[: -len(suffix)]
                break
        name = name.strip()
        return [name] if name and name not in REF_FOLDER_SUFFIXES else []

    @staticmethod
    def _camera_reference_dir(camera):
        """拿它的参考库路径；没配、不存在都返回 None。"""
        raw = getattr(camera, "reference_dir", "") or ""
        try:
            root = Path(str(raw))
            return root if raw and root.exists() else None
        except Exception:
            return None

    @staticmethod
    def _camera_allows_no_ref_fallback(camera) -> bool:
        """它自己是否允许「参考图失败就退回纯文生图」。默认不许。"""
        try:
            return bool(getattr(camera, "fallback_to_generations_when_reference_fails", False))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 请它出图
    # ------------------------------------------------------------------

    async def _call_camera(self, camera, shot: str) -> Optional[Path]:
        """请小回相机出图，返回本地图片路径；失败返回 None。"""
        ratio = getattr(camera, "default_ratio", None) or "3:4"
        prompt, ratio, ref, used_ref = await self._prepare_shot_and_reference(
            camera, shot, ratio
        )

        timeout = self._ref_timeout() if used_ref else self._timeout()
        path = await self._generate_once(camera, prompt, ratio, ref, timeout, used_ref)
        if path is not None:
            return path

        # 带参考图失败：只有它自己允许降级时才重试无参考图，避免悄悄换掉主体
        if used_ref and self._camera_allows_no_ref_fallback(camera):
            logger.info("[moment] 带参考图出图失败，按「小回相机」设置降级重试一次")
            return await self._generate_once(
                camera, shot, ratio, None, self._timeout(), False
            )
        return None

    async def _generate_once(
        self, camera, prompt: str, ratio: str, ref, timeout: float, used_ref: bool
    ) -> Optional[Path]:
        """实际调一次出图，超时/异常/无结果一律返回 None。"""
        tag = "（带参考图）" if used_ref else ""
        try:
            path = await asyncio.wait_for(
                camera._generate_image(prompt, ratio, ref),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[moment] 「小回相机」出图超时{tag}（>{timeout:.0f}s），本条动态不带图"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[moment] 「小回相机」出图失败{tag}：{type(exc).__name__}: {exc}"
            )
            return None

        if not path:
            logger.info("[moment] 「小回相机」未返回图片（可能没配生图 provider）")
            return None

        try:
            candidate = Path(path)
            if not candidate.exists():
                logger.warning(f"[moment] 「小回相机」返回的图片不存在: {candidate}")
                return None
            return candidate
        except Exception:
            logger.exception("[moment] 处理出图结果失败")
            return None

    # ------------------------------------------------------------------
    # 落盘到朋友圈的 images 目录
    # ------------------------------------------------------------------

    async def _store_image(self, src: Path) -> str:
        """把生成的图落进朋友圈的 images 目录，返回文件名。

        默认先压缩成 JPEG 再落盘（原图动辄 2MB，前端一次拉几十条会卡）。
        压缩失败或遇到动图时自动回退为原图直拷，绝不因此漏发配图。
        """
        today = datetime.now().strftime("%Y%m%d")
        uid = uuid.uuid4().hex[:8]

        if self._compress_enabled():
            filename = f"ai_{today}_{uid}.jpg"
            dst = self.images_dir / filename
            try:
                data = await asyncio.to_thread(
                    shrink_to_jpeg,
                    src,
                    dst,
                    self._max_side(),
                    self._quality(),
                )
                if data:
                    logger.info(
                        f"[moment] 动态配图已就绪（已压缩 {len(data) / 1024:.0f}KB）: {filename}"
                    )
                    return filename
                logger.info("[moment] 图片是动图或不适合压缩，按原图保存")
            except Exception:
                logger.exception("[moment] 图片压缩失败，回退为原图直拷")
            # 回退前清掉可能写了一半的残缺文件
            self._safe_unlink(dst)

        ext = src.suffix.lower() if src.suffix else ".png"
        if ext not in ALLOWED_EXTS:
            ext = ".png"
        filename = f"ai_{today}_{uid}{ext}"
        dst = self.images_dir / filename

        try:
            # 拷文件是同步 IO，挪到线程里，别卡住事件循环
            await asyncio.to_thread(shutil.copyfile, str(src), str(dst))
        except Exception:
            logger.exception("[moment] 配图落盘失败，本条动态不带图")
            return ""

        logger.info(f"[moment] 动态配图已就绪: {filename}")
        return filename

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        """尽力删除半成品文件，失败不影响主流程。"""
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.debug(f"[moment] 清理临时图片失败: {path}")
