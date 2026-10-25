"""``SearchMixin._handle_search`` 的新增行为：按 ID 发图与张数不足提示。"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.search import SearchMixin


class _Policy:
    pass


def _illust(ident, width=1200, height=1600):
    return {
        "id": str(ident),
        "title": f"t{ident}",
        "width": width,
        "height": height,
        "x_restrict": 0,
        "tags": [],
    }


class _FlowHarness(SearchMixin):
    def __init__(self, *, illusts=None, detail=None):
        self.config = {"p_coin_cost": 0, "checkin_enabled": False}
        self.illusts = illusts or []
        self.detail = detail
        self.image_index = None
        self.checkin_store = None
        self.downloader = AsyncMock()
        self.downloader.download_for_send = AsyncMock(
            return_value=("p.jpg", "medium", 1024)
        )
        self.candidate_calls = []
        self.detail_calls = []
        self.pick_calls = []

    def _cfg_int(self, key, default, lo, hi):
        return int(self.config.get(key, default))

    def _cfg_float(self, key, default, lo, hi):
        return float(self.config.get(key, default))

    def _cfg_str(self, key, default=""):
        return str(self.config.get(key, default))

    def _cfg_bool(self, key, default):
        return bool(self.config.get(key, default))

    def _check_rate_limit(self, user_id):
        return 0

    def _forward_threshold(self):
        return 0

    async def _content_safety_policy(self, event):
        return _Policy()

    async def _blocked_query_term(self, query, policy=None):
        return ""

    def _filter_manga(self, illusts):
        return illusts

    async def _filter_blacklisted_illusts(self, illusts, policy=None):
        return illusts

    async def _pick_illusts(
        self, event, illusts, pick_count, *, source_key, dedupe_enabled, raw_count
    ):
        self.pick_calls.append(
            {
                "pick_count": pick_count,
                "source_key": source_key,
                "dedupe_enabled": dedupe_enabled,
            }
        )
        return illusts[:pick_count]

    async def _fetch_source_candidates(
        self,
        event,
        tag,
        *,
        count=20,
        offset=0,
        aspect_ratio="",
        use_page_cursor=True,
        policy=None,
    ):
        self.candidate_calls.append(
            {"count": count, "offset": offset, "aspect_ratio": aspect_ratio}
        )
        return self.illusts, len(self.illusts), "lolicon:random"

    async def _fetch_illust_by_id(self, event, illust_id):
        self.detail_calls.append(str(illust_id))
        return self.detail


class _Event:
    def __init__(self, platform="qq_official"):
        self.platform = platform
        self.sent = []

    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return "group:1"

    def get_platform_name(self):
        return self.platform

    def get_self_id(self):
        return "self-bot"

    def plain_result(self, text):
        return f"plain:{text}"

    def chain_result(self, chain):
        return list(chain)

    async def send(self, chain):
        self.sent.append(chain)


def _texts(event):
    texts = []
    for sent in event.sent:
        if isinstance(sent, str):
            texts.append(sent)
        else:
            texts.extend(str(item) for item in sent)
    return texts


async def _run(harness, event, count_str, *, orientation="", illust_id=""):
    return [
        item
        async for item in harness._handle_search(
            event, "", count_str, orientation=orientation, illust_id=illust_id
        )
    ]


@pytest.mark.asyncio
async def test_by_id_search_bypasses_candidate_collection_and_dedupe() -> None:
    harness = _FlowHarness(detail=_illust(12345678))
    event = _Event()
    await _run(harness, event, "", illust_id="12345678")

    assert harness.detail_calls == ["12345678"]
    assert harness.candidate_calls == []
    assert harness.pick_calls == [
        {"pick_count": 1, "source_key": "pixiv:detail", "dedupe_enabled": False}
    ]


@pytest.mark.asyncio
async def test_by_id_search_reports_missing_work() -> None:
    harness = _FlowHarness(detail=None)
    outputs = await _run(harness, _Event(), "", illust_id="999999")

    assert len(outputs) == 1
    assert "没有找到作品 999999" in outputs[0]
    assert "pixiv_refresh_token" in outputs[0]
    assert harness.downloader.download_for_send.await_count == 0


@pytest.mark.asyncio
async def test_shortage_is_reported_with_actual_count() -> None:
    """候选不足时不能静默少发，必须明确告诉用户实际发送张数。"""
    harness = _FlowHarness(illusts=[_illust(1)])
    event = _Event()
    await _run(harness, event, "5")

    texts = _texts(event)
    assert any("请求 5 张，实际发送 1 张" in text for text in texts)


@pytest.mark.asyncio
async def test_no_shortage_notice_when_request_satisfied() -> None:
    harness = _FlowHarness(illusts=[_illust(1), _illust(2)])
    event = _Event()
    await _run(harness, event, "2")

    assert not any("实际发送" in text for text in _texts(event))


@pytest.mark.asyncio
async def test_orientation_is_forwarded_to_candidate_collection() -> None:
    harness = _FlowHarness(illusts=[_illust(1, 1920, 1080)])
    await _run(harness, _Event(), "1", orientation="landscape")

    assert harness.candidate_calls[0]["aspect_ratio"] == "gt1.05lt4.00"


@pytest.mark.asyncio
async def test_default_orientation_comes_from_config() -> None:
    harness = _FlowHarness(illusts=[_illust(1, 1080, 1920)])
    harness.config["image_orientation"] = "portrait"
    await _run(harness, _Event(), "1")

    assert harness.candidate_calls[0]["aspect_ratio"] == "gt0.25lt0.95"


@pytest.mark.asyncio
async def test_orientation_mismatch_explains_the_filter() -> None:
    """方向筛选后无候选时，提示要提到方向，而不是笼统的「无结果」。"""
    harness = _FlowHarness(illusts=[_illust(1, 1080, 1920)])
    outputs = await _run(harness, _Event(), "1", orientation="landscape")

    assert len(outputs) == 1
    assert "横图" in outputs[0]
