"""图片压缩工具 —— 朋友圈里所有「图片会变重」的入口共用这一份。

三个使用场景：
  1. AI 配图落盘      core/image_bridge.py
  2. 用户上传图片     core/standalone_server.py  /api/upload
  3. 用户上传封面     core/standalone_server.py  /api/upload/cover

统一口径：按 EXIF 摆正方向 -> 长边超限则等比缩小 -> 透明通道铺白底
          -> 存为渐进式 JPEG。

设计原则：
  * 压缩是优化，不是必需品。任何一步失败都返回 False，由调用方决定回退策略
    （通常是原图直存），绝不让「压不动」升级成「存不下」。
  * 动图（GIF / animated WebP）直接返回 False：JPEG 装不下动画，
    硬压只会得到一张静止的首帧。
  * PIL 延迟导入：没装 Pillow 也不该拖垮插件启动。
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional, Union

from astrbot.api import logger


# 默认参数
DEFAULT_MAX_SIDE = 1280   # 长边像素上限
DEFAULT_QUALITY = 85      # JPEG 质量
MIN_MAX_SIDE = 480
MAX_MAX_SIDE = 4096

# 低于这个体积就不必再压了：收益微乎其微，重编码反而多一次画质损失
SKIP_BELOW_BYTES = 300 * 1024

# source 可以是路径，也可以是内存里的字节
Source = Union[str, Path, bytes, bytearray, io.BytesIO]


def clamp_max_side(value: Optional[int]) -> int:
    """把长边参数收进合法区间。"""
    try:
        num = int(value) if value is not None else DEFAULT_MAX_SIDE
    except (TypeError, ValueError):
        num = DEFAULT_MAX_SIDE
    return max(MIN_MAX_SIDE, min(MAX_MAX_SIDE, num))


def clamp_quality(value: Optional[int]) -> int:
    """把 JPEG 质量收进合法区间。"""
    try:
        num = int(value) if value is not None else DEFAULT_QUALITY
    except (TypeError, ValueError):
        num = DEFAULT_QUALITY
    return max(40, min(95, num))


def shrink_to_jpeg(
    source: Source,
    dst: Optional[Union[str, Path]] = None,
    max_side: Optional[int] = None,
    quality: Optional[int] = None,
    skip_if_smaller_than: int = 0,
) -> Optional[bytes]:
    """把图片缩小并转成 JPEG。

    Args:
        source: 原图，路径或字节均可。
        dst:    输出路径。为 None 时只返回字节、不落盘。
        max_side: 长边上限，超出才缩（小图不放大）。
        quality:  JPEG 质量 40~95。
        skip_if_smaller_than: 原图小于此字节数时直接放弃压缩（返回 None），
                              用于避免对已经很小、或前端已压过的图重复编码。

    Returns:
        压缩后的 JPEG 字节。
        返回 None 表示「不适合压缩」，调用方应保留原图 —— 可能的原因：
        动图、原图已足够小、PIL 不可用、解不开的格式。
    """
    max_side = clamp_max_side(max_side)
    quality = clamp_quality(quality)

    # PIL 延迟导入：缺失时安静回退，不影响插件其余功能
    try:
        from PIL import Image, ImageOps
    except Exception:
        logger.debug("[moment] Pillow 不可用，跳过图片压缩")
        return None

    try:
        if isinstance(source, (bytes, bytearray)):
            source = io.BytesIO(bytes(source))

        if skip_if_smaller_than > 0:
            size = _source_size(source)
            if size is not None and size < skip_if_smaller_than:
                logger.debug(f"[moment] 图片仅 {size / 1024:.1f}KB，无需压缩")
                return None

        with Image.open(source) as im:
            # 动图不能压：JPEG 只有一帧
            if getattr(im, "is_animated", False):
                logger.debug("[moment] 动图跳过压缩")
                return None

            # 手机竖拍照片带 EXIF 方向，先摆正再缩放，否则成品会躺倒
            im = ImageOps.exif_transpose(im) or im

            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            im = im.convert("RGBA" if has_alpha else "RGB")

            # 长边超限才缩，小图不放大（放大只会变糊还更占体积）
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.LANCZOS)

            # JPEG 无 alpha 通道，透明区域直接转会变黑，先合成到白底
            if has_alpha:
                canvas = Image.new("RGB", im.size, (255, 255, 255))
                canvas.paste(im, mask=im.split()[-1])
                im = canvas

            buf = io.BytesIO()
            im.save(
                buf,
                "JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
    except Exception:
        logger.debug("[moment] 图片压缩失败，保留原图", exc_info=True)
        return None

    data = buf.getvalue()
    if not data:
        return None

    if dst is not None:
        try:
            Path(dst).write_bytes(data)
        except Exception:
            logger.exception("[moment] 压缩图落盘失败")
            return None

    return data


def _source_size(source: Source) -> Optional[int]:
    """尽力猜出原图体积，猜不到返回 None。"""
    try:
        if isinstance(source, (bytes, bytearray)):
            return len(source)
        if isinstance(source, io.BytesIO):
            pos = source.tell()
            source.seek(0, 2)
            size = source.tell()
            source.seek(pos)
            return size
        return Path(source).stat().st_size
    except Exception:
        return None


def probe_size(data: bytes) -> Optional[tuple]:
    """读一下图片的宽高，失败返回 None。仅用于日志与自检。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None
