"""``/p`` 指令分发与收藏流程。

``parse_p_query`` 负责纯文本解析，本模块负责把解析结果落成一次具体的
动作：搜索发图、收藏、取消收藏、查看收藏或帮助。业务实现仍复用
``SearchMixin`` 的取图链路，收藏存储走 ``FavoriteStore``。
"""

from __future__ import annotations

from astrbot.api.all import logger
from astrbot.api.event import AstrMessageEvent

from .caption import SOURCE_SHORT_LABELS, illust_pid
from .favorites import DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT
from .query import (
    ACTION_FAVORITE_ADD,
    ACTION_FAVORITE_LIST,
    ACTION_FAVORITE_REMOVE,
    ACTION_HELP,
    parse_p_query,
)


LOG_PREFIX = "[GetPx]"

HELP_TEXT = """📖 发图指令
/p [标签] [数量] [方向]   搜索并发送图片
    /p 初音ミク 3           发 3 张「初音ミク」
    /p 初音ミク 3 横图      只要横图（竖图 / 方图 同理）
    /p 横图                 不带标签时随机横图
/p #12345678             按作品 ID 直接发图
/p 收藏 [#ID]            收藏最近发过的图，或收藏指定作品
/p 收藏列表 [数量]       查看自己的收藏（默认 10 条，最多 50 条）
/p 取消收藏 [#ID]        取消收藏
/p 帮助                  显示本帮助"""


class PCommandMixin:
    """``/p`` 指令的参数分发与收藏动作。"""

    # ── 配置与工具 ────────────────────────────────────────────

    def _favorites_enabled(self) -> bool:
        return self._cfg_bool("favorites_enabled", True)

    def _favorite_store_or_none(self):
        store = getattr(self, "favorite_store", None)
        if store is None or not self._favorites_enabled():
            return None
        return store

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "")

    def _last_sent_illust_of(self, event: AstrMessageEvent) -> dict | None:
        cache = getattr(self, "_last_sent_illust", None)
        if not cache:
            return None
        return cache.get(self._sender_id(event))

    # ── 主分发 ────────────────────────────────────────────────

    async def _handle_p_query(self, event: AstrMessageEvent, raw_query: str):
        query = parse_p_query(raw_query)

        if query.action == ACTION_HELP:
            yield event.plain_result(HELP_TEXT)
            return

        if query.action == ACTION_FAVORITE_ADD:
            async for result in self._handle_favorite_add(event, query.illust_id):
                yield result
            return

        if query.action == ACTION_FAVORITE_REMOVE:
            async for result in self._handle_favorite_remove(event, query.illust_id):
                yield result
            return

        if query.action == ACTION_FAVORITE_LIST:
            async for result in self._handle_favorite_list(event, query.count):
                yield result
            return

        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 图片源暂不可用，请配置 Lolicon API，或填写 pixiv_refresh_token 作为回退"
            )
            return

        async for result in self._handle_search(
            event,
            tag=query.tag,
            count_str=query.count,
            orientation=query.orientation,
            illust_id=query.illust_id,
        ):
            yield result

    # ── 收藏 ──────────────────────────────────────────────────

    async def _handle_favorite_add(self, event: AstrMessageEvent, illust_id: str):
        store = self._favorite_store_or_none()
        if store is None:
            yield event.plain_result("📚 收藏功能未启用，请联系管理员开启")
            return

        user_id = self._sender_id(event)
        raw_id = str(illust_id or "").strip()

        illust: dict | None
        if raw_id:
            illust = await self._fetch_illust_by_id(event, raw_id)
            if illust is None:
                yield event.plain_result(
                    f"❌ 没有找到作品 {raw_id}，无法收藏"
                    "（按 ID 收藏需要配置 pixiv_refresh_token）"
                )
                return
        else:
            illust = self._last_sent_illust_of(event)
            if not illust:
                yield event.plain_result(
                    "🤔 还没有可收藏的作品，先发一张图，或用 /p 收藏 #作品ID"
                )
                return

        try:
            created = await store.add(
                user_id=user_id,
                illust=illust,
                scope=self._event_scope(event),
            )
            total = await store.count(user_id)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 收藏作品失败: "
                f"illust_id={illust.get('id', '')} "
                f"error_type={type(exc).__name__}"
            )
            yield event.plain_result("😢 收藏失败，请稍后再试")
            return

        title = str(illust.get("title") or "无标题").strip()
        pid = illust_pid(illust)
        if created:
            yield event.plain_result(
                f"⭐ 已收藏「{title}」" + (f"（ID: {pid}）" if pid else "")
                + f"\n当前共 {total} 条收藏，用 /p 收藏列表 查看"
            )
        else:
            yield event.plain_result(
                f"ℹ️ 「{title}」已经在收藏里了" + (f"（ID: {pid}）" if pid else "")
            )

    async def _handle_favorite_remove(self, event: AstrMessageEvent, illust_id: str):
        store = self._favorite_store_or_none()
        if store is None:
            yield event.plain_result("📚 收藏功能未启用，请联系管理员开启")
            return

        user_id = self._sender_id(event)
        raw_id = str(illust_id or "").strip()
        if not raw_id:
            last = self._last_sent_illust_of(event)
            raw_id = str((last or {}).get("id") or "").strip()
            if not raw_id:
                yield event.plain_result(
                    "🤔 请指定要取消收藏的作品，例如 /p 取消收藏 #12345678"
                )
                return

        try:
            removed = await store.remove(user_id=user_id, illust_id=raw_id)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 取消收藏失败: illust_id={raw_id} "
                f"error_type={type(exc).__name__}"
            )
            yield event.plain_result("😢 取消收藏失败，请稍后再试")
            return

        if removed:
            yield event.plain_result(f"🗑️ 已取消收藏（ID: {raw_id}）")
        else:
            yield event.plain_result(f"ℹ️ 收藏里没有这个作品（ID: {raw_id}）")

    async def _handle_favorite_list(self, event: AstrMessageEvent, count_str: str):
        store = self._favorite_store_or_none()
        if store is None:
            yield event.plain_result("📚 收藏功能未启用，请联系管理员开启")
            return

        user_id = self._sender_id(event)
        try:
            limit = int(count_str) if str(count_str or "").strip() else DEFAULT_LIST_LIMIT
        except (TypeError, ValueError):
            limit = DEFAULT_LIST_LIMIT
        limit = max(1, min(limit, MAX_LIST_LIMIT))

        try:
            rows = await store.list_favorites(user_id, limit=limit)
            total = await store.count(user_id)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 读取收藏列表失败: "
                f"error_type={type(exc).__name__}"
            )
            yield event.plain_result("😢 读取收藏失败，请稍后再试")
            return

        if not rows:
            yield event.plain_result(
                "📭 还没有收藏。发图后使用 /p 收藏，或用 /p 收藏 #作品ID"
            )
            return

        lines = [f"⭐ 我的收藏（共 {total} 条，显示最近 {len(rows)} 条）"]
        for index, row in enumerate(rows, 1):
            title = str(row.get("title") or "无标题").strip()
            author = str(row.get("author") or "").strip()
            source = SOURCE_SHORT_LABELS.get(
                str(row.get("source") or ""), str(row.get("source") or "")
            )
            pid = str(row.get("pid") or "").strip()
            url = str(row.get("url") or "").strip()
            line = f"{index}. {title}"
            if author:
                line += f" / {author}"
            if source:
                line += f" · {source}"
            if pid:
                line += f" · ID {pid}"
            if url:
                line += f"\n    {url}"
            lines.append(line)
        yield event.plain_result("\n".join(lines))
