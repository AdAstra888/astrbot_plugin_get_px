"""候选超采与补拉（``SearchMixin._collect_candidates``）的测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.search import SearchMixin


def _illust(ident, width=1200, height=1600):
    return {
        "id": str(ident),
        "title": f"t{ident}",
        "width": width,
        "height": height,
        "x_restrict": 0,
        "tags": [],
    }


class _CandidateHarness(SearchMixin):
    """按轮次返回预置候选，记录每轮传入的 offset / aspect_ratio。"""

    def __init__(self, rounds):
        self.config = {}
        self.image_index = None
        self._rounds = list(rounds)
        self.calls = []

    def _cfg_int(self, key, default, lo, hi):
        return int(self.config.get(key, default))

    def _cfg_float(self, key, default, lo, hi):
        return float(self.config.get(key, default))

    def _cfg_str(self, key, default=""):
        return str(self.config.get(key, default))

    def _cfg_bool(self, key, default):
        return bool(self.config.get(key, default))

    def _filter_manga(self, illusts):
        return illusts

    async def _filter_blacklisted_illusts(self, illusts, policy=None):
        return illusts

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
        self.calls.append(
            {
                "count": count,
                "offset": offset,
                "aspect_ratio": aspect_ratio,
                "use_page_cursor": use_page_cursor,
            }
        )
        batch = self._rounds.pop(0) if self._rounds else []
        return batch, len(batch), "lolicon:test"


async def _collect(harness, *, wanted, orientation="any"):
    return await harness._collect_candidates(
        None,
        "tag",
        wanted=wanted,
        orientation=orientation,
        policy=None,
        filter_manga=True,
    )


@pytest.mark.asyncio
async def test_candidates_are_overfetched_then_refilled_until_enough() -> None:
    harness = _CandidateHarness(
        [
            [_illust(1), _illust(2)],
            [_illust(3), _illust(4), _illust(5), _illust(6)],
        ]
    )
    collected, source_key, raw_count = await _collect(harness, wanted=5)

    assert [item["id"] for item in collected] == ["1", "2", "3", "4", "5", "6"]
    assert source_key == "lolicon:test"
    assert raw_count == 6
    # 首轮超采到 wanted × 4（上限 20），次轮按累计原始条数翻页
    assert harness.calls[0]["count"] == 20
    assert harness.calls[0]["offset"] == 0
    assert harness.calls[0]["use_page_cursor"] is True
    assert harness.calls[1]["offset"] == 2
    assert harness.calls[1]["use_page_cursor"] is False


@pytest.mark.asyncio
async def test_refill_stops_as_soon_as_enough_candidates_arrive() -> None:
    harness = _CandidateHarness([[_illust(1), _illust(2), _illust(3)]])
    collected, _source_key, _raw = await _collect(harness, wanted=2)

    assert [item["id"] for item in collected] == ["1", "2", "3"]
    assert len(harness.calls) == 1


@pytest.mark.asyncio
async def test_refill_dedupes_repeated_ids_across_rounds() -> None:
    harness = _CandidateHarness(
        [[_illust(1), _illust(2)], [_illust(2), _illust(3)]]
    )
    collected, _source_key, _raw = await _collect(harness, wanted=3)

    assert [item["id"] for item in collected] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_refill_gives_up_after_max_rounds() -> None:
    harness = _CandidateHarness([[_illust(1)], [_illust(2)], [_illust(3)], [_illust(4)]])
    collected, _source_key, _raw = await _collect(harness, wanted=10)

    # MAX_REFILL_ROUNDS 为 3，第四轮不应被请求
    assert len(harness.calls) == 3
    assert [item["id"] for item in collected] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_refill_passes_lolicon_aspect_ratio_and_filters_locally() -> None:
    harness = _CandidateHarness(
        [
            [
                _illust(1, width=1920, height=1080),  # 横
                _illust(2, width=1080, height=1920),  # 竖
            ]
        ]
    )
    collected, _source_key, _raw = await _collect(harness, wanted=1, orientation="landscape")

    assert harness.calls[0]["aspect_ratio"] == "gt1.05lt4.00"
    assert [item["id"] for item in collected] == ["1"]


@pytest.mark.asyncio
async def test_refill_returns_empty_when_source_has_nothing() -> None:
    harness = _CandidateHarness([[]])
    collected, _source_key, raw_count = await _collect(harness, wanted=3)

    assert collected == []
    assert raw_count == 0
    assert len(harness.calls) == 1


@pytest.mark.asyncio
async def test_any_orientation_does_not_send_aspect_ratio() -> None:
    harness = _CandidateHarness([[_illust(1)]])
    await _collect(harness, wanted=1, orientation="any")

    assert harness.calls[0]["aspect_ratio"] == ""
