"""发图文案（pixiv.caption）的单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.caption import (
    CAPTION_MODE_COMPACT,
    CAPTION_MODE_FULL,
    CAPTION_MODE_OFF,
    build_caption,
    illust_artist,
    illust_page,
    illust_pid,
    illust_source_key,
    illust_url,
    normalize_caption_mode,
)


def _lolicon_illust(**extra):
    payload = {
        "id": "12345678:2",
        "pid": "12345678",
        "page": 2,
        "title": "夜の街",
        "user": {"id": "100", "name": "画师A"},
        "_source": "lolicon",
    }
    payload.update(extra)
    return payload


def _pixiv_illust(**extra):
    payload = {
        "id": "98765432",
        "title": "朝の光",
        "user": {"id": "200", "name": "画师B"},
        "_source": "pixiv",
    }
    payload.update(extra)
    return payload


def test_normalize_caption_mode_accepts_aliases() -> None:
    assert normalize_caption_mode("full") == CAPTION_MODE_FULL
    assert normalize_caption_mode("compact") == CAPTION_MODE_COMPACT
    assert normalize_caption_mode("简短") == CAPTION_MODE_COMPACT
    assert normalize_caption_mode("off") == CAPTION_MODE_OFF
    assert normalize_caption_mode("关闭") == CAPTION_MODE_OFF
    # 未知值回落到 full，避免用户配错导致文案消失
    assert normalize_caption_mode("乱写") == CAPTION_MODE_FULL
    assert normalize_caption_mode(None) == CAPTION_MODE_FULL


def test_illust_identity_helpers() -> None:
    lolicon = _lolicon_illust()
    assert illust_pid(lolicon) == "12345678"
    assert illust_page(lolicon) == 2
    assert illust_artist(lolicon) == "画师A"
    assert illust_source_key(lolicon) == "lolicon"
    assert illust_url(lolicon) == "https://www.pixiv.net/artworks/12345678"

    pixiv = _pixiv_illust()
    assert illust_pid(pixiv) == "98765432"
    assert illust_page(pixiv) == 0
    assert illust_source_key(pixiv) == "pixiv"


def test_illust_pid_falls_back_to_composite_id() -> None:
    """Lolicon 的 id 是 ``pid:page``，缺 pid 字段时要能拆出来。"""
    illust = {"id": "555555:0", "title": "t", "_source": "lolicon"}
    assert illust_pid(illust) == "555555"
    assert illust_page(illust) == 0
    assert illust_url(illust) == "https://www.pixiv.net/artworks/555555"


def test_illust_helpers_tolerate_missing_fields() -> None:
    assert illust_pid({}) == ""
    assert illust_page({}) == 0
    assert illust_artist({}) == ""
    assert illust_url({}) == ""
    # 未知来源按 Pixiv 处理
    assert illust_source_key({}) == "pixiv"


def test_full_caption_includes_artist_source_id_and_url() -> None:
    caption = build_caption(_lolicon_illust(), CAPTION_MODE_FULL)
    lines = caption.split("\n")
    assert lines[0] == "🎨 夜の街"
    assert lines[1] == "👤 画师：画师A"
    assert lines[2] == "🔗 https://www.pixiv.net/artworks/12345678"
    assert "L站（Lolicon）" in lines[3]
    assert "Pixiv ID 12345678" in lines[3]
    assert "第 3 页" in lines[3]


def test_full_caption_marks_pixiv_source() -> None:
    caption = build_caption(_pixiv_illust(), CAPTION_MODE_FULL)
    assert "P站（Pixiv）" in caption
    assert "L站" not in caption
    assert "Pixiv ID 98765432" in caption
    # 单页作品不显示页码
    assert "第 1 页" not in caption


def test_compact_caption_is_single_line() -> None:
    caption = build_caption(_lolicon_illust(), CAPTION_MODE_COMPACT)
    assert "\n" not in caption
    assert caption.startswith("🎨 夜の街｜👤 画师A｜📦 L站")
    assert "https://www.pixiv.net/artworks/12345678" in caption


def test_off_caption_keeps_title_and_id_only() -> None:
    caption = build_caption(_lolicon_illust(), CAPTION_MODE_OFF)
    assert caption == "🎨 夜の街 (ID: 12345678)"
    assert "画师" not in caption
    assert "https://" not in caption


def test_caption_handles_missing_artist_and_title() -> None:
    illust = {"id": "111111", "_source": "pixiv"}
    caption = build_caption(illust, CAPTION_MODE_FULL)
    assert "🎨 无标题" in caption
    assert "画师" not in caption
    assert "Pixiv ID 111111" in caption
