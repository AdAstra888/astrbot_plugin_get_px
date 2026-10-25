"""发图方向筛选（pixiv.orientation）的单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.orientation import (
    ORIENTATION_ANY,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
    ORIENTATION_SQUARE,
    filter_by_orientation,
    illust_aspect_ratio,
    illust_dimensions,
    lolicon_aspect_ratio,
    matches_orientation,
    normalize_orientation,
    orientation_label,
)


def _illust(width, height, **extra):
    payload = {"id": "1", "width": width, "height": height}
    payload.update(extra)
    return payload


def test_normalize_orientation_accepts_aliases_and_falls_back_to_any() -> None:
    assert normalize_orientation("横图") == ORIENTATION_LANDSCAPE
    assert normalize_orientation("横屏") == ORIENTATION_LANDSCAPE
    assert normalize_orientation("Landscape") == ORIENTATION_LANDSCAPE
    assert normalize_orientation("竖图") == ORIENTATION_PORTRAIT
    assert normalize_orientation("vertical") == ORIENTATION_PORTRAIT
    assert normalize_orientation("正方形") == ORIENTATION_SQUARE
    assert normalize_orientation("不限") == ORIENTATION_ANY
    assert normalize_orientation("") == ORIENTATION_ANY
    assert normalize_orientation(None) == ORIENTATION_ANY
    assert normalize_orientation("这不是方向") == ORIENTATION_ANY


def test_orientation_label_is_chinese() -> None:
    assert orientation_label(ORIENTATION_LANDSCAPE) == "横图"
    assert orientation_label(ORIENTATION_PORTRAIT) == "竖图"
    assert orientation_label(ORIENTATION_SQUARE) == "方图"
    assert orientation_label(ORIENTATION_ANY) == "不限"


def test_lolicon_aspect_ratio_builds_numeric_bounds() -> None:
    # 横图：宽高比 > 1.05 且 < 4.00
    assert lolicon_aspect_ratio(ORIENTATION_LANDSCAPE) == "gt1.05lt4.00"
    # 竖图：宽高比 > 0.25 且 < 0.95
    assert lolicon_aspect_ratio(ORIENTATION_PORTRAIT) == "gt0.25lt0.95"
    # 方图：0.88..1.12，±12% 窄带
    assert lolicon_aspect_ratio(ORIENTATION_SQUARE) == "gt0.88lt1.12"
    # 不限方向时不传 aspectRatio，交给上游随机
    assert lolicon_aspect_ratio(ORIENTATION_ANY) == ""


def test_portrait_covers_phone_wallpapers_and_2_by_3_illusts() -> None:
    """竖图是「竖着」的图，不能只认 3:4 那一小段。

    早期实现沿用签到背景的 3:4 ±20%（0.60~0.90），结果 9:16 手机壁纸
    （0.5625）和 2:3 插画（0.667）虽然也是竖图，却被判成不匹配。
    """
    assert matches_orientation(_illust(1080, 1920), ORIENTATION_PORTRAIT) is True
    assert matches_orientation(_illust(1200, 1800), ORIENTATION_PORTRAIT) is True
    assert matches_orientation(_illust(1080, 1440), ORIENTATION_PORTRAIT) is True
    # 明显是横图的不该被算作竖图
    assert matches_orientation(_illust(1920, 1080), ORIENTATION_PORTRAIT) is False


def test_landscape_covers_ultrawide_and_4_by_3() -> None:
    assert matches_orientation(_illust(1920, 1080), ORIENTATION_LANDSCAPE) is True
    assert matches_orientation(_illust(2560, 1080), ORIENTATION_LANDSCAPE) is True
    assert matches_orientation(_illust(1600, 1200), ORIENTATION_LANDSCAPE) is True
    assert matches_orientation(_illust(1000, 1000), ORIENTATION_LANDSCAPE) is False


def test_matches_orientation_uses_aspect_ratio() -> None:
    assert matches_orientation(_illust(1920, 1080), ORIENTATION_LANDSCAPE) is True
    assert matches_orientation(_illust(1080, 1920), ORIENTATION_LANDSCAPE) is False
    assert matches_orientation(_illust(1080, 1440), ORIENTATION_PORTRAIT) is True
    assert matches_orientation(_illust(1000, 1000), ORIENTATION_SQUARE) is True
    assert matches_orientation(_illust(1920, 1080), ORIENTATION_SQUARE) is False
    # 不限方向时一律通过
    assert matches_orientation(_illust(1, 100), ORIENTATION_ANY) is True


def test_matches_orientation_rejects_unknown_dimensions() -> None:
    """缺尺寸时不能当成「匹配」，否则方向筛选形同虚设。"""
    assert matches_orientation({"id": "1"}, ORIENTATION_LANDSCAPE) is False
    assert matches_orientation(_illust(0, 0), ORIENTATION_PORTRAIT) is False


def test_illust_dimensions_falls_back_to_meta_pages() -> None:
    illust = {
        "id": "1",
        "meta_pages": [
            {"width": 1200, "height": 1600, "image_urls": {"large": "https://x"}}
        ],
    }
    assert illust_dimensions(illust) == (1200, 1600)
    assert illust_aspect_ratio(illust) == 1200 / 1600

    nested = {
        "id": "2",
        "meta_pages": [{"image_urls": {"width": "900", "height": "1200"}}],
    }
    assert illust_dimensions(nested) == (900, 1200)


def test_illust_dimensions_returns_zero_when_missing() -> None:
    assert illust_dimensions({"id": "1"}) == (0, 0)
    assert illust_aspect_ratio({"id": "1"}) == 0.0
    assert illust_dimensions({"id": "1", "meta_pages": []}) == (0, 0)


def test_filter_by_orientation_keeps_only_matching_items() -> None:
    illusts = [
        _illust(1920, 1080),
        _illust(1080, 1920),
        _illust(1000, 1000),
        {"id": "no-size"},
    ]
    landscape = filter_by_orientation(illusts, ORIENTATION_LANDSCAPE)
    assert landscape == [illusts[0]]

    portrait = filter_by_orientation(illusts, ORIENTATION_PORTRAIT)
    assert portrait == [illusts[1]]

    square = filter_by_orientation(illusts, ORIENTATION_SQUARE)
    assert square == [illusts[2]]

    # 不限方向返回全部（含缺尺寸的），且是副本
    every = filter_by_orientation(illusts, ORIENTATION_ANY)
    assert every == illusts
    assert every is not illusts
