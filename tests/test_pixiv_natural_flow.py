"""自然语言触发流程（pixiv.natural_flow）的单元测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.natural import NaturalQuery
from astrbot_plugin_get_px.pixiv.natural_flow import (
    MAX_REJECTED_CACHE,
    NaturalTriggerMixin,
)


class _Event:
    def __init__(self, text: str):
        self._text = text
        self.stopped = False
        self.unified_msg_origin = "test:umo"

    def get_message_str(self) -> str:
        return self._text

    def stop_event(self) -> None:
        self.stopped = True


class _Harness(NaturalTriggerMixin):
    """只实现流程需要的配置读取与两个下游调用。"""

    def __init__(self, config=None, ai_result=None):
        self.config = dict(config or {})
        self.ai_result = ai_result or NaturalQuery()
        self.search_calls = []
        self.ai_calls = []
        self.client_ready = True
        self._ai_rejected_cache = {}
        self._natural_resolver = None
        self.context = object()

    def _cfg_bool(self, key, default):
        return bool(self.config.get(key, default))

    def _cfg_str(self, key, default=""):
        return str(self.config.get(key, default))

    def _cfg_float(self, key, default, low, high):
        return float(self.config.get(key, default))

    def _ensure_client_or_error(self, event):
        return self.client_ready

    async def _handle_search(self, event, tag, count_str, *, orientation="", illust_id=""):
        self.search_calls.append(
            {
                "tag": tag,
                "count_str": count_str,
                "orientation": orientation,
                "illust_id": illust_id,
            }
        )
        yield "sent"

    async def _resolve_natural_query(self, event, message):
        self.ai_calls.append(message)
        return self.ai_result


async def _drain(gen):
    return [item async for item in gen]


ENABLED = {"auto_trigger_enabled": True, "checkin_bot_name": "大肥鱼"}


@pytest.mark.asyncio
async def test_bot_name_prefix_is_dropped_and_random_image_sent() -> None:
    harness = _Harness(ENABLED)
    event = _Event("大肥鱼来张图")

    results = await _drain(harness._handle_auto_trigger(event))

    assert results == ["sent"]
    assert event.stopped is True
    assert harness.search_calls == [
        # 句子没提方向时传空串，由 _handle_search 回落到配置的 image_orientation。
        {"tag": "", "count_str": "1", "orientation": "", "illust_id": ""}
    ]
    # 规则能认出时绝不调用模型。
    assert harness.ai_calls == []


@pytest.mark.asyncio
async def test_non_alias_prefix_is_kept_as_tag() -> None:
    harness = _Harness(ENABLED)
    event = _Event("初音来三张图 竖图")

    await _drain(harness._handle_auto_trigger(event))

    assert harness.search_calls == [
        {"tag": "初音", "count_str": "3", "orientation": "portrait", "illust_id": ""}
    ]


@pytest.mark.asyncio
async def test_extra_aliases_from_config_are_honored() -> None:
    harness = _Harness(
        {
            "auto_trigger_enabled": True,
            "checkin_bot_name": "",
            "auto_trigger_aliases": "肥鱼, 小鱼",
        }
    )
    event = _Event("肥鱼来张图")

    await _drain(harness._handle_auto_trigger(event))

    assert harness.search_calls[0]["tag"] == ""


@pytest.mark.asyncio
async def test_rule_miss_falls_back_to_model() -> None:
    harness = _Harness(
        ENABLED,
        ai_result=NaturalQuery(matched=True, tag="蓝色系", count=2, source="ai"),
    )
    event = _Event("能不能帮我找点蓝色系的图")

    results = await _drain(harness._handle_auto_trigger(event))

    assert results == ["sent"]
    assert harness.ai_calls == ["能不能帮我找点蓝色系的图"]
    assert harness.search_calls == [
        {"tag": "蓝色系", "count_str": "2", "orientation": "", "illust_id": ""}
    ]
    assert event.stopped is True


@pytest.mark.asyncio
async def test_model_rejection_does_not_stop_event() -> None:
    harness = _Harness(ENABLED, ai_result=NaturalQuery())
    event = _Event("这个配色我挺喜欢的")

    results = await _drain(harness._handle_auto_trigger(event))

    assert results == []
    assert event.stopped is False
    assert harness.search_calls == []


@pytest.mark.asyncio
async def test_rejected_message_is_only_sent_to_model_once() -> None:
    harness = _Harness(ENABLED, ai_result=NaturalQuery())

    await _drain(harness._handle_auto_trigger(_Event("这个配色我挺喜欢的")))
    await _drain(harness._handle_auto_trigger(_Event("这个配色我挺喜欢的")))

    assert len(harness.ai_calls) == 1


@pytest.mark.asyncio
async def test_rejected_cache_is_bounded() -> None:
    harness = _Harness(ENABLED, ai_result=NaturalQuery())
    harness._ai_rejected_cache = {f"msg{index}": True for index in range(MAX_REJECTED_CACHE)}

    harness._remember_ai_rejected("新句子")

    assert harness._ai_rejected_cache == {"新句子": True}


@pytest.mark.asyncio
async def test_fallback_disabled_skips_model() -> None:
    harness = _Harness(
        {"auto_trigger_enabled": True, "auto_trigger_ai_fallback": False},
        ai_result=NaturalQuery(matched=True, tag="x"),
    )
    event = _Event("能不能帮我找点蓝色系的图")

    results = await _drain(harness._handle_auto_trigger(event))

    assert results == []
    assert harness.ai_calls == []


@pytest.mark.asyncio
async def test_switch_off_does_nothing() -> None:
    harness = _Harness({"auto_trigger_enabled": False})
    event = _Event("来张图")

    assert await _drain(harness._handle_auto_trigger(event)) == []
    assert event.stopped is False
    assert harness.search_calls == []


@pytest.mark.asyncio
async def test_past_tense_and_questions_skip_model() -> None:
    harness = _Harness(ENABLED, ai_result=NaturalQuery(matched=True, tag="x"))

    assert await _drain(harness._handle_auto_trigger(_Event("我昨天发了一张图"))) == []
    assert await _drain(harness._handle_auto_trigger(_Event("这张图的画师是谁？"))) == []

    assert harness.ai_calls == []
    assert harness.search_calls == []


@pytest.mark.asyncio
async def test_overlong_and_slash_messages_are_ignored() -> None:
    harness = _Harness(ENABLED)

    long_text = "来张图" + "很长" * 30
    assert await _drain(harness._handle_auto_trigger(_Event(long_text))) == []
    assert await _drain(harness._handle_auto_trigger(_Event("/p 初音"))) == []
    assert harness.search_calls == []


@pytest.mark.asyncio
async def test_missing_client_aborts_without_stopping_event() -> None:
    harness = _Harness(ENABLED)
    harness.client_ready = False
    event = _Event("来张图")

    results = await _drain(harness._handle_auto_trigger(event))

    assert results == []
    assert event.stopped is False
    assert harness.search_calls == []
