"""自然语言模型兜底（pixiv.natural_ai）的单元测试。"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.natural_ai import (
    MAX_AI_COUNT,
    NaturalQueryResolver,
    _extract_json,
    _to_natural_query,
)
from astrbot_plugin_get_px.pixiv.orientation import (
    ORIENTATION_ANY,
    ORIENTATION_PORTRAIT,
)


class _Context:
    """最小的 AstrBot context 桩：只实现解析器用到的方法。"""

    def __init__(self, reply="", provider="provider-1", error=None):
        self.reply = reply
        self.provider = provider
        self.error = error
        self.llm_calls = []

    async def get_current_chat_provider_id(self, *, umo=""):
        return self.provider

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(completion_text=self.reply)


class _Event:
    unified_msg_origin = "test:umo"


# ── JSON 抽取 ────────────────────────────────────────────────


def test_extract_json_accepts_plain_and_wrapped_payloads() -> None:
    assert _extract_json('{"is_request": true}') == {"is_request": True}
    assert _extract_json('```json\n{"is_request": true}\n```') == {"is_request": True}
    assert _extract_json('好的，结果如下：{"is_request": false} 以上。') == {
        "is_request": False
    }


def test_extract_json_rejects_garbage() -> None:
    assert _extract_json("") is None
    assert _extract_json("没有 JSON") is None
    assert _extract_json("{不是合法 JSON}") is None
    assert _extract_json("[1, 2, 3]") is None


# ── 字段清洗 ────────────────────────────────────────────────


def test_non_request_payload_is_unmatched() -> None:
    assert not _to_natural_query({"is_request": False, "tag": "初音"}).matched
    assert not _to_natural_query({"tag": "初音"}).matched


def test_payload_fields_are_cleaned() -> None:
    query = _to_natural_query(
        {
            "is_request": True,
            "tag": "  初音ミク  ",
            "count": 99,
            "orientation": "竖图",
            "illust_id": "pixiv.net/artworks/12345678",
        }
    )

    assert query.matched
    assert query.tag == "初音ミク"
    assert query.count == MAX_AI_COUNT
    assert query.orientation == ORIENTATION_PORTRAIT
    assert query.illust_id == "12345678"
    assert query.source == "ai"


def test_payload_without_tag_or_id_still_sends_random() -> None:
    query = _to_natural_query({"is_request": True, "count": 3})

    assert query.matched
    assert query.tag == ""
    assert query.illust_id == ""
    assert query.count == 3
    assert query.orientation == ORIENTATION_ANY


def test_short_numeric_id_is_discarded() -> None:
    assert _to_natural_query({"is_request": True, "illust_id": "123"}).illust_id == ""


# ── 解析器流程 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolver_returns_parsed_query() -> None:
    context = _Context(
        reply='{"is_request": true, "tag": "初音ミク", "count": 2, '
        '"orientation": "portrait", "illust_id": ""}'
    )
    resolver = NaturalQueryResolver(context)

    query = await resolver.resolve(_Event(), "帮我找两张竖着的初音图")

    assert query.matched
    assert query.tag == "初音ミク"
    assert query.count == 2
    assert query.orientation == ORIENTATION_PORTRAIT
    assert context.llm_calls[0]["chat_provider_id"] == "provider-1"


@pytest.mark.asyncio
async def test_resolver_prefers_configured_provider() -> None:
    context = _Context(reply='{"is_request": false}')
    resolver = NaturalQueryResolver(context)

    await resolver.resolve(_Event(), "随便聊聊", provider_id="provider-2")

    assert context.llm_calls[0]["chat_provider_id"] == "provider-2"


@pytest.mark.asyncio
async def test_resolver_returns_unmatched_on_provider_error() -> None:
    context = _Context(error=RuntimeError("boom"))
    resolver = NaturalQueryResolver(context)

    query = await resolver.resolve(_Event(), "帮我找两张竖着的初音图")

    assert not query.matched


@pytest.mark.asyncio
async def test_resolver_returns_unmatched_on_invalid_json() -> None:
    context = _Context(reply="我不太确定你的意思")
    resolver = NaturalQueryResolver(context)

    query = await resolver.resolve(_Event(), "帮我找两张竖着的初音图")

    assert not query.matched


@pytest.mark.asyncio
async def test_resolver_returns_unmatched_without_provider() -> None:
    context = _Context(reply='{"is_request": true, "tag": "初音"}', provider="")
    resolver = NaturalQueryResolver(context)

    query = await resolver.resolve(_Event(), "帮我找两张竖着的初音图")

    assert not query.matched
    assert context.llm_calls == []
