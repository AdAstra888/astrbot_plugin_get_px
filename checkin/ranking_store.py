from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date, timedelta
import re


_MONTH_PATTERN = re.compile(r"\d{4}-\d{2}")
RANKING_TYPES = {"today", "month", "streak", "total"}


class RankingStoreMixin:
    async def list_checkin_groups(self) -> list[dict[str, object]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_checkin_groups_sync)

    async def get_group_ranking(
        self,
        *,
        group_id: str,
        ranking_type: str = "today",
        month: str = "",
        limit: int = 10,
    ) -> dict[str, object]:
        group_id = str(group_id or "").strip()
        if not group_id:
            raise ValueError("group_id is required")
        ranking_type = str(ranking_type or "today").strip().lower()
        if ranking_type not in RANKING_TYPES:
            raise ValueError("ranking type must be today, month, streak or total")
        month = str(month or self.today_key()[:7]).strip()
        if not _MONTH_PATTERN.fullmatch(month):
            raise ValueError("month must use YYYY-MM")
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise ValueError("month must use YYYY-MM") from exc
        limit = max(1, min(int(limit), 100))
        async with self._lock:
            return await asyncio.to_thread(
                self._get_group_ranking_sync,
                group_id,
                ranking_type,
                month,
                limit,
            )

    async def get_group_trend(
        self, *, group_id: str, days: int = 7
    ) -> list[dict[str, object]]:
        group_id = str(group_id or "").strip()
        if not group_id:
            raise ValueError("group_id is required")
        days = 30 if int(days) == 30 else 7
        async with self._lock:
            return await asyncio.to_thread(self._get_group_trend_sync, group_id, days)

    async def get_checkin_overview(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._get_checkin_overview_sync)

    def _list_checkin_groups_sync(self) -> list[dict[str, object]]:
        today = self.today_key()
        month = today[:7]
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT group_id,
                       COALESCE(NULLIF(MAX(group_name), ''), group_id) AS group_name,
                       MAX(platform) AS platform,
                       MAX(last_seen_at) AS last_seen_at,
                       COUNT(DISTINCT CASE WHEN date_key = ? THEN user_id END) AS today_count,
                       COUNT(DISTINCT CASE WHEN substr(date_key, 1, 7) = ?
                                      THEN date_key || ':' || user_id END) AS month_count
                FROM checkin_group_presence
                GROUP BY group_id
                ORDER BY last_seen_at DESC, group_id
                """,
                (today, month),
            ).fetchall()
        return [dict(row) for row in rows]

    def _get_group_ranking_sync(
        self, group_id: str, ranking_type: str, month: str, limit: int
    ) -> dict[str, object]:
        today = self.today_key()
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        with closing(self._connect()) as conn:
            if ranking_type == "today":
                entries = self._ranking_today(conn, group_id, today)
            elif ranking_type == "month":
                entries = self._ranking_month(conn, group_id, month)
            elif ranking_type == "streak":
                entries = self._ranking_streak(conn, group_id, today, yesterday)
            else:
                entries = self._ranking_total(conn, group_id)

        if ranking_type == "today":
            entries.sort(key=lambda item: (item["value"], item["user_id"]))
        else:
            entries.sort(
                key=lambda item: (
                    -int(item["value"]),
                    item["first_seen_at"],
                    item["user_id"],
                )
            )
        for rank, entry in enumerate(entries, 1):
            entry["rank"] = rank
        return {
            "group_id": group_id,
            "type": ranking_type,
            "month": month,
            "total": len(entries),
            "entries": entries[:limit],
            "all_entries": entries,
        }

    # 排行榜聚合全部下沉到 SQL：Python 侧只接收「每个用户一行」的结果，
    # 避免把整群的历史签到行（每人每天一行）读进内存再聚合。
    _RANKING_LATEST_USERNAME = """
        latest AS (
            SELECT user_id, username FROM (
                SELECT user_id, username,
                       ROW_NUMBER() OVER (
                           PARTITION BY user_id
                           ORDER BY last_seen_at DESC, date_key ASC
                       ) AS row_no
                FROM scoped
            ) WHERE row_no = 1
        )
    """

    def _ranking_today(
        self, conn, group_id: str, today: str
    ) -> list[dict[str, object]]:
        rows = conn.execute(
            f"""
            WITH scoped AS (
                SELECT user_id, username, date_key, first_seen_at, last_seen_at
                FROM checkin_group_presence
                WHERE group_id = ?
            ),
            {self._RANKING_LATEST_USERNAME}
            SELECT s.user_id,
                   COALESCE(NULLIF(l.username, ''), s.user_id) AS username,
                   s.first_seen_at
            FROM scoped AS s
            LEFT JOIN latest AS l ON l.user_id = s.user_id
            WHERE s.date_key = ?
            """,
            (group_id, today),
        ).fetchall()
        return [
            {
                "user_id": str(row["user_id"]),
                "username": str(row["username"] or row["user_id"]),
                "value": str(row["first_seen_at"]),
                "first_seen_at": str(row["first_seen_at"]),
            }
            for row in rows
        ]

    def _ranking_month(
        self, conn, group_id: str, month: str
    ) -> list[dict[str, object]]:
        start, end = _month_range(month)
        return self._ranking_span(conn, group_id, start, end)

    def _ranking_total(self, conn, group_id: str) -> list[dict[str, object]]:
        return self._ranking_span(conn, group_id, "", "")

    def _ranking_span(
        self, conn, group_id: str, start: str, end: str
    ) -> list[dict[str, object]]:
        """按 [start, end) 聚合每人签到天数；start/end 为空表示不限时间。"""

        if start and end:
            scope_sql = "WHERE group_id = ? AND date_key >= ? AND date_key < ?"
            params: tuple[object, ...] = (group_id, start, end)
        else:
            scope_sql = "WHERE group_id = ?"
            params = (group_id,)
        rows = conn.execute(
            f"""
            WITH scoped AS (
                SELECT user_id, username, date_key, first_seen_at, last_seen_at
                FROM checkin_group_presence
                {scope_sql}
            ),
            {self._RANKING_LATEST_USERNAME}
            SELECT a.user_id,
                   COALESCE(NULLIF(l.username, ''), a.user_id) AS username,
                   a.days AS value,
                   a.first_seen_at
            FROM (
                SELECT user_id, COUNT(DISTINCT date_key) AS days,
                       MIN(first_seen_at) AS first_seen_at
                FROM scoped GROUP BY user_id
            ) AS a
            LEFT JOIN latest AS l ON l.user_id = a.user_id
            """,
            params,
        ).fetchall()
        return [
            {
                "user_id": str(row["user_id"]),
                "username": str(row["username"] or row["user_id"]),
                "value": int(row["value"]),
                "first_seen_at": str(row["first_seen_at"]),
            }
            for row in rows
        ]

    def _ranking_streak(
        self, conn, group_id: str, today: str, yesterday: str
    ) -> list[dict[str, object]]:
        """用 SQL 窗口函数识别连续日期段，只返回仍在延续的连签长度。"""

        rows = conn.execute(
            f"""
            WITH scoped AS (
                SELECT user_id, username, date_key, first_seen_at, last_seen_at
                FROM checkin_group_presence
                WHERE group_id = ?
            ),
            days AS (
                SELECT DISTINCT user_id, date_key FROM scoped
                WHERE date_key <= ?
            ),
            runs AS (
                SELECT user_id, date_key,
                       CAST(julianday(date_key) AS INTEGER)
                         - ROW_NUMBER() OVER (
                               PARTITION BY user_id ORDER BY date_key
                           ) AS run_key
                FROM days
            ),
            run_agg AS (
                SELECT user_id, COUNT(*) AS streak, MAX(date_key) AS run_end
                FROM runs GROUP BY user_id, run_key
            ),
            active AS (
                SELECT user_id, MAX(streak) AS streak
                FROM run_agg
                WHERE run_end >= ? AND run_end <= ?
                GROUP BY user_id
            ),
            firsts AS (
                SELECT user_id, MIN(first_seen_at) AS first_seen_at
                FROM scoped GROUP BY user_id
            ),
            {self._RANKING_LATEST_USERNAME}
            SELECT a.user_id,
                   COALESCE(NULLIF(l.username, ''), a.user_id) AS username,
                   a.streak AS value,
                   f.first_seen_at
            FROM active AS a
            LEFT JOIN latest AS l ON l.user_id = a.user_id
            LEFT JOIN firsts AS f ON f.user_id = a.user_id
            """,
            (group_id, today, yesterday, today),
        ).fetchall()
        return [
            {
                "user_id": str(row["user_id"]),
                "username": str(row["username"] or row["user_id"]),
                "value": int(row["value"]),
                "first_seen_at": str(row["first_seen_at"]),
            }
            for row in rows
        ]

    def _get_group_trend_sync(
        self, group_id: str, days: int
    ) -> list[dict[str, object]]:
        end = date.fromisoformat(self.today_key())
        start = end - timedelta(days=days - 1)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT date_key, COUNT(DISTINCT user_id) AS count
                FROM checkin_group_presence
                WHERE group_id = ? AND date_key BETWEEN ? AND ?
                GROUP BY date_key
                """,
                (group_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        counts = {str(row["date_key"]): int(row["count"]) for row in rows}
        return [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "count": counts.get((start + timedelta(days=index)).isoformat(), 0),
            }
            for index in range(days)
        ]

    def _get_checkin_overview_sync(self) -> dict[str, int]:
        today = self.today_key()
        month = today[:7]
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT CASE WHEN date_key = ? THEN user_id END) AS today_checkins,
                       COUNT(DISTINCT CASE WHEN date_key = ? THEN group_id END) AS active_groups,
                       COUNT(DISTINCT CASE WHEN substr(date_key, 1, 7) = ? THEN user_id END) AS month_users
                FROM checkin_group_presence
                """,
                (today, today, month),
            ).fetchone()
        return {
            "today_checkins": int(row["today_checkins"] or 0),
            "active_groups": int(row["active_groups"] or 0),
            "month_users": int(row["month_users"] or 0),
        }


def _month_range(month: str) -> tuple[str, str]:
    """把 YYYY-MM 转成 [当月首日, 次月首日) 的半开区间。"""

    start = date.fromisoformat(f"{month}-01")
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)
    return start.isoformat(), end.isoformat()
