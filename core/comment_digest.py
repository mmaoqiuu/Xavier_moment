"""评论区 → 私聊 的摘要构建（纯函数，便于单测）。

只做一件事：把一条动态下「别人来过、他说过什么」压成一段可注入的文本。
不读库、不写库、不碰配置，输入输出都是普通数据。

设计约定（v0.15.0）：
- 不包含用户自己的评论：她本来就知道自己说了什么，回灌反而像复读。
- NPC 与他自己的话混在一起按时间排，人名直接用昵称，不出现「NPC」字样，
  他读起来就是「朋友圈里谁谁来过」。
- 一条动态里一个 NPC 只保留最后一条发言，避免摘要被同一个人刷满。
- 全是用户发言（没有任何 NPC / 他自己的话）时返回 None，表示无需注入。
"""
from __future__ import annotations

from datetime import datetime


POST_SNIPPET_CHARS = 40
COMMENT_SNIPPET_CHARS = 60
MAX_PER_NPC = 1


def _clock(value: str) -> str:
    """ISO 时间串 → HH:MM；解析不了就返回空串（宁可少写个时间点）。"""
    try:
        return datetime.fromisoformat(value).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + "…" if len(text) > limit else text


def build_digest_text(
    post: dict,
    comments: list[dict],
    *,
    max_comments: int = 5,
) -> str | None:
    """生成摘要；没有可讲的内容时返回 None。

    Args:
        post: 动态行（需要 content / created_at）
        comments: 该动态下的评论，按时间升序（db.get_comments 的顺序）
        max_comments: 摘要里最多写几条（取最新的几条）
    """
    if not post:
        return None

    # 1. 只留「别人来过」的部分：用户自己的发言跳过
    interesting = [
        c for c in (comments or [])
        if isinstance(c, dict) and c.get("author") in ("npc", "ai")
    ]

    # 2. 同一个 NPC 只留最后一条；他自己的回复全部保留（通常也就一两条）
    picked: list[dict] = []
    seen_npc: set[str] = set()
    for comment in reversed(interesting):
        if comment.get("author") == "npc":
            name = str(comment.get("author_name") or "有人")
            if name in seen_npc:
                continue
            seen_npc.add(name)
        picked.append(comment)
    picked.reverse()

    limit = max(1, int(max_comments))
    picked = picked[-limit:]

    # 3. 只剩他自己的话时也值得提（NPC 都被裁掉的情况很少见，但不留空摘要）
    if not picked:
        return None

    names = {
        int(c["id"]): (c.get("author_name") or "")
        for c in (comments or [])
        if isinstance(c, dict) and c.get("id") is not None
    }

    header_time = _clock(post.get("created_at", ""))
    header = "【朋友圈】你在 " + (header_time or "刚才") + " 发了一条动态："
    body = [f"「{_clip(post.get('content', ''), POST_SNIPPET_CHARS)}」"]

    for comment in picked:
        who = str(comment.get("author_name") or "").strip()
        text = _clip(comment.get("content", ""), COMMENT_SNIPPET_CHARS)
        if comment.get("author") == "npc":
            body.append(f"- {who or '有人'}：{text}")
            continue
        # 他自己在评论区的发言：说明是回谁
        target = names.get(int(comment.get("parent_comment_id") or 0), "")
        body.append(f"- 你回了{target}：{text}" if target else f"- 你说了句：{text}")

    return header + "\n" + "\n".join(body)
