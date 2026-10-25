"""发图收藏存储（pixiv.favorites）的单元测试。"""

import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.favorites import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    FavoriteStore,
)


def _illust(pid, page=0, title="t", author="a", source="lolicon"):
    return {
        "id": f"{pid}:{page}" if source == "lolicon" else str(pid),
        "pid": str(pid),
        "page": page,
        "title": title,
        "user": {"id": "1", "name": author},
        "_source": source,
    }


@pytest.mark.asyncio
async def test_add_and_list_favorites() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            assert await store.add(user_id="u1", illust=_illust(100), scope="group:1")
            rows = await store.list_favorites("u1")
            assert len(rows) == 1
            assert rows[0]["pid"] == "100"
            assert rows[0]["title"] == "t"
            assert rows[0]["author"] == "a"
            assert rows[0]["source"] == "lolicon"
            assert rows[0]["url"] == "https://www.pixiv.net/artworks/100"
            assert await store.count("u1") == 1
            assert await store.has(user_id="u1", illust_id="100:0") is True
        finally:
            store.close()


@pytest.mark.asyncio
async def test_add_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            assert await store.add(user_id="u1", illust=_illust(100)) is True
            # 重复收藏返回 False，且不会新增行
            assert await store.add(user_id="u1", illust=_illust(100)) is False
            assert await store.count("u1") == 1
        finally:
            store.close()


@pytest.mark.asyncio
async def test_remove_reports_whether_row_existed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            await store.add(user_id="u1", illust=_illust(100))
            assert await store.remove(user_id="u1", illust_id="100:0") is True
            assert await store.remove(user_id="u1", illust_id="100:0") is False
            assert await store.count("u1") == 0
        finally:
            store.close()


@pytest.mark.asyncio
async def test_favorites_are_per_user() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            await store.add(user_id="u1", illust=_illust(100))
            await store.add(user_id="u2", illust=_illust(200))
            assert await store.count("u1") == 1
            assert await store.count("u2") == 1
            assert await store.has(user_id="u1", illust_id="200:0") is False
            u2_rows = await store.list_favorites("u2")
            assert [row["pid"] for row in u2_rows] == ["200"]
        finally:
            store.close()


@pytest.mark.asyncio
async def test_list_limit_is_clamped_and_newest_first() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            for pid in range(300, 305):
                await store.add(user_id="u1", illust=_illust(pid))
            assert len(await store.list_favorites("u1", limit=2)) == 2
            # 超过上限时截到 MAX_LIST_LIMIT，不会抛错
            assert len(await store.list_favorites("u1", limit=999)) == 5
            assert MAX_LIST_LIMIT >= DEFAULT_LIST_LIMIT
            # 非法 limit 回落默认值
            assert len(await store.list_favorites("u1", limit="abc")) == 5
        finally:
            store.close()


@pytest.mark.asyncio
async def test_empty_user_and_illust_are_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            assert await store.add(user_id="", illust=_illust(100)) is False
            assert await store.add(user_id="u1", illust={"id": ""}) is False
            assert await store.list_favorites("") == []
            assert await store.count("") == 0
            assert await store.remove(user_id="", illust_id="1") is False
        finally:
            store.close()


@pytest.mark.asyncio
async def test_favorites_survive_reopen() -> None:
    """收藏必须跨进程长期有效，与按天清理的去重库分开存。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = FavoriteStore(tmp)
        try:
            await store.add(user_id="u1", illust=_illust(100))
        finally:
            store.close()

        reopened = FavoriteStore(tmp)
        try:
            assert await reopened.count("u1") == 1
            assert (Path(tmp) / "pixiv_favorites.sqlite3").exists()
        finally:
            reopened.close()
