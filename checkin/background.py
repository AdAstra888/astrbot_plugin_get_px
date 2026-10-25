"""签到背景比例工具。

比例判定、尺寸提取等纯计算逻辑已下沉到 ``pixiv/orientation.py``，
发图方向筛选与签到背景共用同一套口径。本模块只保留签到背景专用的
目标比例常量，并把旧名字重新导出，避免既有调用方改动。
"""

from __future__ import annotations

from ..pixiv.orientation import (  # noqa: F401  (对外重新导出)
    illust_aspect_ratio,
    illust_dimensions,
)


CHECKIN_ARTWORK_TARGET_RATIO = 0.75
CHECKIN_ARTWORK_TOLERANCE = 0.20

# Lolicon aspectRatio 只接受数值条件组合（gt/gte/lt/lte/eq，如 "gt0.6lt0.9"）；
# "vertical"/"landscape" 等枚举值不是合法语法，传了等于没筛。
# 由目标比例和容差推导，保持与本地精筛同一数值口径。
CHECKIN_ARTWORK_ASPECT_PARAM = (
    f"gt{CHECKIN_ARTWORK_TARGET_RATIO * (1 - CHECKIN_ARTWORK_TOLERANCE):.2f}"
    f"lt{CHECKIN_ARTWORK_TARGET_RATIO * (1 + CHECKIN_ARTWORK_TOLERANCE):.2f}"
)


def parse_aspect_ratio(value: object) -> float:
    raw = str(value or "").strip().lower()
    if not raw:
        return 0.0

    normalized = (
        raw.replace("：", ":").replace("／", "/").replace("×", "x").replace(" ", "")
    )
    for separator in (":", "/", "x"):
        if separator not in normalized:
            continue
        parts = normalized.split(separator)
        if len(parts) != 2:
            return 0.0
        return _ratio_from_numbers(parts[0], parts[1])

    try:
        ratio = float(normalized)
    except (TypeError, ValueError):
        return 0.0
    return ratio if ratio > 0 else 0.0


def filter_illusts_by_aspect_ratio(
    illusts: list[dict],
    target_ratio: float,
    tolerance: float,
) -> list[dict]:
    if target_ratio <= 0:
        return list(illusts)
    return [
        illust
        for illust in illusts
        if aspect_ratio_matches(illust, target_ratio, tolerance)
    ]


def aspect_ratio_matches(illust: dict, target_ratio: float, tolerance: float) -> bool:
    if target_ratio <= 0:
        return True
    actual_ratio = illust_aspect_ratio(illust)
    if actual_ratio <= 0:
        return False
    allowed_delta = target_ratio * max(0.0, tolerance)
    return abs(actual_ratio - target_ratio) <= allowed_delta


def _ratio_from_numbers(width_text: str, height_text: str) -> float:
    try:
        width = float(width_text)
        height = float(height_text)
    except (TypeError, ValueError):
        return 0.0
    if width <= 0 or height <= 0:
        return 0.0
    return width / height
