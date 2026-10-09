"""Tests for `discover.py`: one search page read into a candidate list.

Nothing here opens a note — that is `download.py`'s half, tested in
`test_download.py`. What is tested here is the reading of the results page: the
filters are applied by pressing controls and verified against the page's own
`active` marker, and the cards are read into what a search page can honestly
say (which note, where its detail page is, its title, its date).
"""

from __future__ import annotations

import json

import pytest
import runtime

import discover
from fake_site import (  # noqa: F401
    CHANNELS,
    EXPLORE_PATH,
    HOME_URL,
    NOTE_A,
    NOTE_B,
    PANEL,
    SEARCH_PATH,
    FakeSite,
    data_args,
    run_cli,
    site,
)
from test_playbook import patch_browser


# --- parsing ---------------------------------------------------------------


def test_filters_split_into_dimension_and_option() -> None:
    assert discover.parse_filter_specs("排序依据=最新;半年内") == [
        {"dimension": "排序依据", "option": "最新"},
        {"dimension": None, "option": "半年内"},
    ]
    # Only the first `=` splits, so an option may contain one.
    assert discover.parse_filter_specs("排序依据=a=b")[0]["option"] == "a=b"
    assert discover.parse_filter_specs(None) == []


def test_an_empty_filter_option_is_refused() -> None:
    with pytest.raises(ValueError, match="选项为空"):
        discover.parse_filter_specs("排序依据=")


def test_one_line_collapses_whitespace_and_marks_a_cut() -> None:
    assert runtime.one_line("川西\n 秋色\t") == "川西 秋色"
    assert runtime.one_line("abcdef", 4) == "abc…"
    assert runtime.one_line(None) == ""


# --- reading the panel -----------------------------------------------------


def test_the_panel_reads_into_named_groups_with_one_entry_per_option(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = discover.panel_groups(site.snapshot(), config)

    assert [group["name"] for group in groups] == list(PANEL)
    assert [option["text"] for option in groups[0]["options"]] == PANEL["排序依据"]
    # The site renders every option twice; the reader must see it once.
    assert len(groups[0]["options"]) == 5
    assert groups[0]["options"][0]["active"] is True


def test_the_channel_row_is_read_separately_from_the_panel(site) -> None:
    config = runtime.load_locators()
    channels = discover.channel_options(site.snapshot(), config)

    assert [option["text"] for option in channels] == CHANNELS
    assert [option["text"] for option in channels if option["active"]] == ["全部"]


def test_an_option_is_resolved_by_its_visible_text(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = discover.panel_groups(site.snapshot(), config)

    assert discover.resolve_option(groups, {"dimension": "排序依据", "option": "最新"})["ref"] == (
        "opt:排序依据:最新"
    )
    # A dimension-less option that is unique across the panel resolves too.
    assert discover.resolve_option(groups, {"dimension": None, "option": "半年内"})["dimension"] == (
        "发布时间"
    )


def test_an_ambiguous_bare_option_is_refused_rather_than_guessed(site) -> None:
    """`不限` is a real option of four different groups."""
    config = runtime.load_locators()
    site.panel_open = True
    groups = discover.panel_groups(site.snapshot(), config)

    with pytest.raises(ValueError, match="都有，必须写成"):
        discover.resolve_option(groups, {"dimension": None, "option": "不限"})


def test_an_option_nobody_anticipated_is_still_selectable(site) -> None:
    """The point of reading the page: no vocabulary file to fall out of date."""
    config = runtime.load_locators()
    site.panel_open = True
    groups = discover.panel_groups(site.snapshot(), config)
    groups[0]["options"].append({"text": "刚刚上线的新排序", "active": False, "ref": "opt:new"})

    assert discover.resolve_option(
        groups, {"dimension": "排序依据", "option": "刚刚上线的新排序"}
    )["ref"] == "opt:new"


def test_an_unknown_dimension_lists_the_real_ones(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = discover.panel_groups(site.snapshot(), config)

    with pytest.raises(ValueError, match="排序依据、笔记类型、发布时间、搜索范围"):
        discover.resolve_option(groups, {"dimension": "点赞数", "option": "最新"})


def test_an_unknown_option_lists_the_real_ones(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = discover.panel_groups(site.snapshot(), config)

    with pytest.raises(ValueError, match="综合/最新/最多点赞/最多收藏/最多评论"):
        discover.resolve_option(groups, {"dimension": "排序依据", "option": "最热"})


# --- driving the filters ---------------------------------------------------


def test_filters_are_applied_by_pressing_the_controls(monkeypatch, site) -> None:
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()

    applied = discover.apply_filters(1, config, discover.parse_filter_specs("排序依据=最新;半年内"))

    assert [item["via"] for item in applied] == ["panel", "panel"]
    assert site.active["排序依据"] == "最新"
    assert site.active["发布时间"] == "半年内"


def test_a_note_type_filter_uses_the_channel_row_and_opens_no_panel(monkeypatch, site) -> None:
    """The channel row is always visible, so the cheapest route wins."""
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()

    applied = discover.apply_filters(1, config, discover.parse_filter_specs("笔记类型=图文"))

    assert applied == [{"dimension": "笔记类型", "option": "图文", "via": "channel"}]
    assert site.active["笔记类型"] == "图文"
    assert not site.panel_open
    assert "page.click" in site.methods() and site.methods().count("page.click") == 1


def test_a_filter_that_did_not_register_is_an_error(monkeypatch, site) -> None:
    """Silently returning an unfiltered page is the failure this guards against.

    The obvious check — did the result cards change — would not catch it, because
    a filter the results already satisfy leaves them identical. A press that lands
    nowhere is retried first, so the error only comes once every attempt failed.
    """
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()
    # The panel still opens; only the option presses land nowhere. (Blocking the
    # 筛选 press too would test a different failure — a panel that never opened.)
    real_click = site.click
    pressed: list[str] = []

    def click(ref: str) -> None:
        if ref.startswith(("opt:", "opt2:")):
            pressed.append(ref)
            return
        real_click(ref)

    monkeypatch.setattr(site, "click", click)

    with pytest.raises(RuntimeError, match="都没把它标成生效项"):
        discover.apply_filters(1, config, discover.parse_filter_specs("半年内"))

    assert len(pressed) == discover.FILTER_PRESS_ATTEMPTS


def test_the_panel_is_read_from_a_snapshot_wide_enough_to_hold_it(monkeypatch, site) -> None:
    """Measured 2026-10-07 on a loaded results page: `matched=711` against the
    500-element default, and the cut fell inside the 发布时间 group — the panel
    looked like it offered only 不限/一天内 and `半年内` reported as nonexistent
    while it was on screen."""
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()
    site.calls.clear()

    discover.apply_filters(1, config, discover.parse_filter_specs("半年内"))

    limits = [
        call[call.index("--limit") + 1]
        for call in site.calls
        if call[:2] == ("page", "snapshot") and "--limit" in call
    ]
    assert limits
    assert all(int(value) >= 1000 for value in limits)


def test_a_press_that_missed_once_is_retried_not_failed(monkeypatch, site) -> None:
    """Measured 2026-10-07: the same press applied on a settled page and did
    nothing while the results were still streaming in — the panel re-renders and
    the ref read a moment earlier no longer points at the option."""
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()
    real_click = site.click
    missed = []

    def click(ref: str) -> None:
        if ref.startswith(("opt:", "opt2:")) and not missed:
            missed.append(ref)
            return
        real_click(ref)

    monkeypatch.setattr(site, "click", click)

    applied = discover.apply_filters(1, config, discover.parse_filter_specs("半年内"))

    assert [item["option"] for item in applied] == ["半年内"]
    assert missed


def test_the_panel_is_not_toggled_closed_when_it_is_already_open(monkeypatch, site) -> None:
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()
    site.panel_open = True

    discover.open_panel(1, config)

    assert site.panel_open
    assert "page.click" not in site.methods()


# --- listing the options ---------------------------------------------------


def test_list_filters_prints_the_page_options(monkeypatch, site, capsys) -> None:
    assert run_cli(monkeypatch, site, ["--list-filters", "--tab-id", "1"]) == 0

    out = capsys.readouterr().out
    assert "排序依据: 综合* | 最新 | 最多点赞 | 最多收藏 | 最多评论" in out
    assert "发布时间: 不限* | 一天内 | 一周内 | 半年内" in out
    assert "笔记类型(常驻行): 全部* | 图文 | 视频 | 用户" in out
    # The one thing the caller must not assume is that a URL can carry these.
    assert "sort=/noteType= 一律无效" in out


def test_list_filters_fails_loudly_when_the_panel_has_no_options(monkeypatch, site) -> None:
    monkeypatch.setattr(discover.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    site.panel_open = True
    config = runtime.load_locators()
    monkeypatch.setattr(discover, "panel_groups", lambda page, config: [])

    with pytest.raises(RuntimeError, match="一个选项也没读到"):
        discover.list_filters(1, config, timeout=1.0)


# --- reading the cards ------------------------------------------------------


def test_the_cards_carry_a_rank_a_title_and_a_locator(site) -> None:
    config = runtime.load_locators()
    site.navigate(discover.search_page_url("川西秋色"))
    found = discover.cards(site.snapshot(), config, 10)

    assert [item["rank"] for item in found] == [1, 2]
    assert [item["noteId"] for item in found] == [NOTE_A, "6abfda1f000000001803b413"]
    assert found[0]["title"] == "川西赏秋时间表"
    assert found[0]["href"].startswith("https://www.xiaohongshu.com/search_result/")
    assert found[0]["hasAccessContext"] is True


def test_a_card_carries_no_note_internals(site) -> None:
    """A search page cannot say when a note was edited or how many likes it has.

    Claiming otherwise would mean opening the note, which is the thing this half
    of the tool exists not to do.
    """
    config = runtime.load_locators()
    site.navigate(discover.search_page_url("川西秋色"))
    found = discover.cards(site.snapshot(), config, 10)

    for key in ("updatedAt", "stats", "type", "body", "author"):
        assert key not in found[0]


def test_the_limit_keeps_the_first_n_cards(site) -> None:
    config = runtime.load_locators()
    site.navigate(discover.search_page_url("川西秋色"))

    assert len(discover.cards(site.snapshot(), config, 1)) == 1


# --- the whole run ---------------------------------------------------------


def test_a_search_reads_the_results_page_it_navigated_to(monkeypatch, site, tmp_path, capsys) -> None:
    assert run_cli(monkeypatch, site, [
        "--tab-id", "1", "--keyword", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [item["noteId"] for item in out["candidates"]] == [NOTE_A, NOTE_B]
    assert out["source"]["url"].startswith("https://www.xiaohongshu.com" + SEARCH_PATH)
    assert out["summary"] == {"candidates": 2, "inLibrary": 0, "new": 2}


def test_a_search_does_not_read_the_page_it_is_leaving(monkeypatch, site, tmp_path, capsys) -> None:
    """Measured 2026-10-09 on a real tab left on the explore home feed.

    `tabs navigate` is asynchronous: the snapshot taken right after it still
    shows the page being left, and the home feed is card-for-card the same markup
    as a results page (`section.note-item`, 30 matches). Waiting on the card rule
    alone therefore "found" the old page instantly, and because its links point at
    `/explore/<id>` rather than `/search_result/<id>` the read produced a clean,
    empty candidate list for a search that never ran.
    """
    site.url = HOME_URL
    site.navigate_delay = 1

    assert run_cli(monkeypatch, site, [
        "--tab-id", "1", "--keyword", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert [item["noteId"] for item in out["candidates"]] == [NOTE_A, NOTE_B]


def test_a_second_search_does_not_read_the_first_ones_results(monkeypatch, site, tmp_path, capsys) -> None:
    """The likelier version of the same race: the tab is normally left on the
    previous search, whose cards match every rule a results page has — including
    the `/search_result/` ones. Only the page's own keyword tells them apart.
    """
    site.navigate(discover.search_page_url("稻城亚丁 秋"))
    site.navigate_delay = 1

    assert run_cli(monkeypatch, site, [
        "--tab-id", "1", "--keyword", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = json.loads(capsys.readouterr().out)
    assert out["keyword"] == "川西秋色"
    assert [item["noteId"] for item in out["candidates"]] == [NOTE_A, NOTE_B]


