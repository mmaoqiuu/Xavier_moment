"""图片压缩工具的测试。

这里的重点是「坏掉的时候怎么办」：压缩只是优化，压不动必须安静回退，
不能让一张图把发帖流程带崩。所以用例大量集中在各种异常输入上。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.image_utils import (  # noqa: E402
    clamp_max_side,
    clamp_quality,
    probe_size,
    shrink_to_jpeg,
)

PIL = pytest.importorskip("PIL", reason="压缩功能依赖 Pillow")
from PIL import Image  # noqa: E402


def make_image(width, height, mode="RGB", fmt="PNG", **kwargs):
    """造一张有内容的图。纯色能压得极小，所以铺点噪声。"""
    import random

    random.seed(width * height)
    if mode == "RGBA":
        im = Image.new("RGBA", (width, height))
        px = im.load()
        for x in range(0, width, 3):
            for y in range(0, height, 3):
                px[x, y] = (
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                )
    else:
        im = Image.new("RGB", (width, height))
        px = im.load()
        for x in range(0, width, 3):
            for y in range(0, height, 3):
                px[x, y] = (
                    random.randint(0, 255),
                    random.randint(0, 255),
                    random.randint(0, 255),
                )

    buf = io.BytesIO()
    im.save(buf, fmt, **kwargs)
    return buf.getvalue()


# ----------------------------------------------------------------------
# 参数收敛
# ----------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "abc", 0, -5, 10, 99999])
def test_max_side_always_in_range(raw):
    assert 480 <= clamp_max_side(raw) <= 4096


@pytest.mark.parametrize("raw", [None, "x", -1, 1, 100])
def test_quality_always_in_range(raw):
    assert 40 <= clamp_quality(raw) <= 95


def test_defaults_are_sane():
    assert clamp_max_side(None) == 1280
    assert clamp_quality(None) == 85


# ----------------------------------------------------------------------
# 正常路径
# ----------------------------------------------------------------------


def test_big_image_shrinks_and_becomes_jpeg():
    raw = make_image(2400, 1800)
    assert len(raw) > 500 * 1024

    out = shrink_to_jpeg(raw, max_side=1280, quality=85)

    assert out is not None
    assert out[:2] == b"\xff\xd8", "JPEG 应当以 SOI 标记开头"
    assert len(out) < len(raw) / 3, "压缩后应显著变小"
    assert probe_size(out)[0] <= 1280


def test_writes_to_disk_when_dst_given(tmp_path):
    dst = tmp_path / "out.jpg"
    out = shrink_to_jpeg(make_image(1600, 1200), dst)

    assert out is not None
    assert dst.exists()
    assert dst.read_bytes() == out


def test_small_image_not_upscaled():
    """小图只转格式，不该被放大。"""
    raw = make_image(320, 240)
    out = shrink_to_jpeg(raw, max_side=1280)

    assert probe_size(out) == (320, 240)


def test_accepts_path_source(tmp_path):
    src = tmp_path / "in.png"
    src.write_bytes(make_image(1600, 1200))

    out = shrink_to_jpeg(src)

    assert out is not None
    assert probe_size(out)[0] <= 1280


def test_transparency_flattened_to_white():
    """JPEG 没有 alpha，透明图必须铺白底，否则会变黑块。

    左侧挖空成完全透明，右侧保持实色：只断言透明那半边变白，
    不碰实色那半边，避免和噪声混色搅在一起。
    """
    im = Image.new("RGBA", (600, 600), (200, 30, 30, 255))
    for x in range(300):
        for y in range(600):
            im.putpixel((x, y), (0, 0, 0, 0))

    buf = io.BytesIO()
    im.save(buf, "PNG")

    out = shrink_to_jpeg(buf.getvalue())

    assert out is not None
    result = Image.open(io.BytesIO(out))
    assert result.mode == "RGB"
    assert result.getpixel((10, 10)) == (255, 255, 255), "透明区域应铺成白底"
    assert result.getpixel((590, 590))[0] > 150, "实色区域应保持原色"


# ----------------------------------------------------------------------
# 安静回退（返回 None = 保留原图）
# ----------------------------------------------------------------------


def test_skip_when_smaller_than_threshold():
    raw = make_image(800, 600)
    assert shrink_to_jpeg(raw, skip_if_smaller_than=100 * 1024 * 1024) is None


def test_animated_gif_is_skipped():
    """动图不能压成 JPEG，否则动画只剩首帧。"""
    frames = [Image.new("RGB", (200, 200), (i * 40, 0, 0)) for i in range(3)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:])

    assert shrink_to_jpeg(buf.getvalue()) is None


@pytest.mark.parametrize(
    "garbage",
    [b"", b"not an image at all", b"\x89PNG\r\n\x1a\n truncated", b"\xff\xd8\xff"],
)
def test_garbage_returns_none_without_raising(garbage):
    assert shrink_to_jpeg(garbage) is None


def test_missing_file_returns_none(tmp_path):
    assert shrink_to_jpeg(tmp_path / "nope.png") is None


def test_bad_dst_returns_none(tmp_path):
    """目标路径不可写时不能抛异常。"""
    raw = make_image(1600, 1200)
    assert shrink_to_jpeg(raw, tmp_path / "no_such_dir" / "out.jpg") is None


def test_probe_size_on_garbage():
    assert probe_size(b"nope") is None
