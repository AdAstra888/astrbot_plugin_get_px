"""发图方向（横/竖/方）解析、Lolicon 预筛参数与本地精筛。

发图与签到背景共用同一套比例判定口径：签到背景固定要 3:4 竖图，
发图则由用户临时指定或读取配置默认值。所有函数只做纯计算，不触碰 IO。
"""

from __future__ import annotations

import unicodedata


# Lolicon 的 aspectRatio 只接受数值条件组合（gt/gte/lt/lte/eq，
# 例如 "gt0.6lt0.9"）。传 "vertical"/"landscape" 这类枚举值不是合法语法，
# 等于没有筛选，所以这里统一由目标比例推导。
ORIENTATION_LANDSCAPE = "landscape"
ORIENTATION_PORTRAIT = "portrait"
ORIENTATION_SQUARE = "square"
ORIENTATION_ANY = "any"

ORIENTATIONS = (
    ORIENTATION_ANY,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
    ORIENTATION_SQUARE,
)

ORIENTATION_LABELS = {
    ORIENTATION_ANY: "不限",
    ORIENTATION_LANDSCAPE: "横图",
    ORIENTATION_PORTRAIT: "竖图",
    ORIENTATION_SQUARE: "方图",
}

# 每个方向的宽高比区间（左闭右闭，``None`` 表示该侧不限）。
#
# 这里刻意用「大于 1 就是横、小于 1 就是竖」的宽口径，而不是签到背景那种
# 3:4 ±20% 的窄带：用户说「竖图」指的是竖着的图，9:16 的手机壁纸、2:3 的
# 插画都算，用 0.60~0.90 去卡会把一半竖图筛掉。方图仍用 ±12% 的窄带，
# 因为「方图」本身就是个精确概念。
#
# 上下界都写成有限值，是为了拼出 Lolicon 能接受的 ``gtXltY`` 双条件语法。
_ORIENTATION_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    ORIENTATION_LANDSCAPE: (1.05, 4.00),
    ORIENTATION_PORTRAIT: (0.25, 0.95),
    ORIENTATION_SQUARE: (0.88, 1.12),
}

# 用户可能输入的各种写法，统一归一到上面的四个值。
_ORIENTATION_ALIASES: dict[str, str] = {
    "any": ORIENTATION_ANY,
    "all": ORIENTATION_ANY,
    "auto": ORIENTATION_ANY,
    "不限": ORIENTATION_ANY,
    "随机": ORIENTATION_ANY,
    "全部": ORIENTATION_ANY,
    "landscape": ORIENTATION_LANDSCAPE,
    "horizontal": ORIENTATION_LANDSCAPE,
    "wide": ORIENTATION_LANDSCAPE,
    "横": ORIENTATION_LANDSCAPE,
    "横图": ORIENTATION_LANDSCAPE,
    "横向": ORIENTATION_LANDSCAPE,
    "横屏": ORIENTATION_LANDSCAPE,
    "宽图": ORIENTATION_LANDSCAPE,
    "壁纸": ORIENTATION_LANDSCAPE,
    "portrait": ORIENTATION_PORTRAIT,
    "vertical": ORIENTATION_PORTRAIT,
    "tall": ORIENTATION_PORTRAIT,
    "竖": ORIENTATION_PORTRAIT,
    "竖图": ORIENTATION_PORTRAIT,
    "纵向": ORIENTATION_PORTRAIT,
    "竖屏": ORIENTATION_PORTRAIT,
    "长图": ORIENTATION_PORTRAIT,
    "square": ORIENTATION_SQUARE,
    "方": ORIENTATION_SQUARE,
    "方图": ORIENTATION_SQUARE,
    "方形": ORIENTATION_SQUARE,
    "正方": ORIENTATION_SQUARE,
    "正方形": ORIENTATION_SQUARE,
}


def normalize_orientation(value: object) -> str:
    """把用户输入或配置值归一为 ``ORIENTATIONS`` 中的一项。

    无法识别时回落到 ``ORIENTATION_ANY``，调用方无需处理 None。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    if not text:
        return ORIENTATION_ANY
    return _ORIENTATION_ALIASES.get(text, ORIENTATION_ANY)


def orientation_label(value: object) -> str:
    return ORIENTATION_LABELS[normalize_orientation(value)]


def lolicon_aspect_ratio(value: object) -> str:
    """返回 Lolicon API 的 ``aspectRatio`` 参数值；不限方向时返回空串。"""
    orientation = normalize_orientation(value)
    bounds = _ORIENTATION_BOUNDS.get(orientation)
    if bounds is None:
        return ""
    lower, upper = bounds
    parts = []
    if lower is not None:
        parts.append(f"gt{lower:.2f}")
    if upper is not None:
        parts.append(f"lt{upper:.2f}")
    return "".join(parts)


def matches_orientation(illust: dict, value: object) -> bool:
    """本地精筛：作品的宽高比是否落在目标方向区间内。

    宽高比未知（缺少 width/height）时返回 ``False``——Lolicon 与 Pixiv
    正常都会返回尺寸，缺尺寸说明数据不完整，按不匹配处理更安全。
    """
    orientation = normalize_orientation(value)
    bounds = _ORIENTATION_BOUNDS.get(orientation)
    if bounds is None:
        return True
    ratio = illust_aspect_ratio(illust)
    if ratio <= 0:
        return False
    lower, upper = bounds
    if lower is not None and ratio < lower:
        return False
    if upper is not None and ratio > upper:
        return False
    return True


def filter_by_orientation(illusts: list[dict], value: object) -> list[dict]:
    orientation = normalize_orientation(value)
    if orientation == ORIENTATION_ANY:
        return list(illusts)
    return [illust for illust in illusts if matches_orientation(illust, orientation)]


def illust_aspect_ratio(illust: dict) -> float:
    width, height = illust_dimensions(illust)
    if width <= 0 or height <= 0:
        return 0.0
    return width / height


def illust_dimensions(illust: dict) -> tuple[int, int]:
    width = _positive_int(illust.get("width"))
    height = _positive_int(illust.get("height"))
    if width > 0 and height > 0:
        return width, height

    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        first_page = meta_pages[0] or {}
        image_urls = first_page.get("image_urls") or {}
        width = _positive_int(first_page.get("width") or image_urls.get("width"))
        height = _positive_int(first_page.get("height") or image_urls.get("height"))
        if width > 0 and height > 0:
            return width, height

    return 0, 0


def _positive_int(value: object) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0
