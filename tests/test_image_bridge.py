"""小回相机桥接层单测（离线：不联网、不依赖 AstrBot 运行时）。

这里只关心一件事：
    配图这条路无论怎么坏，都不能连累发帖。

所以每条用例检查的不是「图好不好看」，
而是「坏掉的时候有没有安静地返回空字符串、有没有抛异常」。
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.image_bridge import ImageBridge, CAMERA_PLUGIN_NAME


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# 脚手架
# ----------------------------------------------------------------------


class FakeCamera:
    """够用的假小回相机。"""

    def __init__(self, *, result=None, raises=None, delay=0.0, ratio="3:4"):
        self.default_ratio = ratio
        self._result = result
        self._raises = raises
        self._delay = delay
        self.calls = []

    async def _generate_image(self, prompt, ratio, ref_path):
        self.calls.append({"prompt": prompt, "ratio": ratio, "ref_path": ref_path})
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise self._raises
        return self._result


def make_context(camera=None, *, activated=True, with_instance=True, version="v1.3.0"):
    """造一个假的 AstrBot Context。"""
    if camera is None and not with_instance:
        meta = None
    else:
        meta = SimpleNamespace(
            name=CAMERA_PLUGIN_NAME,
            version=version,
            activated=activated,
            star_cls=camera if with_instance else None,
        )
    ctx = SimpleNamespace()
    ctx.get_registered_star = lambda name: meta
    return ctx


def make_bridge(tmp_path, camera=None, *, config=None, llm=None, **ctx_kwargs):
    conf = {
        "image_generate_enabled": True,
        "image_generate_probability": 1.0,
        "image_generate_style_hint": "",
        "image_generate_timeout": 90,
    }
    conf.update(config or {})
    return ImageBridge(
        context=make_context(camera, **ctx_kwargs),
        config=conf,
        data_dir=tmp_path,
        llm_caller=llm,
    )


async def default_llm(prompt, system_prompt=""):
    return "窗台上的咖啡杯，午后暖光"


# ----------------------------------------------------------------------
# 各种「安静跳过」
# ----------------------------------------------------------------------


def test_disabled_never_touches_camera(tmp_path):
    camera = FakeCamera(result=tmp_path / "never.png")
    bridge = make_bridge(
        tmp_path, camera, config={"image_generate_enabled": False}, llm=default_llm
    )
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls == []


def test_probability_zero(tmp_path):
    camera = FakeCamera(result=tmp_path / "x.png")
    bridge = make_bridge(
        tmp_path, camera, config={"image_generate_probability": 0.0}, llm=default_llm
    )
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls == []


def test_plugin_missing(tmp_path):
    bridge = make_bridge(tmp_path, None, with_instance=False, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""


def test_plugin_not_activated(tmp_path):
    camera = FakeCamera(result=tmp_path / "x.png")
    bridge = make_bridge(tmp_path, camera, activated=False, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls == []


def test_star_cls_missing(tmp_path):
    """插件刚载入、实例还没挂上时不能崩。"""
    camera = FakeCamera(result=tmp_path / "x.png")
    bridge = make_bridge(tmp_path, camera, with_instance=False, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""


def test_incompatible_camera_version(tmp_path):
    """老版本没有 _generate_image，必须安静跳过。"""
    old_camera = SimpleNamespace(default_ratio="3:4")
    bridge = make_bridge(tmp_path, old_camera, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""


def test_camera_raises(tmp_path):
    camera = FakeCamera(raises=RuntimeError("所有生图接口都失败"))
    bridge = make_bridge(tmp_path, camera, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""


def test_camera_timeout(tmp_path, monkeypatch):
    camera = FakeCamera(result=tmp_path / "slow.png", delay=0.5)
    bridge = make_bridge(tmp_path, camera, llm=default_llm)
    monkeypatch.setattr(bridge, "_timeout", lambda: 0.05)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls, "超时前确实应该已经发起过出图"


def test_camera_returns_none(tmp_path):
    """没配生图 provider 时它会返回 None。"""
    camera = FakeCamera(result=None)
    bridge = make_bridge(tmp_path, camera, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""


def test_camera_returns_missing_file(tmp_path):
    camera = FakeCamera(result=tmp_path / "not_here.png")
    bridge = make_bridge(tmp_path, camera, llm=default_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""


def test_llm_raises(tmp_path):
    camera = FakeCamera(result=tmp_path / "x.png")

    async def bad_llm(prompt, system_prompt=""):
        raise RuntimeError("provider 挂了")

    bridge = make_bridge(tmp_path, camera, llm=bad_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls == [], "拍摄指令都没写出来，不该去调出图"


def test_llm_returns_blank(tmp_path):
    camera = FakeCamera(result=tmp_path / "x.png")

    async def blank_llm(prompt, system_prompt=""):
        return "   "

    bridge = make_bridge(tmp_path, camera, llm=blank_llm)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls == []


def test_no_llm_caller(tmp_path):
    camera = FakeCamera(result=tmp_path / "x.png")
    bridge = make_bridge(tmp_path, camera, llm=None)
    assert run(bridge.try_generate_for_post("今天下雨")) == ""
    assert camera.calls == []


@pytest.mark.parametrize(
    "exc", [RuntimeError("x"), ValueError("y"), KeyError("z"), OSError("w")]
)
def test_never_raises_whatever_camera_throws(tmp_path, exc):
    camera = FakeCamera(raises=exc)
    bridge = make_bridge(tmp_path, camera, llm=default_llm)
    assert run(bridge.try_generate_for_post("随便写点什么")) == ""


# ----------------------------------------------------------------------
# 正常路径
# ----------------------------------------------------------------------


def test_success_copies_into_images_dir(tmp_path):
    src = tmp_path / "xhc_20261001_120000.png"
    src.write_bytes(b"fake-png-bytes")
    camera = FakeCamera(result=src)
    bridge = make_bridge(tmp_path, camera, llm=default_llm)

    name = run(bridge.try_generate_for_post("深夜的便利店关东煮真香", "开心"))

    assert name.startswith("ai_") and name.endswith(".png")
    saved = tmp_path / "images" / name
    assert saved.exists()
    assert saved.read_bytes() == b"fake-png-bytes"
    # 原图仍在小回相机那边，我们只是拷了一份
    assert src.exists()


def test_ratio_and_no_reference_passed_through(tmp_path):
    src = tmp_path / "x.png"
    src.write_bytes(b"x")
    camera = FakeCamera(result=src, ratio="4:3")
    bridge = make_bridge(tmp_path, camera, llm=default_llm)

    run(bridge.try_generate_for_post("随手拍"))

    call = camera.calls[0]
    assert call["ratio"] == "4:3"
    assert call["ref_path"] is None, "无人画面不该喂参考图"


def test_style_hint_appended(tmp_path):
    src = tmp_path / "x.png"
    src.write_bytes(b"x")
    camera = FakeCamera(result=src)
    bridge = make_bridge(
        tmp_path, camera, config={"image_generate_style_hint": "偏冷色调"}, llm=default_llm
    )

    run(bridge.try_generate_for_post("随手拍"))

    assert camera.calls[0]["prompt"].endswith("偏冷色调")


def test_missing_ratio_falls_back(tmp_path):
    src = tmp_path / "x.png"
    src.write_bytes(b"x")
    camera = FakeCamera(result=src)
    del camera.default_ratio
    bridge = make_bridge(tmp_path, camera, llm=default_llm)

    run(bridge.try_generate_for_post("随手拍"))

    assert camera.calls[0]["ratio"] == "3:4"


def test_unknown_ext_is_normalized(tmp_path):
    src = tmp_path / "weird.bmp"
    src.write_bytes(b"x")
    camera = FakeCamera(result=src)
    bridge = make_bridge(tmp_path, camera, llm=default_llm)

    name = run(bridge.try_generate_for_post("随手拍"))

    assert name.endswith(".png")


def test_shot_prompt_forbids_people(tmp_path):
    """喂给 LLM 的模板里必须写明不要出现人，否则容易拍出人物。"""
    src = tmp_path / "x.png"
    src.write_bytes(b"x")
    seen = {}

    async def spy_llm(prompt, system_prompt=""):
        seen["prompt"] = prompt
        return "窗台咖啡杯"

    bridge = make_bridge(tmp_path, FakeCamera(result=src), llm=spy_llm)
    run(bridge.try_generate_for_post("随手拍"))
    assert "不要出现人" in seen["prompt"]


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ('"窗台咖啡杯"', "窗台咖啡杯"),
        ("「窗台咖啡杯」", "窗台咖啡杯"),
        ("  1. 窗台咖啡杯  ", "窗台咖啡杯"),
        ("窗台\n咖啡杯", "窗台 咖啡杯"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_line(raw, expected):
    assert ImageBridge._clean_line(raw) == expected
