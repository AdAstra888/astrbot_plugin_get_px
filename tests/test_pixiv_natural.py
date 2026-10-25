"""自然语言发图解析（pixiv.natural）的单元测试。"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.natural import (
    AUTO_TRIGGER_PATTERN,
    looks_like_past_tense,
    looks_like_question,
    parse_natural_query,
    resolve_aliases,
)
from astrbot_plugin_get_px.pixiv.orientation import (
    ORIENTATION_ANY,
    ORIENTATION_LANDSCAPE,
    ORIENTATION_PORTRAIT,
)

ALIASES = ["大肥鱼", "肥鱼"]


def _parse(text, aliases=ALIASES):
    return parse_natural_query(text, aliases)


# ── 称呼与标签的区分 ──────────────────────────────────────────


def test_alias_prefix_is_dropped_from_tag() -> None:
    """「大肥鱼来张图」= 在叫机器人，发随机图，不能把称呼当标签。"""
    query = _parse("大肥鱼来张图")

    assert query.matched
    assert query.tag == ""
    assert query.prefix_is_alias is True
    assert query.count == 1


def test_alias_prefix_keeps_tag_after_trigger() -> None:
    query = _parse("大肥鱼 来 三张 初音图")

    assert query.matched
    assert query.tag == "初音"
    assert query.count == 3
    assert query.prefix_is_alias is True


def test_non_alias_prefix_becomes_tag() -> None:
    """「初音来张图」里的「初音」不是称呼，应当当标签搜。"""
    query = _parse("初音来张图")

    assert query.matched
    assert query.tag == "初音"
    assert query.prefix_is_alias is False


def test_prefix_before_trigger_is_joined_with_tag_after() -> None:
    query = _parse("大肥鱼 来张 初音图")

    assert query.tag == "初音"
    assert query.prefix_is_alias is True


# ── 数量 ─────────────────────────────────────────────────────


def test_chinese_numerals_cover_teens_and_liang() -> None:
    assert _parse("来三张初音图").count == 3
    assert _parse("来两张初音图").count == 2
    assert _parse("来俩张初音图").count == 2
    assert _parse("来十二张初音图").count == 12
    assert _parse("来十八张初音图").count == 18
    assert _parse("来二十张初音图").count == 20


def test_arabic_numerals_and_default() -> None:
    assert _parse("来5张初音图").count == 5
    assert _parse("来张图").count == 1
    assert _parse("来几张图").count == 1


# ── 方向 ─────────────────────────────────────────────────────


def test_orientation_recognized_with_and_without_space() -> None:
    assert _parse("来张初音图 竖图").orientation == ORIENTATION_PORTRAIT
    assert _parse("来张初音竖图").orientation == ORIENTATION_PORTRAIT
    assert _parse("来张初音图 横图").orientation == ORIENTATION_LANDSCAPE
    assert _parse("来张初音图").orientation == ORIENTATION_ANY


def test_orientation_is_not_part_of_tag() -> None:
    assert _parse("来张初音图 竖图").tag == "初音"
    assert _parse("来张初音竖图").tag == "初音"


# ── 标签清洗 ──────────────────────────────────────────────────


def test_trailing_de_is_stripped_from_tag() -> None:
    """「大肥鱼的图」里的「的」是语气助词，不该进标签。"""
    assert _parse("来张大肥鱼的图").tag == "大肥鱼"


def test_decoration_words_are_not_tags() -> None:
    assert _parse("来份福利图").tag == ""
    assert _parse("来张涩图").tag == ""


def test_lead_in_words_are_stripped() -> None:
    assert _parse("给我来张图").tag == ""
    assert _parse("请来三张风景图").tag == "风景"


# ── 作品 ID ───────────────────────────────────────────────────


def test_illust_id_from_hash_and_bare_digits() -> None:
    assert _parse("来张#12345678").illust_id == "12345678"
    assert _parse("来张 12345678").illust_id == "12345678"
    assert _parse("来张#12345678").tag == ""


# ── 负例 ─────────────────────────────────────────────────────


def test_non_request_sentences_do_not_match() -> None:
    assert not _parse("今天天气不错").matched
    assert not _parse("这张图真好看").matched
    assert not _parse("签到").matched
    assert not _parse("").matched


def test_past_tense_sentence_is_not_a_request() -> None:
    assert not _parse("我昨天发了一张图").matched
    assert looks_like_past_tense("我昨天发了一张图")
    assert not looks_like_past_tense("来张图")


def test_question_sentences_are_detected() -> None:
    assert looks_like_question("这张图的画师是谁？")
    assert looks_like_question("这个怎么用")
    assert not looks_like_question("来张图")


# ── 入口闸门 ──────────────────────────────────────────────────


def test_gate_admits_requests_and_rejects_chatter() -> None:
    for text in (
        "来张图",
        "大肥鱼来张图",
        "来三张初音图",
        "能不能帮我找点蓝色系的图",
        "来张#12345678",
    ):
        assert re.match(AUTO_TRIGGER_PATTERN, text), text

    for text in ("今天天气不错", "签到", "这张图真好看", "整点好看的"):
        assert not re.match(AUTO_TRIGGER_PATTERN, text), text


def test_gate_rejects_overlong_messages() -> None:
    long_text = "来张图" + "很长的前缀" * 20
    assert not re.match(AUTO_TRIGGER_PATTERN, long_text)


# ── 别名汇总 ──────────────────────────────────────────────────


def test_resolve_aliases_merges_bot_name_and_extra() -> None:
    assert resolve_aliases("大肥鱼", "肥鱼, 小鱼") == ["大肥鱼", "肥鱼", "小鱼"]


def test_resolve_aliases_dedupes_and_skips_empty() -> None:
    assert resolve_aliases("", "") == []
    assert resolve_aliases("neko", "neko,,NEKO") == ["neko"]
