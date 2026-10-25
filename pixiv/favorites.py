"""发图收藏：把用户喜欢的作品存到本地 SQLite。

与 ``ImageIndexStore`` 的去重表刻意分开：去重表按保留天数清理，
收藏必须长期有效，所以单独一个数据库文件，避免清理逻辑误伤。

所有异步方法都在 ``asyncio.Lock`` 内通过 ``asyncio.to_thread`` 执行同步
SQLite 操作，与仓库既有的存储层写法保持一致。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .caption import illust_artist, illust_page, illust_pid, illust_source_key, illust_url


LOG_PREFIX = "[GetPx]"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_LIST_LIMIT = 10
MAX_LIST_LIMIT = 50


class FavoriteStore:
    """按用户维度保存收藏作品。"""

    def __init__(self, data_dir: Path | str):
        self._db_path = Path(data_dir) / "pixiv_favorites.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭收藏数据库连接失败: "
                    f"error_type={type(exc).__name__}"
                )
            self._conn = None

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pixiv_favorites (
                user_id TEXT NOT NULL,
                illust_id TEXT NOT NULL,
                pid TEXT NOT NULL DEFAULT '',
                page INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                author TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                scope TEXT NOT NULL DEFAULT '',
                favorited_at TEXT NOT NULL,
                PRIMARY KEY (user_id, illust_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pixiv_favorites_recent
            ON pixiv_favorites (user_id, favorited_at DESC)
            """
        )
        conn.commit()

    # ── 异步对外接口 ──────────────────────────────────────────

    async def add(self, *, user_id: str, illust: dict, scope: str = "") -> bool:
        """新增收藏；已收藏时返回 ``False``（幂等，不覆盖原收藏时间）。"""
        user_id = str(user_id or "").strip()
        illust_id = str(illust.get("id") or "").strip()
        if not user_id or not illust_id:
            return False
        row = (
            user_id,
            illust_id,
            illust_pid(illust),
            illust_page(illust),
            str(illust.get("title") or "").strip(),
            illust_artist(illust),
            illust_source_key(illust),
            illust_url(illust),
            str(scope or ""),
            self.now_iso(),
        )
        async with self._lock:
            return await asyncio.to_thread(self._add_sync, row)

    async def remove(self, *, user_id: str, illust_id: str) -> bool:
        user_id = str(user_id or "").strip()
        illust_id = str(illust_id or "").strip()
        if not user_id or not illust_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._remove_sync, user_id, illust_id)

    async def list_favorites(
        self, user_id: str, *, limit: int = DEFAULT_LIST_LIMIT
    ) -> list[dict[str, Any]]:
        user_id = str(user_id or "").strip()
        if not user_id:
            return []
        try:
            resolved_limit = int(limit)
        except (TypeError, ValueError):
            resolved_limit = DEFAULT_LIST_LIMIT
        resolved_limit = max(1, min(resolved_limit, MAX_LIST_LIMIT))
        async with self._lock:
            return await asyncio.to_thread(
                self._list_sync, user_id, resolved_limit
            )

    async def count(self, user_id: str) -> int:
        user_id = str(user_id or "").strip()
        if not user_id:
            return 0
        async with self._lock:
            return await asyncio.to_thread(self._count_sync, user_id)

    async def has(self, *, user_id: str, illust_id: str) -> bool:
        user_id = str(user_id or "").strip()
        illust_id = str(illust_id or "").strip()
        if not user_id or not illust_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._has_sync, user_id, illust_id)

    # ── 同步实现 ──────────────────────────────────────────────

    def _add_sync(self, row: tuple) -> bool:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO pixiv_favorites (
                    user_id, illust_id, pid, page, title, author,
                    source, url, scope, favorited_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return cursor.rowcount > 0

    def _remove_sync(self, user_id: str, illust_id: str) -> bool:
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "DELETE FROM pixiv_favorites WHERE user_id = ? AND illust_id = ?",
                (user_id, illust_id),
            )
            removed = cursor.rowcount > 0
            if not removed:
                # Lolicon 作品的 illust_id 形如 "pid:page"，用户只会给出 pid。
                # 按 pid 再删一次，避免「/p 取消收藏 #12345678」永远删不掉。
                cursor = conn.execute(
                    "DELETE FROM pixiv_favorites WHERE user_id = ? AND pid = ?",
                    (user_id, illust_id.split(":")[0]),
                )
                removed = cursor.rowcount > 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return removed

    def _list_sync(self, user_id: str, limit: int) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT illust_id, pid, page, title, author, source, url, favorited_at
            FROM pixiv_favorites
            WHERE user_id = ?
            ORDER BY favorited_at DESC, illust_id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def _count_sync(self, user_id: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM pixiv_favorites WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _has_sync(self, user_id: str, illust_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM pixiv_favorites WHERE user_id = ? AND illust_id = ?",
            (user_id, illust_id),
        ).fetchone()
        return row is not None
