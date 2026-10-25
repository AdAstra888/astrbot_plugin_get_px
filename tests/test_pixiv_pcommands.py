"""``/p`` 指令分发与收藏动作（pixiv.pcommands）的测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.pcommands import HELP_TEXT, PCommandMixin


class _Store:
    """按 ``FavoriteStore.list_favorites`` 的行形状保存，避免测试桩掩盖字段差异。"""

    def __init__(self):
        self.rows = {}

    async def add(self, *, user_id, illust, scope=""):
        key = (user_id, str(illust.get("id") or ""))
        if key in self.rows:
            return False
        pid = str(illust.get("pid") or str(illust.get("id") or "").split(":")[0])
        self.rows[key] = {
            "illust_id": key[1],
            "pid": pid,
            "page": int(illust.get("page") or 0),
            "title": str(illust.get("title") or ""),
            "author": str((illust.get("user") or {}).get("name") or ""),
            "source": str(illust.get("_source") or ""),
            "url": f"https://www.pixiv.net/artworks/{pid}",
            "scope": scope,
        }
        return True

    async def remove(self, *, user_id, illust_id):
        key = (user_id, illust_id)
        if key in self.rows:
            del self.rows[key]
            return True
        # 与 FavoriteStore._remove_sync 一致：Lolicon 存的是 "pid:page"，
        # 用户只会给纯 pid，所以按 pid 再删一次。
        pid = str(illust_id).split(":")[0]
        for row_key, row in list(self.rows.items()):
            if row_key[0] == user_id and row["pid"] == pid:
                del self.rows[row_key]
                return True
        return False

    async def list_favorites(self, user_id, *, limit=10):
        rows = [row for (uid, _), row in self.rows.items() if uid == user_id]
        return rows[:limit]

    async def count(self, user_id):
        return sum(1 for (uid, _) in self.rows if uid == user_id)


class _Event:
    def __init__(self, sender="10001"):
        self._sender = sender
        self.sent = []

    def get_sender_id(self):
        return self._sender

    def get_group_id(self):
        return "group:1"

    def plain_result(self, text):
        return f"plain:{text}"

    async def send(self, chain):
        self.sent.append(chain)


class _Harness(PCommandMixin):
    def __init__(self, *, favorites_enabled=True, store=None, detail=None):
        self.config = {"favorites_enabled": favorites_enabled}
        self.favorite_store = store
        self._detail = detail
        self._last_sent_illust = {}
        self.search_calls = []

    def _cfg_bool(self, key, default):
        return bool(self.config.get(key, default))

    def _event_scope(self, event):
        return f"group:{event.get_group_id()}"

    def _ensure_client_or_error(self, event):
        return True

    async def _fetch_illust_by_id(self, event, illust_id):
        return self._detail

    async def _handle_search(self, event, tag, count_str, *, orientation="", illust_id=""):
        self.search_calls.append((tag, count_str, orientation, illust_id))
        yield "search-result"


async def _collect(agen):
    return [item async for item in agen]


def _illust(ident="12345678:0"):
    return {
        "id": ident,
        "pid": ident.split(":")[0],
        "page": 0,
        "title": "夜の街",
        "user": {"id": "1", "name": "画师A"},
        "_source": "lolicon",
    }


@pytest.mark.asyncio
async def test_help_action_does_not_touch_search_flow() -> None:
    harness = _Harness()
    event = _Event()
    outputs = await _collect(harness._handle_p_query(event, "帮助"))
    assert outputs == [f"plain:{HELP_TEXT}"]
    assert harness.search_calls == []


@pytest.mark.asyncio
async def test_search_action_forwards_orientation_and_illust_id() -> None:
    harness = _Harness()
    event = _Event()

    await _collect(harness._handle_p_query(event, "初音ミク 3 横图"))
    assert harness.search_calls[-1] == ("初音ミク", "3", "landscape", "")

    await _collect(harness._handle_p_query(event, "#12345678"))
    assert harness.search_calls[-1] == ("", "", "", "12345678")


@pytest.mark.asyncio
async def test_favorite_add_without_id_uses_last_sent_illust() -> None:
    store = _Store()
    harness = _Harness(store=store)
    event = _Event()
    harness._last_sent_illust["10001"] = _illust()

    outputs = await _collect(harness._handle_p_query(event, "收藏"))
    assert len(outputs) == 1
    assert "已收藏「夜の街」" in outputs[0]
    assert "当前共 1 条收藏" in outputs[0]
    assert await store.count("10001") == 1


@pytest.mark.asyncio
async def test_favorite_add_is_idempotent_and_keeps_message_clear() -> None:
    store = _Store()
    harness = _Harness(store=store)
    event = _Event()
    harness._last_sent_illust["10001"] = _illust()

    await _collect(harness._handle_p_query(event, "收藏"))
    outputs = await _collect(harness._handle_p_query(event, "收藏"))
    assert "已经在收藏里了" in outputs[0]
    assert await store.count("10001") == 1


@pytest.mark.asyncio
async def test_favorite_add_reports_missing_last_sent_illust() -> None:
    harness = _Harness(store=_Store())
    outputs = await _collect(harness._handle_p_query(_Event(), "收藏"))
    assert "还没有可收藏的作品" in outputs[0]


@pytest.mark.asyncio
async def test_favorite_add_by_id_requires_pixiv_detail() -> None:
    harness = _Harness(store=_Store(), detail=None)
    outputs = await _collect(harness._handle_p_query(_Event(), "收藏 #12345678"))
    assert "没有找到作品 12345678" in outputs[0]
    assert "pixiv_refresh_token" in outputs[0]


@pytest.mark.asyncio
async def test_favorite_actions_report_disabled_feature() -> None:
    harness = _Harness(favorites_enabled=False, store=_Store())
    for query in ("收藏", "收藏列表", "取消收藏 #12345678"):
        outputs = await _collect(harness._handle_p_query(_Event(), query))
        assert "收藏功能未启用" in outputs[0]


@pytest.mark.asyncio
async def test_favorite_list_renders_rows_and_empty_state() -> None:
    store = _Store()
    harness = _Harness(store=store)
    event = _Event()

    empty = await _collect(harness._handle_p_query(event, "收藏列表"))
    assert "还没有收藏" in empty[0]

    await store.add(user_id="10001", illust=_illust())
    listed = await _collect(harness._handle_p_query(event, "收藏列表 5"))
    text = listed[0]
    assert "我的收藏（共 1 条" in text
    assert "夜の街" in text
    assert "画师A" in text
    assert "L站" in text
    assert "ID 12345678" in text


@pytest.mark.asyncio
async def test_favorite_remove_accepts_bare_pid_and_reports_result() -> None:
    store = _Store()
    harness = _Harness(store=store)
    event = _Event()
    await store.add(user_id="10001", illust=_illust())

    # 用户给的是纯 pid，而 Lolicon 收藏存的 illust_id 是 "pid:page"
    removed = await _collect(harness._handle_p_query(event, "取消收藏 #12345678"))
    assert "已取消收藏" in removed[0]

    missing = await _collect(harness._handle_p_query(event, "取消收藏 #12345678"))
    assert "收藏里没有这个作品" in missing[0]


@pytest.mark.asyncio
async def test_favorite_remove_without_id_uses_last_sent_illust() -> None:
    store = _Store()
    harness = _Harness(store=store)
    event = _Event()
    await store.add(user_id="10001", illust=_illust())
    harness._last_sent_illust["10001"] = _illust()

    removed = await _collect(harness._handle_p_query(event, "取消收藏"))
    assert "已取消收藏" in removed[0]
    assert await store.count("10001") == 0


@pytest.mark.asyncio
async def test_favorite_remove_without_id_or_cache_asks_for_one() -> None:
    harness = _Harness(store=_Store())
    outputs = await _collect(harness._handle_p_query(_Event(), "取消收藏"))
    assert "请指定要取消收藏的作品" in outputs[0]
