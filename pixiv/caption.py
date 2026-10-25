"""发图消息文案：标题、画师、作品链接与图源标注。

只做纯字符串拼装，不依赖 AstrBot，方便单测直接调用。
"""

from __future__ import annotations


CAPTION_MODE_FULL = "full"
CAPTION_MODE_COMPACT = "compact"
CAPTION_MODE_OFF = "off"

CAPTION_MODES = (CAPTION_MODE_FULL, CAPTION_MODE_COMPACT, CAPTION_MODE_OFF)

SOURCE_PIXIV = "pixiv"
SOURCE_LOLICON = "lolicon"

# 完整模式用全称，紧凑模式用短名；用户明确要求能看出图是 P 站还是 L 站。
SOURCE_LABELS = {
    SOURCE_PIXIV: "P站（Pixiv）",
    SOURCE_LOLICON: "L站（Lolicon）",
}
SOURCE_SHORT_LABELS = {
    SOURCE_PIXIV: "P站",
    SOURCE_LOLICON: "L站",
}

PIXIV_ARTWORK_URL = "https://www.pixiv.net/artworks/{pid}"


def normalize_caption_mode(value: object) -> str:
    text = str(value or "").strip().casefold()
    if text in ("off", "none", "disable", "disabled", "关闭", "无"):
        return CAPTION_MODE_OFF
    if text in ("compact", "short", "brief", "简短", "简洁"):
        return CAPTION_MODE_COMPACT
    return CAPTION_MODE_FULL


def illust_pid(illust: dict) -> str:
    """作品 ID（Pixiv 的 illust id）；Lolicon 的 ``id`` 形如 ``pid:page``。"""
    pid = str(illust.get("pid") or "").strip()
    if pid:
        return pid
    raw = str(illust.get("id") or "").strip()
    return raw.split(":", 1)[0] if raw else ""


def illust_page(illust: dict) -> int:
    """多页作品的页码（0 基）；单页作品返回 0。"""
    raw_page = illust.get("page")
    if raw_page is not None:
        try:
            return max(0, int(raw_page))
        except (TypeError, ValueError):
            return 0
    raw = str(illust.get("id") or "").strip()
    if ":" not in raw:
        return 0
    try:
        return max(0, int(raw.split(":", 1)[1]))
    except (TypeError, ValueError):
        return 0


def illust_artist(illust: dict) -> str:
    user = illust.get("user") or {}
    if isinstance(user, dict):
        return str(user.get("name") or "").strip()
    return ""


def illust_source_key(illust: dict) -> str:
    raw = str(illust.get("_source") or "").strip().casefold()
    if raw == SOURCE_LOLICON:
        return SOURCE_LOLICON
    return SOURCE_PIXIV


def illust_url(illust: dict) -> str:
    pid = illust_pid(illust)
    return PIXIV_ARTWORK_URL.format(pid=pid) if pid else ""


def build_caption(illust: dict, mode: object = CAPTION_MODE_FULL) -> str:
    """按配置拼装一条发图文案。

    - ``full``：标题 + 画师 + 作品链接 + 图源与 ID（默认）
    - ``compact``：单行，标题｜画师｜图源｜链接
    - ``off``：只保留标题与作品 ID
    """
    resolved = normalize_caption_mode(mode)
    title = str(illust.get("title") or "").strip() or "无标题"
    pid = illust_pid(illust)
    page = illust_page(illust)
    artist = illust_artist(illust)
    source = illust_source_key(illust)
    url = illust_url(illust)

    if resolved == CAPTION_MODE_OFF:
        return f"🎨 {title}" + (f" (ID: {pid})" if pid else "")

    if resolved == CAPTION_MODE_COMPACT:
        parts = [f"🎨 {title}"]
        if artist:
            parts.append(f"👤 {artist}")
        parts.append(f"📦 {SOURCE_SHORT_LABELS[source]}")
        if url:
            parts.append(f"🔗 {url}")
        return "｜".join(parts)

    lines = [f"🎨 {title}"]
    if artist:
        lines.append(f"👤 画师：{artist}")
    if url:
        lines.append(f"🔗 {url}")
    source_line = f"📦 图源：{SOURCE_LABELS[source]}"
    if pid:
        source_line += f" · Pixiv ID {pid}"
    if page > 0:
        source_line += f" · 第 {page + 1} 页"
    lines.append(source_line)
    return "\n".join(lines)
