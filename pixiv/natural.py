"""自然语言发图请求解析（规则优先）。

``/p`` 有明确的参数边界，自然语言没有。这里只做一件事：把
「大肥鱼来张图」「来三张初音ミク图 竖图」这类句子拆成与
``/p`` 同构的 :class:`NaturalQuery`，交给 ``SearchMixin._handle_search``。

解析顺序固定：定位「来/整/搞/弄/发/给 + 数量 + 量词」这个触发骨架，
骨架之前是称呼或标签，骨架之后是标签与方向。所有函数只做纯计算，
不触碰 IO，也不读配置——别名表由调用方从配置读好传进来。
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from .orientation import ORIENTATION_ANY, ORIENTATION_LANDSCAPE
from .orientation import ORIENTATION_PORTRAIT, ORIENTATION_SQUARE
from .query import match_orientation_token


# 入口闸门。这里刻意放宽：只要句子「提到图」并且「带请求意味」，就放进
# 解析器，具体是不是发图请求交给 ``parse_natural_query`` 和（可选的）模型兜底
# 判断。写窄了会把「能不能帮我找点蓝色系的图」这类说法直接挡在门外，
# 模型兜底就永远没有机会。
#
# 注意 ``@filter.regex`` 用 fullmatch 语义，所以整体锚定 ``^...$``，
# 用前瞻表达式表达「含图片词」「含请求词」「长度不超过 60」三个条件。
AUTO_TRIGGER_PATTERN = (
    r"^/?"
    r"(?=[\s\S]{0,60}$)"
    r"(?=[\s\S]*(?:图|插画|插圖|壁纸|#\d{6,12}))"
    r"(?=[\s\S]*(?:来|要|整|搞|弄|发|给|找|求|想|拿|换|有))"
    r"[\s\S]*$"
)

# 骨架本身：动词 + 可选数量 + 量词。
_TRIGGER_PATTERN = re.compile(
    r"(?:来|整|搞|弄|发|给)\s*"
    r"(?P<count>[0-9零〇一二两俩三四五六七八九十几]{0,3})\s*"
    r"(?P<unit>份|个|张|点|些)"
)

_ILLUSTR_ID_PATTERN = re.compile(r"^#?(\d{6,12})$")

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "俩": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

# 句首的客套与祈使词，去掉后剩下的才是真正的称呼或标签。
# 长的放前面，避免「给我」被「给」抢先吃掉。
_LEAD_IN_WORDS = (
    "能不能",
    "可不可以",
    "可以",
    "麻烦",
    "帮我",
    "帮忙",
    "给我",
    "请",
    "你",
    "您",
    "快点",
    "赶紧",
    "那就",
    "快",
    "再",
    "那",
)

_TRAILING_PARTICLES = " \t\u3000吧呀啊哦嘛呢~～!！。.，,、"

# 句尾方向词。按长度倒序匹配，避免「正方形」被「正方」截断。
_ORIENTATION_SUFFIXES = (
    ("正方形", ORIENTATION_SQUARE),
    ("horizontal", ORIENTATION_LANDSCAPE),
    ("landscape", ORIENTATION_LANDSCAPE),
    ("portrait", ORIENTATION_PORTRAIT),
    ("vertical", ORIENTATION_PORTRAIT),
    ("square", ORIENTATION_SQUARE),
    ("横屏", ORIENTATION_LANDSCAPE),
    ("横图", ORIENTATION_LANDSCAPE),
    ("横向", ORIENTATION_LANDSCAPE),
    ("竖屏", ORIENTATION_PORTRAIT),
    ("竖图", ORIENTATION_PORTRAIT),
    ("纵向", ORIENTATION_PORTRAIT),
    ("方图", ORIENTATION_SQUARE),
    ("方形", ORIENTATION_SQUARE),
    ("正方", ORIENTATION_SQUARE),
    ("不限", ORIENTATION_ANY),
    ("any", ORIENTATION_ANY),
)

# 「福利」「涩」这类词只是语气修饰，不是标签；当成标签去搜会搜不到东西。
_DECORATION_WORDS = frozenset({"福利", "色", "瑟", "涩", "塞", "涩涩", "色色"})

# 过去时标记。自然语言入口是「要图」，出现这些词说明是在描述已经发生的事，
# 比如「我昨天发了一张图」，不该被当成发图请求。
_PAST_MARKERS = ("了", "过", "昨天", "前天", "刚才", "上次", "之前")

# 提问标记。问出处、问画师、问用法都不该触发发图。
_QUESTION_MARKERS = (
    "为什么",
    "怎么",
    "什么",
    "哪个",
    "哪里",
    "是谁",
    "画师",
    "出处",
    "是不是",
    "能不能教",
)


@dataclass(frozen=True)
class NaturalQuery:
    """一条自然语言消息解析出的发图请求。"""

    matched: bool = False
    tag: str = ""
    count: int = 1
    orientation: str = ORIENTATION_ANY
    illust_id: str = ""
    prefix: str = ""
    prefix_is_alias: bool = False
    source: str = "rule"


def parse_natural_query(text: object, aliases: object = ()) -> NaturalQuery:
    """解析自然语言发图请求；不是发图请求时返回 ``matched=False``。

    ``aliases`` 是机器人的称呼（如「大肥鱼」「neko」）。出现在句首时视为
    在叫机器人，从标签里剔除；不在表里的一律当标签，这样「初音来张图」
    仍然能搜到初音。
    """
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if raw.startswith("/"):
        raw = raw[1:].strip()
    if not raw:
        return NaturalQuery()

    match = _TRIGGER_PATTERN.search(raw)
    if not match:
        return NaturalQuery()

    prefix_text = raw[: match.start()].strip()
    after = raw[match.end() :].rstrip(_TRAILING_PARTICLES)
    # 「发了一张图」「发过一张图」是在描述已发生的事，不是要图。
    if after.lstrip().startswith(("了", "过")):
        return NaturalQuery()
    after, orientation = _extract_orientation(after)

    illust_id = ""
    tag = _strip_tag(_take_before_last_picture(after))
    id_match = _ILLUSTR_ID_PATTERN.match(tag)
    if id_match:
        illust_id = id_match.group(1)
        tag = ""
    elif tag in _DECORATION_WORDS:
        tag = ""

    alias_keys = _normalize_aliases(aliases)
    prefix = _strip_lead_ins(prefix_text)
    prefix_is_alias = bool(prefix) and prefix.casefold() in alias_keys
    if prefix_is_alias:
        prefix = ""
    if prefix:
        tag = f"{prefix} {tag}".strip()

    return NaturalQuery(
        matched=True,
        tag=tag,
        count=_parse_count(match.group("count")),
        orientation=orientation,
        illust_id=illust_id,
        prefix=prefix,
        prefix_is_alias=prefix_is_alias,
    )


def looks_like_past_tense(text: object) -> bool:
    """句子是否在描述已发生的事（含「了」「过」「昨天」等标记）。"""
    value = str(text or "")
    return any(marker in value for marker in _PAST_MARKERS)


def looks_like_question(text: object) -> bool:
    """句子是否在提问（问出处、问画师、问怎么用）。

    这类句子常常也带「图」和「要」「想」，光靠闸门分不出来，但它们不该
    触发发图，更不该送去模型兜底白烧 token。
    """
    value = str(text or "")
    if value.rstrip().endswith(("？", "?")):
        return True
    return any(marker in value for marker in _QUESTION_MARKERS)


def _parse_count(raw: object) -> int:
    """把「3」「三」「十二」「两」解析成正整数；无法识别按 1 处理。"""
    text = str(raw or "").strip()
    if not text:
        return 1
    if text.isdigit():
        value = int(text)
        return value if value > 0 else 1
    if "十" in text:
        head, _, tail = text.partition("十")
        tens = _CN_DIGITS.get(head, 1) if head else 1
        ones = _CN_DIGITS.get(tail, 0) if tail else 0
        value = tens * 10 + ones
        return value if value > 0 else 1
    value = _CN_DIGITS.get(text)
    return value if value and value > 0 else 1


def _extract_orientation(text: str) -> tuple[str, str]:
    """从句尾摘掉方向词，返回 ``(剩余文本, 方向)``。

    先按空白切词整词匹配，再退一步做后缀匹配，这样「初音图 竖图」和
    「初音竖图」两种写法都能认出来。
    """
    tokens = text.split()
    orientation = ORIENTATION_ANY
    while tokens:
        resolved = match_orientation_token(tokens[-1])
        if not resolved:
            break
        orientation = resolved
        tokens.pop()
    remainder = " ".join(tokens).strip()
    if orientation != ORIENTATION_ANY:
        return remainder, orientation

    for suffix, value in _ORIENTATION_SUFFIXES:
        if remainder.casefold().endswith(suffix) and len(remainder) > len(suffix):
            return remainder[: -len(suffix)].strip(), value
    return remainder, orientation


def _take_before_last_picture(text: str) -> str:
    """取最后一个「图」之前的部分。

    用最后一个而不是第一个，是因为「初音图」里的「图」才是句子结构里的
    那个「图」，「图」本身也可能出现在标签中间。
    """
    index = text.rfind("图")
    if index < 0:
        return text
    return text[:index]


def _strip_tag(value: str) -> str:
    text = value.strip(" \t\u3000")
    # 「大肥鱼的图」→「大肥鱼」；只在句尾去「的」，不碰标签中间的。
    text = re.sub(r"(?:的|之)+$", "", text).strip()
    return text


def _strip_lead_ins(value: str) -> str:
    text = value.strip()
    changed = True
    while changed and text:
        changed = False
        for word in _LEAD_IN_WORDS:
            if text.startswith(word):
                text = text[len(word) :].lstrip("，,、 ")
                changed = True
                break
    return text.strip("，,、~～!！。. ")


def resolve_aliases(bot_name: object = "", extra: object = "") -> list[str]:
    """汇总机器人称呼：配置的角色名 + 用户额外填写的别名。

    ``bot_name`` 是签到卡片里那个角色名（``checkin_bot_name``），用户通常
    就是拿它叫机器人；``extra`` 是逗号分隔的补充别名。
    """
    items = [str(bot_name or "")]
    items.extend(re.split(r"[,，、;；/\s]+", str(extra or "")))
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = unicodedata.normalize("NFKC", item).strip()
        if not normalized or normalized.casefold() in seen:
            continue
        seen.add(normalized.casefold())
        result.append(normalized)
    return result


def _normalize_aliases(aliases: object) -> set[str]:
    if isinstance(aliases, str):
        items = re.split(r"[,，、;；/\s]+", aliases)
    else:
        items = [str(item) for item in (aliases or ())]
    return {
        unicodedata.normalize("NFKC", item).strip().casefold()
        for item in items
        if unicodedata.normalize("NFKC", item).strip()
    }
