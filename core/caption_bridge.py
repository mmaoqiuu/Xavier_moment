import os
import asyncio
from pathlib import Path
from typing import Optional
from astrbot.api import logger
from astrbot.core.star.context import Context


class CaptionBridge:
    """借助 AstrBot 辅助识图模型，对朋友圈动态中的配图进行画面理解与描述提取。"""

    def __init__(self, context: Context, config: dict, data_dir: Path):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        self.images_dir = data_dir / "images"
        # 缓存：filename -> caption 描述文本，避免 AI 和多个 NPC 重复请求视觉模型
        self._cache: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _get_caption_provider(self):
        """寻找可用的识图 Provider。优先插件配置，兜底使用 AstrBot 全局识图模型。"""
        prov_id = (self.config.get("image_caption_provider_id") or "").strip()

        # 尝试从全局配置获取
        if not prov_id:
            try:
                global_cfg = self.context.get_config()
                if isinstance(global_cfg, dict):
                    prov_id = (
                        global_cfg.get("provider_settings", {}).get(
                            "default_image_caption_provider_id"
                        )
                        or global_cfg.get("default_image_caption_provider_id")
                        or ""
                    )
            except Exception as e:
                logger.debug(f"[moment] 获取全局识图配置异常: {e}")

        if not prov_id:
            return None

        # 通过 context 获取 Provider 实例
        prov = None
        if hasattr(self.context, "get_provider_by_id"):
            try:
                prov = self.context.get_provider_by_id(prov_id)
            except Exception:
                prov = None

        if prov is None:
            provider_mgr = getattr(self.context, "provider_manager", None)
            if provider_mgr:
                if hasattr(provider_mgr, "get_provider"):
                    prov = provider_mgr.get_provider(prov_id)
                elif hasattr(provider_mgr, "providers") and isinstance(provider_mgr.providers, dict):
                    for p_name, p_obj in provider_mgr.providers.items():
                        if prov_id in p_name or p_name == prov_id:
                            prov = p_obj
                            break

        return prov

    async def get_image_caption(self, filename: str) -> Optional[str]:
        """对单张图片提取画面描述。"""
        if not filename:
            return None

        # 1. 读缓存
        async with self._lock:
            if filename in self._cache:
                return self._cache[filename]

        img_path = self.images_dir / filename
        if not img_path.is_file():
            # 兼容可能是绝对路径
            p = Path(filename)
            if p.is_file():
                img_path = p
            else:
                logger.warning(f"[moment] 识图文件不存在: {img_path}")
                return None

        provider = self._get_caption_provider()
        if not provider:
            logger.debug("[moment] 未配置或未找到识图 Provider，跳过图片理解")
            return None

        # 2. 调用识图 Provider
        prompt = (
            self.config.get("image_caption_prompt")
            or "请用中文详细且生动地描述这张图片中的主体内容、画面细节、环境与氛围，方便在朋友圈互动时参考。字数在 100 字以内。"
        )

        try:
            logger.info(f"[moment] 正在使用辅助模型对配图 {filename} 进行画面识别...")
            abs_path_str = str(img_path.resolve())
            resp = await provider.text_chat(
                prompt=prompt,
                image_urls=[abs_path_str],
            )
            text = getattr(resp, "completion_text", "") or ""
            text = text.strip()
            if text:
                async with self._lock:
                    self._cache[filename] = text
                logger.info(f"[moment] 图片 {filename} 识别成功: {text[:40]}...")
                return text
        except Exception as e:
            logger.warning(f"[moment] 图片 {filename} 辅助识图失败: {e}")

        return None

    async def get_post_caption(self, post: dict) -> str:
        """获取某条动态所有配图的画面综合描述。"""
        images_str = (post.get("images") or "").strip()
        if not images_str:
            return ""

        filenames = [f.strip() for f in images_str.split(",") if f.strip()]
        if not filenames:
            return ""

        captions = []
        for i, fn in enumerate(filenames, 1):
            cap = await self.get_image_caption(fn)
            if cap:
                if len(filenames) > 1:
                    captions.append(f"【图{i}】：{cap}")
                else:
                    captions.append(cap)

        return "\n".join(captions)
