"""AstrBot Moment Plugin — 小回相机桥接

朋友圈想让「他」发动态时带上图，出图这件事不自建，
直接借「小回相机」的现成管线——它已经调好了手机随手拍质感、
人物建模质感锁定、锁脸参考图、主备接口切换和超时控制。

这一层只做四件事：
  1. 找到小回相机的插件实例（拿不到就当没有，安静跳过）
  2. 把一条动态改写成一句「拍什么」的拍摄指令
  3. 请它出图
  4. 把图拷进朋友圈自己的 images 目录，返回文件名

对外只有一个方法 try_generate_for_post()。
它不抛异常 —— 配图是锦上添花，失败绝不能连累发帖。
"""
from __future__ import annotations

import asyncio
import random
import shutil
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
    # 请它出图
    # ------------------------------------------------------------------

    async def _call_camera(self, camera, shot: str) -> Optional[Path]:
        """请小回相机出图，返回本地图片路径；失败返回 None。"""
        ratio = getattr(camera, "default_ratio", None) or "3:4"
        timeout = self._timeout()

        try:
            path = await asyncio.wait_for(
                camera._generate_image(shot, ratio, None),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[moment] 「小回相机」出图超时（>{timeout:.0f}s），本条动态不带图"
            )
            return None
        except Exception as exc:
            logger.warning(
                f"[moment] 「小回相机」出图失败：{type(exc).__name__}: {exc}"
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
