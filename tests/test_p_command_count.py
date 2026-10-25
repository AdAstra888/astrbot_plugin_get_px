"""Regression tests for /p [tag] [count] parameter parsing."""

import inspect
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.pixiv.query import (
    ACTION_FAVORITE_ADD,
    ACTION_FAVORITE_LIST,
    ACTION_FAVORITE_REMOVE,
    ACTION_SEARCH,
    parse_p_query,
)


def _cmd_p_filter() -> CommandFilter:
    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent
    ):
        if "astrbot_plugin_get_px" not in str(handler.handler_module_path):
            continue
        if handler.handler_name != "cmd_p":
            continue
        for event_filter in handler.event_filters:
            if isinstance(event_filter, CommandFilter):
                return event_filter
    raise AssertionError("cmd_p CommandFilter not registered")


def test_cmd_p_registers_greedy_str_query() -> None:
    """AstrBot only treats a param as greedy when default/annotation is GreedyStr itself."""
    cmd_filter = _cmd_p_filter()
    assert "query" in cmd_filter.handler_params
    assert cmd_filter.handler_params["query"] is GreedyStr
    assert inspect.signature(GetPxPlugin.cmd_p).parameters["query"].default is GreedyStr


def test_cmd_p_greedy_str_keeps_trailing_count() -> None:
    """/p 标签 3 must keep both tokens so parse_p_query can read the count."""
    cmd_filter = _cmd_p_filter()
    params = cmd_filter.validate_and_convert_params(
        ["初音ミク", "3"], cmd_filter.handler_params
    )
    assert params["query"] == "初音ミク 3"

    empty = cmd_filter.validate_and_convert_params([], cmd_filter.handler_params)
    assert empty["query"] == ""

    only_count = cmd_filter.validate_and_convert_params(
        ["2"], cmd_filter.handler_params
    )
    assert only_count["query"] == "2"


def test_parse_p_query_extracts_trailing_number() -> None:
    assert parse_p_query("").tag == ""
    assert parse_p_query("").count == ""

    assert parse_p_query("3").tag == ""
    assert parse_p_query("3").count == "3"

    parsed = parse_p_query("初音ミク 3")
    assert (parsed.action, parsed.tag, parsed.count) == (ACTION_SEARCH, "初音ミク", "3")

    spaced = parse_p_query("初音 ミク 3")
    assert (spaced.tag, spaced.count) == ("初音 ミク", "3")

    no_count = parse_p_query("初音ミク")
    assert (no_count.tag, no_count.count) == ("初音ミク", "")

    # non-separated trailing digit is part of the tag, not count
    assert parse_p_query("初音3").tag == "初音3"
    assert parse_p_query("初音3").count == ""


def test_parse_p_query_recognizes_orientation_and_ids() -> None:
    landscape = parse_p_query("初音ミク 3 横图")
    assert (landscape.tag, landscape.count, landscape.orientation) == (
        "初音ミク",
        "3",
        "landscape",
    )
    assert landscape.has_orientation is True

    leading = parse_p_query("竖图 初音ミク")
    assert (leading.tag, leading.orientation) == ("初音ミク", "portrait")

    by_id = parse_p_query("#12345678")
    assert by_id.illust_id == "12345678"
    assert by_id.tag == ""

    url_id = parse_p_query("https://www.pixiv.net/artworks/98765432")
    assert url_id.illust_id == "98765432"


def test_parse_p_query_routes_favorite_actions() -> None:
    assert parse_p_query("收藏").action == ACTION_FAVORITE_ADD
    assert parse_p_query("收藏 #12345678").illust_id == "12345678"
    assert parse_p_query("收藏列表 20").action == ACTION_FAVORITE_LIST
    assert parse_p_query("收藏列表 20").count == "20"
    assert parse_p_query("取消收藏 #12345678").action == ACTION_FAVORITE_REMOVE
    assert parse_p_query("取消收藏 #12345678").illust_id == "12345678"
