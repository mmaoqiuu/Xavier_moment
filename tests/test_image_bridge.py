"""小回相机桥接层单测（离线：不联网、不依赖 AstrBot 运行时）。

这里只关心一件事：
    配图这条路无论怎么坏，都不能连累发帖。

所以每条用例检查的不是「图好不好看」，
而是「坏掉的时候有没有安静地返回空字符串、有没有抛异常」。
"""
import asyncio
import os
import sys
from pathlib import Path
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
        "image_use_reference": True,
        "image_reference_hint": "",
        "image_reference_timeout": 150,
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


# ----------------------------------------------------------------------
# v2.1.0：配图读参考图
# ----------------------------------------------------------------------


class RefCamera(FakeCamera):
    """带参考图检索的小回相机假件（方法名与真件一致）。"""

    def __init__(self, *, refs=(), reference_dir="", fallback=False, raises_with_ref=None, **kw):
        super().__init__(**kw)
        self._refs = [Path(r) for r in refs]
        self.reference_dir = str(reference_dir)
        self.fallback_to_generations_when_reference_fails = fallback
        self._raises_with_ref = raises_with_ref
        self.searches = []
        self.scenes = []
        self.built = []

    def _infer_scene(self, want):
        return "daily_no_face"

    def _detect_requested_objects(self, want):
        return []

    def _find_reference_images(self, want, scene, hint=""):
        self.scenes.append({"want": want, "scene": scene, "hint": hint})
        return list(self._refs)

    def _build_prompt(
        self, want, ratio, scene, requested_objects, has_reference, ref_path, ref_paths=None
    ):
        self.built.append(
            {
                "want": want,
                "ratio": ratio,
                "has_reference": has_reference,
                "ref_paths": list(ref_paths or []),
            }
        )
        ref_name = Path(ref_path).name if ref_path else None
        return f"CAMERA-PROMPT[{want}][ref={ref_name}]", ratio or "3:4", scene

    def _search_reference_by_text(self, root, text, strong=False):
        self.searches.append(text)
        for ref in self._refs:
            if text in str(ref):
                return ref
        return None

    async def _generate_image(self, prompt, ratio, ref_path):
        if self._raises_with_ref is not None and ref_path is not None:
            self.calls.append({"prompt": prompt, "ratio": ratio, "ref_path": ref_path})
            raise self._raises_with_ref
        return await super()._generate_image(prompt, ratio, ref_path)


def test_reference_is_used_when_found(tmp_path):
    ref = tmp_path / "露台参考" / "露台-午后.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = RefCamera(refs=[ref], result=src)

    name = run(make_bridge(tmp_path, camera, llm=default_llm).try_generate_for_post("露台的午后"))

    assert name, "命中参考图后应该照常出图"
    call = camera.calls[0]
    assert call["ref_path"] == ref
    assert call["prompt"].startswith("CAMERA-PROMPT["), "命中参考图应改用相机自己的提示词"
    assert camera.built[0]["has_reference"] is True
    assert camera.built[0]["ref_paths"] == [ref]


def test_reference_can_be_switched_off(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = RefCamera(refs=[ref], result=src)
    bridge = make_bridge(tmp_path, camera, config={"image_use_reference": False}, llm=default_llm)

    run(bridge.try_generate_for_post("露台的午后"))

    assert camera.scenes == [], "关掉开关后不该去检索参考图"
    assert camera.calls[0]["ref_path"] is None


def test_reference_timeout_used_when_reference(tmp_path, monkeypatch):
    """带参考图时要用 image_reference_timeout 那套超时。"""
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    camera = RefCamera(refs=[ref], result=tmp_path / "slow.png", delay=0.5)
    bridge = make_bridge(tmp_path, camera, llm=default_llm)

    seen = {}
    monkeypatch.setattr(
        bridge, "_ref_timeout", lambda: seen.setdefault("ref", 0.05) or 0.05
    )
    monkeypatch.setattr(
        bridge, "_timeout", lambda: seen.setdefault("plain", 9.0) or 9.0
    )

    assert run(bridge.try_generate_for_post("露台的午后")) == ""
    assert seen["ref"] == 0.05, "带参考图应该走参考图超时，而不是被 9 秒放过去"
    assert camera.calls and camera.calls[0]["ref_path"] == ref


def test_reference_failure_retries_without_reference_when_camera_allows(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = RefCamera(
        refs=[ref], result=src, fallback=True, raises_with_ref=RuntimeError("edits 挂了")
    )

    name = run(make_bridge(tmp_path, camera, llm=default_llm).try_generate_for_post("露台"))

    assert name, "对方允许降级时应该无参考图重试成功"
    assert [c["ref_path"] for c in camera.calls] == [ref, None]


def test_reference_failure_keeps_no_image_when_camera_forbids_fallback(tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = RefCamera(refs=[ref], result=src, raises_with_ref=RuntimeError("edits 挂了"))

    assert run(make_bridge(tmp_path, camera, llm=default_llm).try_generate_for_post("露台")) == ""
    assert len(camera.calls) == 1, "对方禁止降级时不该偷偷无参考重试"


def test_reference_folder_name_used_as_fallback_keyword(tmp_path):
    """拍摄指令里写到参考库文件夹名时，直接取那个文件夹的图。"""
    lib = tmp_path / "生图参考"
    ref = lib / "露台参考" / "露台-傍晚.jpg"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")

    class HardToFindCamera(RefCamera):
        def _find_reference_images(self, want, scene, hint=""):
            self.scenes.append({"want": want, "scene": scene, "hint": hint})
            return []

    camera = HardToFindCamera(refs=[ref], reference_dir=lib, result=src)

    async def shot_llm(prompt, system_prompt=""):
        return "露台的傍晚，风把桌布吹起来一点"

    name = run(
        make_bridge(tmp_path, camera, llm=shot_llm).try_generate_for_post("露台的傍晚")
    )

    assert name
    assert camera.calls[0]["ref_path"] == ref, "名字对得上就该喂它的参考图"


def test_manual_hint_keyword_is_used(tmp_path):
    lib = tmp_path / "生图参考"
    ref = lib / "兔球球" / "bunny.png"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")

    class EmptyCamera(RefCamera):
        def _find_reference_images(self, want, scene, hint=""):
            self.scenes.append({"want": want, "scene": scene, "hint": hint})
            return []

    camera = EmptyCamera(refs=[ref], reference_dir=lib, result=src)
    bridge = make_bridge(
        tmp_path, camera, config={"image_reference_hint": "兔球球"}, llm=default_llm
    )

    run(bridge.try_generate_for_post("窗边有一本书"))

    assert camera.calls[0]["ref_path"] == ref, "点名了就得用这个文件夹的图"
    assert "兔球球" in camera.built[0]["want"]
    assert camera.calls[0]["ref_path"] == ref


def test_reference_methods_missing_still_works(tmp_path):
    """对方是老版本、没有检索方法时，必须安静退回旧路径。"""
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = SimpleNamespace(default_ratio="3:4")

    async def generate(prompt, ratio, ref_path):
        camera.calls = getattr(camera, "calls", [])
        camera.calls.append({"prompt": prompt, "ref_path": ref_path})
        return src

    camera._generate_image = generate

    name = run(make_bridge(tmp_path, camera, llm=default_llm).try_generate_for_post("随手拍"))

    assert name
    assert camera.calls[0]["ref_path"] is None


@pytest.mark.parametrize("exc", [RuntimeError("x"), OSError("y"), ValueError("z")])
def test_reference_lookup_errors_are_swallowed(tmp_path, exc):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"ref")
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")

    class BoomCamera(RefCamera):
        def _find_reference_images(self, want, scene, hint=""):
            raise exc

        def _search_reference_by_text(self, root, text, strong=False):
            raise exc

    camera = BoomCamera(refs=[ref], reference_dir=tmp_path, result=src)

    name = run(make_bridge(tmp_path, camera, llm=default_llm).try_generate_for_post("露台"))

    assert name, "参考图这一步炸了也要照常出图（只是不带参考图）"
    assert camera.calls[0]["ref_path"] is None


def test_hint_keywords_split():
    assert ImageBridge._hint_keywords("露台, 兔球球；客厅") == ["露台", "兔球球", "客厅"]
    assert ImageBridge._hint_keywords("") == []


@pytest.mark.parametrize(
    "folder,expected",
    [
        ("露台参考", ["露台"]),
        ("兔球球", ["兔球球"]),
        ("手部参考图", ["手部"]),
        ("参考", []),
        ("", []),
    ],
)
def test_keywords_of_folder(folder, expected):
    assert ImageBridge._keywords_of(folder) == expected


# ----------------------------------------------------------------------
# v2.1.1：参考库文件夹是自由命名的（「兔球球」），相机自己认不出来
# ----------------------------------------------------------------------


class FolderOnlyCamera(RefCamera):
    """真相机的行为：拍摄指令里没有它的主体大类关键词时，检索直接落空。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.folder_picks = []

    def _find_reference_images(self, want, scene, hint=""):
        self.scenes.append({"want": want, "scene": scene, "hint": hint})
        return []

    def _search_reference_by_text(self, root, text, strong=False):
        self.searches.append(text)
        return None

    def _pick_from_folder_by_text(
        self, ref_root, folder_name, text, fallback_primary=True, allow_random=True
    ):
        folder = Path(ref_root) / folder_name
        files = sorted(f for f in folder.iterdir() if f.is_file())
        if not files:
            return None
        self.folder_picks.append(folder_name)
        return files[0]


def make_plush_library(tmp_path):
    """参考库里只有自由命名的「兔球球」文件夹。"""
    root = tmp_path / "生图参考"
    doll = root / "兔球球" / "兔球球-阳台.png"
    doll.parent.mkdir(parents=True, exist_ok=True)
    doll.write_bytes(b"ref")
    return root, doll


def test_named_folder_used_when_camera_finds_nothing(tmp_path):
    """动态里写到「兔球球」，相机认不出来，也要带上它的参考图。"""
    root, doll = make_plush_library(tmp_path)
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = FolderOnlyCamera(result=src, reference_dir=root)

    async def llm(prompt, system_prompt=""):
        return "阳台藤椅上晒着的兔球球，午后暖光"

    name = run(make_bridge(tmp_path, camera, llm=llm).try_generate_for_post("晒玩偶"))

    assert name
    assert camera.calls[0]["ref_path"] == doll
    assert camera.built and camera.built[0]["has_reference"] is True


def test_named_folder_used_when_hint_points_at_it(tmp_path):
    """配置里点名「兔球球」时，即使指令里没写也得带上参考图。"""
    root, doll = make_plush_library(tmp_path)
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = FolderOnlyCamera(result=src, reference_dir=root)
    bridge = make_bridge(
        tmp_path, camera, config={"image_reference_hint": "兔球球"}, llm=default_llm
    )

    name = run(bridge.try_generate_for_post("晒太阳"))

    assert name
    assert camera.calls[0]["ref_path"] == doll
    assert "兔球球" in camera.built[0]["want"], "点名的关键词要写进交给相机的拍摄指令"


def test_unrelated_post_takes_no_reference(tmp_path):
    """跟参考库对不上的动态，不许硬塞参考图。"""
    root, _doll = make_plush_library(tmp_path)
    src = tmp_path / "shot.png"
    src.write_bytes(b"x")
    camera = FolderOnlyCamera(result=src, reference_dir=root)

    async def llm(prompt, system_prompt=""):
        return "书桌上的台灯，暖黄光线"

    name = run(make_bridge(tmp_path, camera, llm=llm).try_generate_for_post("看了一页书"))

    assert name, "没参考图也要照常出图"
    assert camera.calls[0]["ref_path"] is None


def test_library_names_are_offered_to_llm(tmp_path):
    """参考库里的名字要告诉模型，它才写得出「兔球球」。"""
    root, _doll = make_plush_library(tmp_path)
    camera = FolderOnlyCamera(result=tmp_path / "shot.png", reference_dir=root)
    seen = {}

    async def llm(prompt, system_prompt=""):
        seen["prompt"] = prompt
        return "阳台上的兔球球"

    run(make_bridge(tmp_path, camera, llm=llm).try_generate_for_post("晒太阳"))

    assert "兔球球" in seen["prompt"]


@pytest.mark.parametrize(
    "folder,keyword,expected",
    [
        ("兔球球", "兔球球", True),
        ("兔球球", "球球", True),
        ("小狗玩偶参考", "小狗玩偶", True),
        ("露台参考", "露台", True),
        ("露台参考", "客厅", False),
        ("兔球球", "台灯", False),
    ],
)
def test_folder_matches(folder, keyword, expected):
    assert ImageBridge._folder_matches(folder, keyword) is expected
