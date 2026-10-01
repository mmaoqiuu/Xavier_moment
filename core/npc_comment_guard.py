"""NPC 评论的串行守卫（离线可测，不依赖 AstrBot 运行时）。

为什么需要：_do_npc_comment 的流程是「查重 -> 调模型生成（好几秒）-> 落库」，
这几步之间都有 await。如果同一个人排了两个任务又恰好同时到点，两边都会在
对方写入之前通过查重，于是同一条动态下冒出两条一模一样的话（同一份上下文
喂了两遍模型，内容自然雷同）。

一把按 (post_id, npc_name) 的锁就够了：后到的那条一进来，前一条已经落库，
查重就能拦住它。

单独放一个模块，是为了能脱离 AstrBot 运行时做并发单测。
"""

import asyncio


def lock_key(post_id, npc_name) -> str:
    """同一对人共用一把锁；动态或人名不同则互不干扰。"""
    return f"{post_id}:{npc_name}"


class NpcCommentGuard:
    """按「动态 + 人名」发锁，保证同一对 (post_id, npc_name) 串行执行。"""

    def __init__(self, max_keys: int = 1000):
        self._locks: dict[str, asyncio.Lock] = {}
        self._max_keys = max_keys

    def lock_for(self, post_id, npc_name) -> asyncio.Lock:
        """取（必要时创建）这一对专属的锁。"""
        if len(self._locks) > self._max_keys:
            # 跑久了清一遍没人排队的锁，别让字典无限长大
            self._locks = {k: v for k, v in self._locks.items() if v.locked()}
        return self._locks.setdefault(lock_key(post_id, npc_name), asyncio.Lock())

    @property
    def size(self) -> int:
        return len(self._locks)
