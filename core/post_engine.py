"""AstrBot Moment Plugin — AI 发帖引擎

负责使用收集到的素材，通过 LLM 生成一条原创动态。
"""
from __future__ import annotations

import random
from typing import Optional
from astrbot.api import logger


# 内置默认发帖风格提示词
DEFAULT_POST_STYLE_PROMPT = """【发帖核心指令】
你现在要完全代入你的人设，以第一人称写一条简短的朋友圈动态（50~150字）。

你抽到了今日的发帖主题：【{topic}】

请围绕这个主题，自由选择以下**任意一种**切入角度进行发挥：
- **生活经历**：分享今天遇到了什么、做了什么、或者某个瞬间的联想。
- **内心独白**：就这个主题发表一段抽象的思考、情绪宣泄或趣味吐槽。
- **消费分享**：如果在你的设定里你会看书、听歌、看电影或打游戏，你可以分享相关的体验。

【铁律警告（违者判定为失败）】
1. **关于作品**：如果你在发帖中具体点名了某本书、某首歌、某部电影、游戏等实体作品，**该作品必须在现实三次元世界中真实存在并广为人知，绝对禁止虚构、拼接或编造作品名**。如果不确定是否真实存在，请不要提具体名字。
2. **关于语气**：严禁像写报告或写作文一样开头（如"今天聊聊..."、"关于..."或"今天我要分享"），必须像真人在微信朋友圈里随手发的闲言碎语。
3. **格式约束**：不要 @ 任何人，不要提及"记忆"、"系统"等概念。偶尔可以用 0~2 个 emoji。"""

class PostEngine:
    """AI 发帖生成引擎。"""

    def __init__(self, context, config: dict):
        self.context = context
        self.config = config

    async def generate_post(self, materials: dict) -> Optional[dict]:
        """基于收集到的素材，用 LLM 生成一条动态。

        Args:
            materials: MaterialCollector.collect() 的返回值

        Returns:
            {"content": str, "mood": str} 或 None（生成失败时）
        """
        try:
            # 构建 prompt
            prompt = self._build_prompt(materials)

            # 调用 LLM
            # 用人设作为 system_prompt，让 AI 始终保持角色
            persona = materials.get("persona", "")
            response_text = await self._call_llm(prompt, system_prompt=persona)
            if not response_text:
                return None

            # 清理 LLM 输出
            content = self._clean_llm_output(response_text)

            # 如果太短或太长，裁剪
            max_len = int(self.config.get("max_post_length", 280))
            if len(content) < 5:
                return None
            if len(content) > max_len:
                content = content[:max_len]

            # 推断心情
            mood = self._infer_mood(content)

            return {"content": content, "mood": mood}

        except Exception as e:
            logger.error(f"[moment] 生成动态失败: {e}")
            return None

    def _build_prompt(self, materials: dict) -> str:
        """构建发帖的 LLM 提示词。"""
        # 获取用户自定义或使用默认
        style_prompt = self.config.get("post_style_prompt", "").strip()
        topic = materials.get("topic", "日常")
        
        if not style_prompt:
            style_prompt = DEFAULT_POST_STYLE_PROMPT.replace("{topic}", topic)
        else:
            # 如果用户有自定义的提示词，我们在最前面强行把主题塞进去
            style_prompt = f"【今日发帖主题：{topic}】\n\n" + style_prompt

        parts = [style_prompt]

        # 添加人设信息（让 AI 保持角色一致性）
        persona = materials.get("persona", "")
        if persona:
            parts.append(f"\n---\n你的人设背景（帮助你保持角色一致，但不要在动态里暴露这些是「设定」）：\n{persona[:500]}")

        # 添加当前时间信息
        current_time = materials.get("current_time", "")
        if current_time:
            parts.append(f"\n---\n【当前时间状态】（发帖时请务必符合此时的作息规律，比如晚上不要去晒太阳）：\n{current_time}")

        # 添加防重复参考
        recent_posts = materials.get("recent_posts", [])
        if recent_posts:
            parts.append("\n---\n【你最近发布过的动态】（请**不要**发和下面类似的内容或相同的话题，必须找点新鲜事）：")
            for i, p in enumerate(recent_posts, 1):
                parts.append(f"{i}. {p}")

        parts.append("\n---\n现在请写一条动态。要求：")
        parts.append("- 直接输出内容，不要加任何前缀、标题、解释")
        parts.append("- 如果需要分段，请直接用换行，不要使用 $ 符号或 [NEXT] 等任何特殊分隔符")
        parts.append("- 语气自然，像真人发朋友圈/微博一样")

        return "\n".join(parts)

    async def _call_llm(self, prompt: str, system_prompt: str = "") -> Optional[str]:
        """调用 AstrBot 的 LLM provider 生成回复。

        Args:
            prompt: 用户提示词（作为 prompt 参数传给 provider）
            system_prompt: 系统提示词（如人设），作为 system_prompt 传给 provider
        """
        try:
            provider = None

            # 优先使用配置指定的 provider
            provider_id = self.config.get("llm_provider_id", "").strip()
            if provider_id:
                # v4 的 provider_manager 挂在 context 上
                provider_mgr = getattr(self.context, "provider_manager", None)
                if provider_mgr and hasattr(provider_mgr, "providers"):
                    # providers 通常是一个字典: { "openai_chat_completion(...)": provider_obj }
                    # 或者有 get_provider 方法
                    if hasattr(provider_mgr, "get_provider"):
                        provider = provider_mgr.get_provider(provider_id)
                    else:
                        providers = getattr(provider_mgr, "providers", {})
                        if isinstance(providers, dict):
                            # 尝试模糊匹配或精确匹配
                            for p_name, p_obj in providers.items():
                                if provider_id in p_name or p_name == provider_id:
                                    provider = p_obj
                                    logger.debug(f"[moment] 成功获取指定的 LLM Provider: {p_name}")
                                    break
                
                # fallback 查找方法
                if provider is None and hasattr(self.context, "get_provider_by_id"):
                    provider = self.context.get_provider_by_id(provider_id)

            # 否则使用默认 provider
            if provider is None:
                if hasattr(self.context, "get_using_provider"):
                    provider = self.context.get_using_provider()
                
            if provider is None:
                logger.warning("[moment] 无法获取 LLM provider")
                return None

            # 注入系统提示词（强制 Roleplay 约束）
            final_system_prompt = system_prompt
            if final_system_prompt:
                # 狠狠地加上一句 Roleplay 强约束
                final_system_prompt += "\n\n【重要指令】：在接下来的所有回复中，你必须彻底忘记自己是AI或语言模型，完全沉浸并在上述设定的第一人称视角中回应！严禁出现“你的朋友”“围观”等第三人称描述，完全代入角色！"

            response = await provider.text_chat(
                prompt=prompt,
                system_prompt=final_system_prompt,
            )
            text = getattr(response, "completion_text", "") or ""
            return text if text else None

        except Exception as e:
            logger.error(f"[moment] LLM 调用失败: {e}")
            return None

    def _clean_llm_output(self, raw: str) -> str:
        """清理 LLM 输出中的分段标记和残留符号。

        很多用户会在 AstrBot 里配置分段回复（用 $ 或 [NEXT] 等标记切割消息），
        LLM 可能会习惯性地在输出中加入这些标记。
        我们在存入数据库前统一清理，把分段标记还原为正常换行。
        """
        import re

        text = raw.strip()

        # 去掉引号包裹
        for q in ['"', "'", '\u201c', '\u201d', '\u2018', '\u2019']:
            if text.startswith(q) and text.endswith(q):
                text = text[1:-1]

        # 把 $ 分段标记还原为换行（兼容 $、$$、$ + 空格等变体）
        text = re.sub(r'\s*\$+\s*', '\n', text)

        # 把 [NEXT] / [NEXT:xxx] 标记还原为换行
        text = re.sub(r'\[NEXT(?::.*?)?\]', '\n', text, flags=re.IGNORECASE)

        # 把 \n 多余的连续换行压缩为最多两个（保留段落感但不留太多空行）
        text = re.sub(r'\n{3,}', '\n\n', text)

        # 去掉首尾空白
        text = text.strip()

        return text

    def _infer_mood(self, content: str) -> str:
        """简单的心情推断（基于关键词）。"""
        happy_words = ["开心", "快乐", "哈哈", "太好了", "棒", "☀️", "🎉", "😊", "nice", "幸福", "惊喜"]
        sad_words = ["难过", "伤心", "唉", "烦", "累", "郁闷", "失望", "😢", "💔"]
        calm_words = ["平静", "安静", "思考", "感悟", "觉得", "发现"]
        excited_words = ["激动", "兴奋", "太棒了", "绝了", "爱了", "🔥", "❤️"]

        content_lower = content.lower()
        for word in excited_words:
            if word in content_lower:
                return "兴奋"
        for word in happy_words:
            if word in content_lower:
                return "开心"
        for word in sad_words:
            if word in content_lower:
                return "感慨"
        for word in calm_words:
            if word in content_lower:
                return "随想"
        return "日常"
