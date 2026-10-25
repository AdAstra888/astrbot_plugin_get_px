"""自然语言发图的模型兜底解析。

规则解析（``pixiv/natural.py``）覆盖了绝大多数说法，但用户可能写得
很绕（「能不能帮我从那个蓝色的站弄几张最近很火的风景」）。这种情况
才调用 AstrBot 的 LLM，让它把句子抽成结构化字段。

设计约束：
- 只在规则解析失败时调用，命中规则永不触发模型，避免每条群消息都烧 token。
- 模型只负责「抽取」，不负责「决定要不要发图」；返回里带 ``is_request``
  供调用方二次判断，且任何异常/超时都退化成「不匹配」，绝不阻塞消息流。
- 结果只取标签、数量、方向、作品 ID 四个字段，其余一律忽略，防止模型
  编造指令。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger

from .natural import NaturalQuery
from .orientation import normalize_orientation


LOG_PREFIX = "[GetPx]"

MAX_AI_TAG_LENGTH = 40
MAX_AI_COUNT = 20

NATURAL_QUERY_SYSTEM_PROMPT = (
    "你只负责从聊天消息里抽取「请求发送插画」这一意图的结构化参数。"
    "只输出一个 JSON 对象，不要输出任何解释、Markdown 或代码块标记。"
    '字段固定为 {"is_request": bool, "tag": string, "count": int, '
    '"orientation": string, "illust_id": string}。'
    "is_request 表示这条消息是否在要求发送插画；"
    "tag 是用户想搜索的作品标签（原样保留，不要翻译、不要编造，没有就留空）；"
    "count 是想要的张数（1-20，没说就填 1）；"
    "orientation 只能是 any/landscape/portrait/square，没说就填 any；"
    "illust_id 是用户明确给出的 Pixiv 作品 ID（纯数字），没有就留空。"
    "如果消息不是在要图（例如闲聊、提问、描述已发生的事），"
    'is_request 填 false，其余字段留空或填默认值。'
)


class NaturalQueryResolver:
    """调用 AstrBot 当前会话模型，把自然语言抽成 :class:`NaturalQuery`。"""

    def __init__(self, context: Any):
        self.context = context

    async def resolve(
        self,
        event: Any,
        text: str,
        *,
        provider_id: str = "",
        timeout: float = 6.0,
    ) -> NaturalQuery:
        started_at = time.monotonic()
        resolved_provider = await self._resolve_provider(event, provider_id)
        if not resolved_provider:
            logger.debug(f"{LOG_PREFIX} 自然语言模型解析跳过: reason=no_model")
            return NaturalQuery()

        prompt = f"<message>\n{text}\n</message>"
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=resolved_provider,
                    prompt=prompt,
                    system_prompt=NATURAL_QUERY_SYSTEM_PROMPT,
                    max_tokens=160,
                ),
                timeout=max(float(timeout), 0.001),
            )
        except asyncio.TimeoutError:
            self._log("timeout", started_at)
            return NaturalQuery()
        except Exception as exc:
            self._log("provider_error", started_at, exc)
            return NaturalQuery()

        payload = _extract_json(getattr(response, "completion_text", ""))
        if payload is None:
            self._log("invalid_response", started_at)
            return NaturalQuery()

        result = _to_natural_query(payload)
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        logger.info(
            f"{LOG_PREFIX} 自然语言模型解析完成: result="
            f"{'命中' if result.matched else '未命中'} "
            f"tag_configured={'yes' if result.tag else 'no'} count={result.count} "
            f"orientation={result.orientation} duration={elapsed_ms}ms"
        )
        return result

    async def _resolve_provider(self, event: Any, provider_id: str) -> str:
        resolved = str(provider_id or "").strip()
        if resolved:
            return resolved
        try:
            return str(
                await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
                or ""
            ).strip()
        except Exception as exc:
            self._log("provider_lookup_error", time.monotonic(), exc)
            return ""

    def _log(self, reason: str, started_at: float, exc: Exception | None = None) -> None:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        suffix = f" error_type={type(exc).__name__}" if exc is not None else ""
        logger.warning(
            f"{LOG_PREFIX} 自然语言模型解析失败: reason={reason} "
            f"duration={elapsed_ms}ms{suffix}"
        )


def _extract_json(text: object) -> dict | None:
    """从模型回复里抠出 JSON 对象。

    模型偶尔会包一层 ```json 代码块或加几句解释，这里取第一个 ``{``
    到最后一个 ``}`` 之间的内容再解析，解析失败就当没识别。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _to_natural_query(payload: dict) -> NaturalQuery:
    if payload.get("is_request") is not True:
        return NaturalQuery()

    tag = str(payload.get("tag") or "").strip()[:MAX_AI_TAG_LENGTH]
    illust_id = _clean_illust_id(payload.get("illust_id"))
    count = _clean_count(payload.get("count"))
    orientation = normalize_orientation(payload.get("orientation"))
    if not tag and not illust_id:
        # 模型说是在要图，却既没给标签也没给 ID，等同于随机图请求。
        return NaturalQuery(
            matched=True,
            count=count,
            orientation=orientation,
            source="ai",
        )
    return NaturalQuery(
        matched=True,
        tag=tag,
        count=count,
        orientation=orientation,
        illust_id=illust_id,
        source="ai",
    )


def _clean_count(value: object) -> int:
    try:
        count = int(str(value).strip())
    except (TypeError, ValueError):
        return 1
    return max(1, min(count, MAX_AI_COUNT))


def _clean_illust_id(value: object) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) >= 6 else ""
