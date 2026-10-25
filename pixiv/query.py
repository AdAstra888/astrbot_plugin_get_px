"""``/p`` 指令参数解析。

发图指令需要同时支持标签、数量、方向、按作品 ID 发图和收藏操作，
全部塞在 ``GreedyStr`` 里，所以这里集中做一次解析，把结果交给
``SearchMixin`` / ``PCommandMixin``，避免解析逻辑散落在指令层。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .orientation import (
    ORIENTATION_ANY,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
    ORIENTATION_SQUARE,
)


ACTION_SEARCH = "search"
ACTION_FAVORITE_ADD = "favorite_add"
ACTION_FAVORITE_REMOVE = "favorite_remove"
ACTION_FAVORITE_LIST = "favorite_list"
ACTION_HELP = "help"

# 只有明确写成 #数字 或 Pixiv 作品链接时才算「按 ID 发图」。
# 裸数字保留原语义（数量），避免破坏 /p 标签 3 的既有用法。
_ILLUSTR_ID_PATTERN = re.compile(r"^#(\d{1,12})$")
_PIXIV_URL_PATTERN = re.compile(
    r"^(?:https?://)?(?:www\.)?pixiv\.net/(?:[a-z]{2}/)?artworks/(\d{1,12})/?$",
    re.IGNORECASE,
)
_BARE_ID_PATTERN = re.compile(r"^#?(\d{1,12})$")

_FAVORITE_LIST_WORDS = ("收藏列表", "收藏夹", "我的收藏", "收藏清单")
_FAVORITE_REMOVE_WORDS = ("取消收藏", "删除收藏", "移除收藏")
_FAVORITE_ADD_WORDS = ("收藏",)
_HELP_WORDS = ("帮助", "help", "?", "？")


@dataclass(frozen=True)
class SearchQuery:
    """一次 ``/p`` 调用的解析结果。"""

    action: str = ACTION_SEARCH
    tag: str = ""
    count: str = ""
    orientation: str = ""
    illust_id: str = ""

    @property
    def has_orientation(self) -> bool:
        return self.orientation not in ("", ORIENTATION_ANY)


def parse_p_query(raw: object) -> SearchQuery:
    """把 ``/p`` 的原始参数解析成 :class:`SearchQuery`。

    支持的形式（顺序无关，除动作词必须在首位）::

        /p                     随机 1 张
        /p 初音ミク 3           标签 + 数量
        /p 初音ミク 3 横图      标签 + 数量 + 方向
        /p 横图 初音ミク        方向 + 标签
        /p #12345678           按作品 ID 发图
        /p 收藏                收藏最近一次发过的作品
        /p 收藏 #12345678       按 ID 收藏
        /p 收藏列表 20          查看收藏（最多 20 条）
        /p 取消收藏 12345678    取消收藏
    """
    tokens = str(raw or "").split()
    if not tokens:
        return SearchQuery()

    action = ACTION_SEARCH
    head = tokens[0]
    if head in _FAVORITE_LIST_WORDS:
        action = ACTION_FAVORITE_LIST
        tokens = tokens[1:]
    elif head in _FAVORITE_REMOVE_WORDS:
        action = ACTION_FAVORITE_REMOVE
        tokens = tokens[1:]
    elif head in _FAVORITE_ADD_WORDS:
        action = ACTION_FAVORITE_ADD
        tokens = tokens[1:]
    elif head in _HELP_WORDS:
        return SearchQuery(action=ACTION_HELP)

    if action == ACTION_FAVORITE_LIST:
        count = tokens[0] if tokens and tokens[0].isdigit() else ""
        return SearchQuery(action=action, count=count)

    if action in (ACTION_FAVORITE_ADD, ACTION_FAVORITE_REMOVE):
        return SearchQuery(
            action=action, illust_id=_first_bare_id(tokens)
        )

    return _parse_search_tokens(tokens)


def _parse_search_tokens(tokens: list[str]) -> SearchQuery:
    count = ""
    illust_id = ""
    orientation = ""
    tag_tokens: list[str] = []

    for token in tokens:
        matched_id = _match_illust_id(token)
        if matched_id and not illust_id:
            illust_id = matched_id
            continue
        # 纯数字：位数长的是作品 ID，短的是数量。
        if token.isdigit():
            if _looks_like_illust_id(token) and not illust_id:
                illust_id = token
            elif not _looks_like_illust_id(token):
                count = token
            continue
        if not orientation:
            resolved = _match_orientation(token)
            if resolved:
                orientation = resolved
                continue
        tag_tokens.append(token)

    return SearchQuery(
        tag=" ".join(tag_tokens),
        count=count,
        orientation=orientation,
        illust_id=illust_id,
    )


def _looks_like_illust_id(token: str) -> bool:
    """纯数字 token 是作品 ID 还是数量。

    单次发图上限是 ``MAX_IMAGE_COUNT``（20），数量不会超过两位数；
    Pixiv 作品 ID 都在 6 位以上。中间地带（3-5 位）按数量处理，
    反正会被上限截断，不会误伤标签。
    """
    return len(token) >= 6


def _match_illust_id(token: str) -> str:
    match = _ILLUSTR_ID_PATTERN.match(token) or _PIXIV_URL_PATTERN.match(token)
    return match.group(1) if match else ""


def _first_bare_id(tokens: list[str]) -> str:
    for token in tokens:
        matched = _match_illust_id(token)
        if matched:
            return matched
        bare = _BARE_ID_PATTERN.match(token)
        if bare:
            return bare.group(1)
    return ""


def match_orientation_token(token: object) -> str:
    """只把明确的写法当方向词，避免吃掉正常的搜索标签。

    这里刻意不用 ``orientation.py`` 的全量别名表：「壁纸」「长图」「宽图」
    本身是常见的 Pixiv 标签，当方向词摘掉会让用户搜不到图。

    自然语言入口（``pixiv/natural.py``）复用同一张表，保证 ``/p`` 与
    自然语言对「横图」「竖图」的判定口径一致。
    """
    text = unicodedata.normalize("NFKC", str(token or "")).strip().casefold()
    return _ORIENTATION_TOKENS.get(text, "")


def _match_orientation(token: str) -> str:
    return match_orientation_token(token)


_ORIENTATION_TOKENS = {
    "横图": ORIENTATION_LANDSCAPE,
    "横屏": ORIENTATION_LANDSCAPE,
    "横向": ORIENTATION_LANDSCAPE,
    "横版": ORIENTATION_LANDSCAPE,
    "landscape": ORIENTATION_LANDSCAPE,
    "horizontal": ORIENTATION_LANDSCAPE,
    "竖图": ORIENTATION_PORTRAIT,
    "竖屏": ORIENTATION_PORTRAIT,
    "纵向": ORIENTATION_PORTRAIT,
    "竖版": ORIENTATION_PORTRAIT,
    "portrait": ORIENTATION_PORTRAIT,
    "vertical": ORIENTATION_PORTRAIT,
    "方图": ORIENTATION_SQUARE,
    "方形": ORIENTATION_SQUARE,
    "正方": ORIENTATION_SQUARE,
    "正方形": ORIENTATION_SQUARE,
    "square": ORIENTATION_SQUARE,
    "不限": ORIENTATION_ANY,
    "any": ORIENTATION_ANY,
}
