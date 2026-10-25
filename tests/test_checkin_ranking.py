from __future__ import annotations

import tempfile

import pytest

from checkin import CheckinStore


class FrozenCheckinStore(CheckinStore):
    def __init__(self, data_dir: str, *, date_key: str):
        self.date_key = date_key
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"


@pytest.mark.asyncio
async def test_group_presence_is_separate_from_global_daily_reward() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        first = await store.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
            group_id="20001",
            group_name="Group A",
            platform="aiocqhttp",
        )
        second = await store.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
            group_id="20002",
            group_name="Group B",
            platform="aiocqhttp",
        )

        assert not first.duplicate
        assert second.duplicate
        assert (await store.get_profile("10001")).total_days == 1
        groups = await store.list_checkin_groups()
        assert {item["group_id"] for item in groups} == {"20001", "20002"}
        assert all(item["today_count"] == 1 for item in groups)


@pytest.mark.asyncio
async def test_group_rankings_and_trend_use_only_current_group() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-12")
        await store.checkin(
            user_id="10001", username="Alice", bot_name="neko", group_id="20001"
        )
        await store.checkin(
            user_id="10002", username="Bob", bot_name="neko", group_id="20002"
        )
        store.date_key = "2026-07-13"
        await store.checkin(
            user_id="10001", username="Alice", bot_name="neko", group_id="20001"
        )
        await store.checkin(
            user_id="10002", username="Bob", bot_name="neko", group_id="20001"
        )

        month = await store.get_group_ranking(
            group_id="20001", ranking_type="month", limit=10
        )
        streak = await store.get_group_ranking(
            group_id="20001", ranking_type="streak", limit=10
        )
        trend = await store.get_group_trend(group_id="20001", days=7)

        assert [(item["user_id"], item["value"]) for item in month["entries"]] == [
            ("10001", 2),
            ("10002", 1),
        ]
        assert streak["entries"][0]["user_id"] == "10001"
        assert streak["entries"][0]["value"] == 2
        assert trend[-2:] == [
            {"date": "2026-07-12", "count": 1},
            {"date": "2026-07-13", "count": 2},
        ]


@pytest.mark.asyncio
async def test_ranking_aggregates_in_sql_with_month_and_streak_rules() -> None:
    """月榜只统计目标月，连签只在今天或昨天有记录时延续。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-01")
        # Alice：7/01-7/03 连续签到，7/05 断签后重新开始
        for day in ("2026-07-01", "2026-07-02", "2026-07-03", "2026-07-05"):
            store.date_key = day
            await store.checkin(
                user_id="10001", username="Alice", bot_name="neko", group_id="20001"
            )
        # Bob：只在 6 月签到，7 月无记录
        store.date_key = "2026-06-30"
        await store.checkin(
            user_id="10002", username="Bob", bot_name="neko", group_id="20001"
        )
        store.date_key = "2026-07-05"

        month = await store.get_group_ranking(
            group_id="20001", ranking_type="month", month="2026-07", limit=10
        )
        june = await store.get_group_ranking(
            group_id="20001", ranking_type="month", month="2026-06", limit=10
        )
        streak = await store.get_group_ranking(
            group_id="20001", ranking_type="streak", limit=10
        )
        total = await store.get_group_ranking(
            group_id="20001", ranking_type="total", limit=10
        )

        assert [(item["user_id"], item["value"]) for item in month["entries"]] == [
            ("10001", 4)
        ]
        assert [(item["user_id"], item["value"]) for item in june["entries"]] == [
            ("10002", 1)
        ]
        # 7/05 是今天，所以 Alice 的连签是 1 而不是 3
        assert [(item["user_id"], item["value"]) for item in streak["entries"]] == [
            ("10001", 1)
        ]
        assert {item["user_id"]: item["value"] for item in total["entries"]} == {
            "10001": 4,
            "10002": 1,
        }


@pytest.mark.asyncio
async def test_streak_continues_from_yesterday_when_today_unsigned() -> None:
    """今天还没签到时，连签应从昨天往前数，而不是直接归零。"""

    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-10")
        for day in ("2026-07-10", "2026-07-11", "2026-07-12"):
            store.date_key = day
            await store.checkin(
                user_id="10001", username="Alice", bot_name="neko", group_id="20001"
            )
        store.date_key = "2026-07-13"

        streak = await store.get_group_ranking(
            group_id="20001", ranking_type="streak", limit=10
        )

        assert [(item["user_id"], item["value"]) for item in streak["entries"]] == [
            ("10001", 3)
        ]


@pytest.mark.asyncio
async def test_streak_is_empty_after_two_idle_days() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-10")
        await store.checkin(
            user_id="10001", username="Alice", bot_name="neko", group_id="20001"
        )
        store.date_key = "2026-07-13"

        streak = await store.get_group_ranking(
            group_id="20001", ranking_type="streak", limit=10
        )

        assert streak["entries"] == []
        assert streak["total"] == 0


@pytest.mark.asyncio
async def test_snapshot_round_trip_preserves_group_presence() -> None:
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        source = FrozenCheckinStore(src, date_key="2026-07-13")
        await source.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
            group_id="20001",
            group_name="Group A",
            platform="aiocqhttp",
        )
        snapshot = await source.export_snapshot()

        assert snapshot["schema_version"] == 7
        assert len(snapshot["group_presence"]) == 1

        target = FrozenCheckinStore(dst, date_key="2026-07-13")
        result = await target.import_snapshot(snapshot)
        groups = await target.list_checkin_groups()

        assert result["group_presence"] == 1
        assert groups[0]["group_name"] == "Group A"
