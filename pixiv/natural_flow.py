"""自然语言发图的触发流程：规则优先，模型兜底。

``main.py`` 只保留 ``@filter.regex`` 装饰器与一行调用，具体判定放在这里：

1. 规则解析（``pixiv/natural.py``）能认出就直接发图，零延迟零成本；
2. 规则认不出、且用户开启了模型兜底时，才调用 ``NaturalQueryResolver``；
3. 模型也认不出就静默放过，不 ``stop_event``，让消息继续走别的插件。

这样「大肥鱼来张图」「来三张初音竖图」这类常见说法永远不花 token，
只有写得很绕的句子才会走模型。
"""

from __future__ import annotations

from astrbot.api.all import logger
from astrbot.api.event import AstrMessageEvent

from .natural import (
    looks_like_past_tense,
    looks_like_question,
    parse_natural_query,
    resolve_aliases,
)
from .natural_ai import NaturalQueryResolver
from .orientation import ORIENTATION_ANY


LOG_PREFIX = "[GetPx]"

# 超过这个长度基本不是发图请求，直接放过，避免把长段闲聊送去模型。
MAX_TRIGGER_LENGTH = 40

# 模型判定「不是在要图」的句子缓存条数。同一句闲聊重复出现时不必反复调用。
MAX_REJECTED_CACHE = 512


class NaturalTriggerMixin:
    """自然语言自动触发的判定与分发。"""

    def _auto_trigger_aliases(self) -> list[str]:
        return resolve_aliases(
            self._cfg_str("checkin_bot_name", ""),
            self._cfg_str("auto_trigger_aliases", ""),
        )

    def _ai_fallback_enabled(self) -> bool:
        return self._cfg_bool("auto_trigger_ai_fallback", True)

    async def _handle_auto_trigger(self, event: AstrMessageEvent):
        """判定并执行自然语言发图；不匹配时不产出任何结果。"""
        if not self._cfg_bool("auto_trigger_enabled", False):
            return

        message = str(event.get_message_str() or "").strip()
        if not message or len(message) > MAX_TRIGGER_LENGTH:
            return
        if message.startswith("/"):
            return

        query = parse_natural_query(message, self._auto_trigger_aliases())
        source = "rule"

        if not query.matched:
            if not self._ai_fallback_enabled():
                return
            if looks_like_past_tense(message) or looks_like_question(message):
                return
            if self._ai_rejected(message):
                return
            query = await self._resolve_natural_query(event, message)
            source = "ai"
            if not query.matched:
                self._remember_ai_rejected(message)
                return

        if not self._ensure_client_or_error(event):
            return

        event.stop_event()
        logger.info(
            f"{LOG_PREFIX} 自然语言触发: source={source} "
            f"count={query.count} tag_configured={'yes' if query.tag else 'no'} "
            f"orientation={query.orientation} "
            f"by_id={'yes' if query.illust_id else 'no'} "
            f"alias_prefix={'yes' if query.prefix_is_alias else 'no'}"
        )
        async for result in self._handle_search(
            event,
            tag=query.tag,
            count_str=str(query.count),
            # 句子里没提方向时传空串，让 _handle_search 回落到配置里的
            # image_orientation；直接传 "any" 会把这个默认值顶掉。
            orientation="" if query.orientation == ORIENTATION_ANY else query.orientation,
            illust_id=query.illust_id,
        ):
            yield result

    async def _resolve_natural_query(self, event: AstrMessageEvent, message: str):
        resolver = getattr(self, "_natural_resolver", None)
        if resolver is None:
            resolver = NaturalQueryResolver(self.context)
            self._natural_resolver = resolver
        return await resolver.resolve(
            event,
            message,
            provider_id=self._cfg_str("auto_trigger_ai_provider_id", ""),
            timeout=self._cfg_float("auto_trigger_ai_timeout", 6.0, 1.0, 30.0),
        )

    def _ai_rejected(self, message: str) -> bool:
        cache = getattr(self, "_ai_rejected_cache", None)
        if not cache:
            return False
        return message in cache

    def _remember_ai_rejected(self, message: str) -> None:
        cache = getattr(self, "_ai_rejected_cache", None)
        if cache is None:
            cache = {}
            self._ai_rejected_cache = cache
        if len(cache) >= MAX_REJECTED_CACHE:
            cache.clear()
        cache[message] = True
