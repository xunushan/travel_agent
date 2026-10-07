"""Tests for phase one, driven against a fake site that speaks only the CLI.

Everything phase one does to a page it does through `chrome-agent`, so the fake
here is a `chrome-agent` stand-in rather than a mocked module: it answers
`tabs navigate`, `page snapshot`, `page click`, `page text`, `page images` and
`page download-images` from a small model of the site, and it models the two
behaviours the real one was measured to have that shape the design —

* the filter panel is opened by pressing 筛选 and options register by class
  (`active`), never by URL, so `page click` here really does move the state the
  next `page snapshot` reports;
* the search results page is client-rendered, so a snapshot taken before the
  cards mount has none.

The tests that matter most are the two the whole design exists for: a note
already in the index is not opened a second time, and a filter that did not
register is an error rather than a silently unfiltered result page.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import pytest
import runtime
import search
from test_playbook import element, patch_browser

NOTE_A = "6aba9036000000001c00d596"
NOTE_B = "6abfda1f000000001803b413"
NOTE_C = "6ac5fbbf0000000018007118"

IMAGE_A = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/1040g3aaa.webp"
IMAGE_B = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/1040g3bbb.webp"

# What the real page offers, measured 2026-10-07 (references/filters.md).
PANEL = {
    "排序依据": ["综合", "最新", "最多点赞", "最多收藏", "最多评论"],
    "笔记类型": ["不限", "视频", "图文"],
    "发布时间": ["不限", "一天内", "一周内", "半年内"],
    "搜索范围": ["不限", "已看过", "未看过", "已关注"],
}
CHANNELS = ["全部", "图文", "视频", "用户"]
DEFAULTS = {"排序依据": "综合", "笔记类型": "不限", "发布时间": "不限", "搜索范围": "不限"}

SEARCH_PATH = "/search_result"


def rect(x: float, y: float, width: float, height: float) -> dict:
    return {"x": x, "y": y, "width": width, "height": height}


def state_blob(note: str, *, title: str, body: str, updated_ms: int, published_ms: int) -> str:
    return (
        "window.__INITIAL_STATE__="
        + json.dumps(
            {"note": {"noteDetailMap": {note: {"note": {
                "noteId": note, "title": title, "desc": body, "type": "normal",
                "time": published_ms, "lastUpdateTime": updated_ms,
                "interactInfo": {"likedCount": "91", "collectedCount": "32",
                                 "commentCount": "18", "shareCount": "0", "niceCount": ""},
                "tagList": [{"id": "t", "name": "川西", "type": "topic"}],
                "imageList": [{"url": "", "urlDefault": IMAGE_A, "width": "1440", "height": "2400"},
                              {"url": "", "urlDefault": IMAGE_B, "width": "1440", "height": "2400"}],
                "user": {"userId": "u1", "nickname": "冷三岁"}}}}}},
            ensure_ascii=False,
        )
        + "};window.__SSR__=true"
    )


def note_page(note: str, *, title: str, body: str, updated_ms: int = 1785590297000,
              published_ms: int = 1785590297000) -> tuple[dict, dict]:
    """A note detail page and the per-ref texts the CLI would return for it."""
    page = {
        "url": f"https://www.xiaohongshu.com/explore/{note}?xsec_token=tok",
        "elements": [
            element("container", className="note-container", rect=rect(0, 0, 600, 740)),
            element("slider", className="xhs-slider-container", rect=rect(0, 0, 600, 500)),
            element("body", className="note-text", text=body, rect=rect(0, 500, 600, 200)),
            element("h1", tag="h1", className="title", text=title, rect=rect(0, 480, 600, 20)),
            # The overlay's close control. It exists so a phase-two run, which
            # closes whatever note is open before opening the next one, can get
            # back to the results.
            element("close", tag="button", className="close-icon", rect=rect(560, 8, 32, 32)),
            element("footer", className="bottom-container", rect=rect(0, 700, 600, 40)),
            element("date", className="date", text="09-27 四川", rect=rect(0, 700, 200, 19)),
            element("script", tag="script", className="None",
                    text="window.__INITIAL_STATE__={", rect=rect(0, 0, 0, 0)),
        ],
    }
    texts = {
        "body": body,
        "script": state_blob(note, title=title, body=body, updated_ms=updated_ms,
                            published_ms=published_ms),
    }
    return page, texts


def card_elements(index: int, note: str, title: str, time_text: str) -> list[dict]:
    href = (
        f"https://www.xiaohongshu.com{SEARCH_PATH}/{note}"
        f"?xsec_token=tok&xsec_source=pc_search"
    )
    y = 400 + index * 320
    return [
        element(f"card{index}", tag="section", className="note-item", text=f"{title} 冷三岁 {time_text} 91",
                rect=rect(960, y, 230, 300)),
        element(f"cover{index}", className="cover mask ld", href=href, rect=rect(960, y, 230, 300)),
        element(f"title{index}", className="title", href=href, text=title, rect=rect(960, y + 260, 230, 20)),
        element(f"time{index}", className="time", text=time_text, rect=rect(1080, y + 270, 60, 19)),
    ]


def channel_elements(state: str) -> list[dict]:
    # The row spells "no filter" as 全部 while the panel spells it 不限, and the
    # two really are one state — a fake that did not know that would report no
    # active channel at all.
    current = "全部" if state in ("全部", "不限") else state
    return [
        element(f"chan:{name}", className=f"channel{' active' if name == current else ''}",
                text=name, rect=rect(260 + index * 64, 88, 60, 40))
        for index, name in enumerate(CHANNELS)
    ]


def panel_elements(active: dict[str, str]) -> list[dict]:
    elements = [element("panel", className="filter-panel", rect=rect(900, 100, 620, 560))]
    for row, (name, options) in enumerate(PANEL.items()):
        top = 120 + row * 90
        # Every option must sit inside its group and tag container: the reader
        # scopes by rectangle, so a container narrower than its own row of
        # options silently loses the right-hand ones.
        elements.append(element(f"group:{name}", className="filters", text=name,
                                rect=rect(920, top, 560, 70)))
        elements.append(element(f"label:{name}", tag="span", text=name,
                                rect=rect(920, top, 200, 19)))
        elements.append(element(f"tcon:{name}", className="tag-container", rect=rect(920, top + 25, 560, 40)))
        for index, option in enumerate(options):
            is_active = active.get(name) == option
            elements.append(
                element(f"opt:{name}:{option}", className=f"tags{' active' if is_active else ''}",
                        text=option, rect=rect(920 + index * 100, top + 25, 96, 40))
            )
            # The site renders every option twice at the same rectangle.
            elements.append(
                element(f"opt2:{name}:{option}", className=f"tags{' active' if is_active else ''}",
                        text=option, rect=rect(920 + index * 100, top + 25, 96, 40))
            )
    return elements


class FakeSite:
    """A chrome-agent stand-in with just enough of the site's behaviour."""

    def __init__(self, *, keywords: dict[str, list[tuple[str, str, str]]],
                 notes: dict[str, tuple[dict, dict]], tmp_path: Path) -> None:
        self.keyword_cards = keywords          # keyword -> [(note, title, time text)]
        self.notes = notes                     # note -> (page, texts)
        self.tmp = tmp_path
        self.url = ""
        self.search_url = ""
        self.keyword = ""
        self.tabs_seen: set[int] = set()
        self.panel_open = False
        self.active = dict(DEFAULTS)
        self.images: list[str] = [IMAGE_A, IMAGE_B]
        self.calls: list[tuple] = []
        self.download_count = 0

    # --- the CLI surface ---

    def __call__(self, *args: str) -> dict:
        self.calls.append(args)
        self.note_tab(args)
        verb = args[:2]
        if verb == ("tabs", "navigate"):
            # (`tabs navigate <tab-id> <url>` — the URL is the fourth word, not
            # the third; passing the tab id here is what made every search look
            # empty and spin out the render timeout.)
            self.navigate(args[3])
            return {"navigated": True}
        if verb == ("tabs", "list"):
            # `collect.py` records the tab a collection ran against, so a fake
            # that answers nothing here fails the write of downloads.json.
            return {"tabs": [
                {"id": tab, "url": self.url, "title": "川西秋色 - 小红书"}
                for tab in sorted(self.tabs_seen or {1})
            ]}
        if verb == ("page", "snapshot"):
            return self.snapshot()
        if verb == ("page", "click"):
            self.click(args[args.index("--ref") + 1])
            return {"success": True, "clicked": True}
        if verb == ("page", "text"):
            return self.text(args[args.index("--ref") + 1])
        if verb == ("page", "images"):
            return {"images": [
                {"src": url, "width": 1440, "height": 2400} for url in self.images
            ]}
        if verb == ("page", "download-images"):
            url = args[args.index("--url") + 1]
            return {"downloads": [{"url": url, "state": "complete",
                                   "filename": str(self.download(url))}]}
        return {"success": True}

    def methods(self) -> list[str]:
        return [args[0] + "." + args[1] for args in self.calls]

    def note_tab(self, args: tuple) -> None:
        """Remember which tab the caller is driving, for `tabs list`."""
        if "--tab-id" in args:
            self.tabs_seen.add(int(args[args.index("--tab-id") + 1]))
        elif args[0] == "tabs" and len(args) > 2 and args[1] != "list":
            self.tabs_seen.add(int(args[2]))

    # --- the site ---

    def navigate(self, url: str) -> None:
        self.url = url
        if SEARCH_PATH + "?" in url:
            self.search_url = url
            self.keyword = parse_qs(urlparse(url).query).get("keyword", [""])[0]
            # A fresh search starts from the site's defaults.
            self.panel_open, self.active = False, dict(DEFAULTS)

    def click(self, ref: str) -> None:
        if ref == "close":
            # Closing the note overlay puts the results page back on screen —
            # which is what a batch run does between two notes.
            self.url = self.search_url
        elif ref == "filter-btn":
            self.panel_open = True
        elif ref.startswith("chan:"):
            self.active["笔记类型"] = ref.split(":", 1)[1]
        elif ref.startswith("opt:") or ref.startswith("opt2:"):
            _, dimension, option = ref.split(":", 2)
            self.active[dimension] = option

    def snapshot(self) -> dict:
        note = runtime.note_id(self.url)
        if note and note in self.notes:
            return self.notes[note][0]
        elements = channel_elements(self.active["笔记类型"])
        elements.append(element("filter-btn", className="filter", text="筛选",
                                rect=rect(1324, 88, 84, 40)))
        if self.panel_open:
            elements.extend(panel_elements(self.active))
        for index, (note_id, title, time_text) in enumerate(
            self.keyword_cards.get(self.keyword, [])
        ):
            elements.extend(card_elements(index, note_id, title, time_text))
        return {"url": self.url, "elements": elements}

    def text(self, ref: str) -> dict:
        note = runtime.note_id(self.url)
        texts = self.notes.get(note, ({}, {}))[1] if note else {}
        value = texts.get(ref, "")
        return {"text": value, "length": len(value), "truncated": False}

    def download(self, url: str) -> Path:
        self.download_count += 1
        path = self.tmp / f"download-{self.download_count}.webp"
        path.write_bytes(b"image")
        return path


@pytest.fixture
def site(tmp_path) -> FakeSite:
    return FakeSite(
        keywords={
            "川西秋色": [(NOTE_A, "川西赏秋时间表", "09-27"),
                        (NOTE_B, "川西晚秋｜雪山彩林", "4天前")],
            "稻城亚丁 秋": [(NOTE_B, "川西晚秋｜雪山彩林", "4天前"),
                          (NOTE_C, "稻城亚丁怎么走", "10-01")],
        },
        notes={
            NOTE_A: note_page(NOTE_A, title="川西赏秋时间表", body="10月中下旬开始，川西进入最佳观赏期。"),
            NOTE_B: note_page(NOTE_B, title="川西晚秋｜雪山彩林", body="彩林最好的时候是十月中。",
                              updated_ms=1785590297000, published_ms=1784000000000),
            NOTE_C: note_page(NOTE_C, title="稻城亚丁怎么走", body="从成都出发走雅康高速。"),
        },
        tmp_path=tmp_path,
    )


def run_cli(monkeypatch, site, argv: list[str]) -> int:
    patch_browser(monkeypatch, site)
    monkeypatch.setattr("sys.argv", ["search.py", *argv])
    return search.main()


def data_args(tmp_path, name: str = "xhs-e2e") -> list[str]:
    return ["--data-root", str(tmp_path / name)]


def database_of(tmp_path, name: str = "xhs-e2e") -> Path:
    return runtime.db_path(runtime.data_root(str(tmp_path / name)))


# --- parsing ---------------------------------------------------------------


def test_keywords_are_one_semicolon_separated_flag() -> None:
    assert search.split_items("川西秋色;稻城亚丁 秋") == ["川西秋色", "稻城亚丁 秋"]
    assert search.split_items(" 川西秋色 ;; ") == ["川西秋色"]
    assert search.split_items(None) == []
    # A comma is part of a keyword, never a separator.
    assert search.split_items("喀纳斯,禾木 怎么走") == ["喀纳斯,禾木 怎么走"]


def test_filters_split_into_dimension_and_option() -> None:
    assert search.parse_filter_specs("排序依据=最新;半年内") == [
        {"dimension": "排序依据", "option": "最新"},
        {"dimension": None, "option": "半年内"},
    ]
    # Only the first `=` splits, so an option may contain one.
    assert search.parse_filter_specs("排序依据=a=b")[0]["option"] == "a=b"
    assert search.parse_filter_specs(None) == []


def test_an_empty_filter_option_is_refused() -> None:
    with pytest.raises(ValueError, match="选项为空"):
        search.parse_filter_specs("排序依据=")


def test_one_line_collapses_whitespace_and_marks_a_cut() -> None:
    assert search.one_line("川西\n 秋色\t") == "川西 秋色"
    assert search.one_line("abcdef", 4) == "abc…"
    assert search.one_line(None) == ""


# --- reading a card --------------------------------------------------------


def card_page(elements: list[dict]) -> dict:
    return {"url": f"https://www.xiaohongshu.com{SEARCH_PATH}?keyword=甘南环线",
            "elements": elements}


def test_a_time_is_not_printed_when_the_page_gives_every_card_one_rect() -> None:
    """The 最新 sort reports one origin for all of its cards (measured 2026-10-07).

    All twelve digest rows then printed the same time, and it was a real card's
    time that simply advanced 42分钟前 -> 44分钟前 between runs. The card that
    "contains" each candidate's link is arbitrary in that state too, so there is
    nothing to fall back on: the answer is no answer, and the table shows "?".
    """
    config = runtime.load_locators()
    page = card_page([
        element("card0", tag="section", className="note-item",
                text="甘南小环线超全攻略 皮皮日记 3分钟前 12", rect=rect(267, 217, 230, 300)),
        element("title0", className="title", text="甘南小环线超全攻略", rect=rect(267, 460, 230, 20)),
        element("time0", className="time", text="3分钟前", rect=rect(267, 480, 60, 19)),
        element("card1", tag="section", className="note-item",
                text="甘南环线第二站 小甲 2小时前 5", rect=rect(267, 217, 230, 340)),
        element("title1", className="title", text="甘南环线第二站", rect=rect(267, 490, 230, 20)),
        element("time1", className="time", text="2小时前", rect=rect(267, 505, 60, 19)),
    ])
    assert search.card_time(page, config, {"ref": "title0"}) is None
    assert search.card_time(page, config, {"ref": "title1"}) is None


def test_a_shared_time_slot_falls_back_to_the_cards_own_text() -> None:
    """Two neighbouring cards overlap, so a slot sits in both of them.

    The card's own text still carries its own time, and reading the LAST time
    shape in it (title, author, time, likes) is what keeps a title that happens
    to contain a date from being read as the time.
    """
    config = runtime.load_locators()
    page = card_page([
        element("card0", tag="section", className="note-item",
                text="甘南小环线超全攻略 皮皮日记 3分钟前 12", rect=rect(960, 400, 230, 300)),
        element("title0", className="title", text="甘南小环线超全攻略", rect=rect(960, 500, 230, 20)),
        element("time0", className="time", text="3分钟前", rect=rect(960, 680, 60, 19)),
        element("card1", tag="section", className="note-item",
                text="10-1甘南出发 小甲 2小时前 5", rect=rect(960, 600, 230, 300)),
        element("title1", className="title", text="10-1甘南出发", rect=rect(960, 700, 230, 20)),
        element("time1", className="time", text="2小时前", rect=rect(960, 880, 60, 19)),
    ])
    # `time0` is inside card1 as well (600..900 covers 680..699), so it cannot be
    # attributed — and the card's text says "3分钟前", not the title's "10-1".
    assert search.card_time(page, config, {"ref": "title0"}) == "3分钟前"
    assert search.card_time(page, config, {"ref": "title1"}) == "2小时前"


def test_each_card_keeps_its_own_time_when_the_rects_separate_them() -> None:
    config = runtime.load_locators()
    page = card_page([
        element("card0", tag="section", className="note-item",
                text="甘南小环线超全攻略 皮皮日记 3分钟前 12", rect=rect(960, 400, 230, 300)),
        element("title0", className="title", text="甘南小环线超全攻略", rect=rect(960, 600, 230, 20)),
        element("time0", className="time", text="3分钟前", rect=rect(1080, 660, 60, 19)),
        element("card1", tag="section", className="note-item",
                text="甘南环线第二站 小甲 2小时前 5", rect=rect(960, 760, 230, 300)),
        element("title1", className="title", text="甘南环线第二站", rect=rect(960, 960, 230, 20)),
        element("time1", className="time", text="2小时前", rect=rect(1080, 1020, 60, 19)),
    ])
    assert search.card_time(page, config, {"ref": "title0"}) == "3分钟前"
    assert search.card_time(page, config, {"ref": "title1"}) == "2小时前"


# --- reading the panel -----------------------------------------------------


def test_the_panel_reads_into_named_groups_with_one_entry_per_option(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = search.panel_groups(site.snapshot(), config)

    assert [group["name"] for group in groups] == list(PANEL)
    assert [option["text"] for option in groups[0]["options"]] == PANEL["排序依据"]
    # The site renders every option twice; the reader must see it once.
    assert len(groups[0]["options"]) == 5
    assert groups[0]["options"][0]["active"] is True


def test_the_channel_row_is_read_separately_from_the_panel(site) -> None:
    config = runtime.load_locators()
    channels = search.channel_options(site.snapshot(), config)

    assert [option["text"] for option in channels] == CHANNELS
    assert [option["text"] for option in channels if option["active"]] == ["全部"]


def test_an_option_is_resolved_by_its_visible_text(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = search.panel_groups(site.snapshot(), config)

    assert search.resolve_option(groups, {"dimension": "排序依据", "option": "最新"})["ref"] == (
        "opt:排序依据:最新"
    )
    # A dimension-less option that is unique across the panel resolves too.
    assert search.resolve_option(groups, {"dimension": None, "option": "半年内"})["dimension"] == (
        "发布时间"
    )


def test_an_ambiguous_bare_option_is_refused_rather_than_guessed(site) -> None:
    """`不限` is a real option of four different groups."""
    config = runtime.load_locators()
    site.panel_open = True
    groups = search.panel_groups(site.snapshot(), config)

    with pytest.raises(ValueError, match="都有，必须写成"):
        search.resolve_option(groups, {"dimension": None, "option": "不限"})


def test_an_option_nobody_anticipated_is_still_selectable(site) -> None:
    """The point of reading the page: no vocabulary file to fall out of date."""
    config = runtime.load_locators()
    site.panel_open = True
    groups = search.panel_groups(site.snapshot(), config)
    groups[0]["options"].append({"text": "刚刚上线的新排序", "active": False, "ref": "opt:new"})

    assert search.resolve_option(
        groups, {"dimension": "排序依据", "option": "刚刚上线的新排序"}
    )["ref"] == "opt:new"


def test_an_unknown_dimension_lists_the_real_ones(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = search.panel_groups(site.snapshot(), config)

    with pytest.raises(ValueError, match="排序依据、笔记类型、发布时间、搜索范围"):
        search.resolve_option(groups, {"dimension": "点赞数", "option": "最新"})


def test_an_unknown_option_lists_the_real_ones(site) -> None:
    config = runtime.load_locators()
    site.panel_open = True
    groups = search.panel_groups(site.snapshot(), config)

    with pytest.raises(ValueError, match="综合/最新/最多点赞/最多收藏/最多评论"):
        search.resolve_option(groups, {"dimension": "排序依据", "option": "最热"})


# --- driving the filters ---------------------------------------------------


def test_filters_are_applied_by_pressing_the_controls(monkeypatch, site) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()

    applied = search.apply_filters(1, config, search.parse_filter_specs("排序依据=最新;半年内"))

    assert [item["via"] for item in applied] == ["panel", "panel"]
    assert site.active["排序依据"] == "最新"
    assert site.active["发布时间"] == "半年内"


def test_a_note_type_filter_uses_the_channel_row_and_opens_no_panel(monkeypatch, site) -> None:
    """The channel row is always visible, so the cheapest route wins."""
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()

    applied = search.apply_filters(1, config, search.parse_filter_specs("笔记类型=图文"))

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
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
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
        search.apply_filters(1, config, search.parse_filter_specs("半年内"))

    assert len(pressed) == search.FILTER_PRESS_ATTEMPTS


def test_the_panel_is_read_from_a_snapshot_wide_enough_to_hold_it(monkeypatch, site) -> None:
    """Measured 2026-10-07 on a loaded results page: `matched=711` against the
    500-element default, and the cut fell inside the 发布时间 group — the panel
    looked like it offered only 不限/一天内 and `半年内` reported as nonexistent
    while it was on screen."""
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()
    site.calls.clear()

    search.apply_filters(1, config, search.parse_filter_specs("半年内"))

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
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
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

    applied = search.apply_filters(1, config, search.parse_filter_specs("半年内"))

    assert [item["option"] for item in applied] == ["半年内"]
    assert missed


def test_the_panel_is_not_toggled_closed_when_it_is_already_open(monkeypatch, site) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    config = runtime.load_locators()
    site.panel_open = True

    search.open_panel(1, config)

    assert site.panel_open
    assert "page.click" not in site.methods()


# --- listing the options ---------------------------------------------------


def test_filters_prints_the_page_options(monkeypatch, site, capsys) -> None:
    assert run_cli(monkeypatch, site, ["filters", "--tab-id", "1"]) == 0

    out = capsys.readouterr().out
    assert "排序依据: 综合* | 最新 | 最多点赞 | 最多收藏 | 最多评论" in out
    assert "发布时间: 不限* | 一天内 | 一周内 | 半年内" in out
    assert "笔记类型(常驻行): 全部* | 图文 | 视频 | 用户" in out
    # The one thing the caller must not assume is that a URL can carry these.
    assert "sort=/noteType= 一律无效" in out


def test_filters_fails_loudly_when_the_panel_has_no_options(monkeypatch, site) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    patch_browser(monkeypatch, site)
    site.panel_open = True
    config = runtime.load_locators()
    monkeypatch.setattr(search, "panel_groups", lambda page, config: [])

    with pytest.raises(RuntimeError, match="一个选项也没读到"):
        search.list_filters(1, config, timeout=1.0)


# --- a search run ----------------------------------------------------------


def test_no_screen_prints_cards_and_opens_no_note(monkeypatch, site, tmp_path, capsys) -> None:
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", "--no-screen",
        *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert f"{NOTE_A}  川西赏秋时间表" in out
    assert "09-27" in out          # the card's fuzzy time
    assert "正文与封面在" in out
    assert not any(call[:2] == ("tabs", "navigate") and "/search_result/" in call[2]
                   for call in site.calls)
    assert not (tmp_path / "xhs-e2e" / "notes").exists()


def test_a_run_screens_every_candidate_and_records_it(monkeypatch, site, tmp_path, capsys) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert f"{NOTE_A}  川西赏秋时间表" in out
    assert "10月中下旬开始" in out
    assert f"cover: {tmp_path}/xhs-e2e/notes/{NOTE_A}/images/cover.webp" in out

    # The body landed on disk, and only the body and the cover were fetched.
    note = json.loads((tmp_path / "xhs-e2e" / "notes" / NOTE_A / "note.json").read_text(
        encoding="utf-8"))
    assert note["content"] == "10月中下旬开始，川西进入最佳观赏期。"
    assert note["capturedFrom"] == "state"
    assert note["updatedAt"] is not None and note["tags"] == ["川西"]
    assert note["stats"]["shares"] == 0
    assert not (tmp_path / "xhs-e2e" / "notes" / NOTE_A / "comments.json").exists()

    # And in the index, as screened.
    with db.open_db(database_of(tmp_path)) as conn:
        row = db.note_row(conn, NOTE_A)
        assert row["status"] == db.STATUS_SCREENED
        assert row["likes"] == 91 and row["shares"] == 0
        assert Path(row["note_dir"]).name == NOTE_A
        # Every discovered picture is recorded, not only the one fetched, so a
        # later media run does not read the rest as newly added.
        assert db.stored_media_urls(conn, NOTE_A) == {
            runtime.media_name(IMAGE_A), runtime.media_name(IMAGE_B),
        }
        assert db.downloaded_urls(conn, NOTE_A) == {runtime.media_name(IMAGE_A)}
        items = db.run_items(conn, db.run_row(conn, 1)["run_id"])
        assert [item["note_id"] for item in items] == [NOTE_A, NOTE_B]
        assert all(item["decision"] == db.DECISION_PENDING for item in items)


def test_a_note_already_in_the_index_is_not_opened_again(
    monkeypatch, site, tmp_path, capsys
) -> None:
    """The cost that only gets paid once — this is what the index is for."""
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ])
    notes = tmp_path / "xhs-e2e" / "notes"
    first = {path.name: path.stat().st_mtime_ns for path in notes.iterdir()}
    # Screening opens a note by the card's own href, which is a `/search_result/`
    # URL carrying the token — so "a note page was opened" is not "/explore/".
    opened = [call for call in site.calls
              if call[:2] == ("tabs", "navigate") and runtime.note_id(call[3])]
    site.calls.clear()
    assert len(opened) == 2

    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "库里已有 2" in out
    assert "打开 0" in out
    assert "已筛过" not in out  # they were only seen, never judged
    # Neither note page was visited a second time.
    assert not [call for call in site.calls if call[:2] == ("tabs", "navigate")
                and "/explore/" in str(call[-1])]
    assert len(opened) == 2
    # And nothing was rewritten.
    assert {path.name: path.stat().st_mtime_ns for path in notes.iterdir()} == first


def test_a_judged_note_is_marked_as_such_in_the_next_run(monkeypatch, site, tmp_path, capsys) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ])
    with db.open_db(database_of(tmp_path)) as conn:
        db.decide(conn, 1, [NOTE_A], db.DECISION_KEEP, reason="有时间表")
        db.decide(conn, 1, [NOTE_B], db.DECISION_DROP, reason="只有风景照")

    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "已筛过:keep" in out
    assert "已筛过:drop" in out


def test_two_keywords_merge_into_one_table_without_duplicates(
    monkeypatch, site, tmp_path, capsys
) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色;稻城亚丁 秋", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "川西秋色;稻城亚丁 秋" in out
    # Three distinct notes from four card mentions.
    assert "3 候选" in out
    # One row, not two. (Counting bare mentions would also catch the note id
    # inside its own cover path, which appears for every row.)
    assert out.count(f"  {NOTE_B}  ") == 1

    with db.open_db(database_of(tmp_path)) as conn:
        items = db.run_items(conn, 1)
    assert [item["note_id"] for item in items] == [NOTE_A, NOTE_B, NOTE_C]
    assert [item["keyword"] for item in items] == ["川西秋色", "川西秋色", "稻城亚丁 秋"]


def test_screen_limit_caps_how_many_pages_get_opened(monkeypatch, site, tmp_path, capsys) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", "--screen-limit", "1",
        *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "打开 1" in out
    # The keyword that lost a candidate is named, and so is the count.
    assert "「川西秋色」2 候选 · 打开 1 · 未展开 1" in out
    assert "「川西秋色」到上限没打开 1 条" in out
    assert f"{NOTE_B}" in out          # still listed, just not opened


def test_each_keyword_gets_its_own_opening_budget(monkeypatch, tmp_path, capsys) -> None:
    """The loss this was written for, measured in run 2 of 2026-10-07.

    Two keywords, 26 candidates, `--screen-limit 15`: the first keyword spent the
    whole budget, the second keyword's 11 candidates were never opened, and one
    summary line reported "未展开 11 条" with no keyword attached. The budget is
    per keyword now, so a keyword is not cut short by the keyword before it.
    """
    site = FakeSite(
        keywords={
            "川西秋色": [(NOTE_A, "川西赏秋时间表", "09-27"), (NOTE_B, "川西环线", "09-20")],
            "稻城亚丁 秋": [(NOTE_C, "稻城亚丁", "09-18")],
        },
        notes={
            NOTE_A: note_page(NOTE_A, title="川西赏秋时间表", body="10月中下旬开始，川西进入最佳观赏期。"),
            NOTE_B: note_page(NOTE_B, title="川西环线", body="第一天成都出发。"),
            NOTE_C: note_page(NOTE_C, title="稻城亚丁", body="十月是稻城最好的季节。"),
        },
        tmp_path=tmp_path,
    )
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色;稻城亚丁 秋",
        "--screen-limit", "1", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "「川西秋色」2 候选 · 打开 1 · 未展开 1" in out
    # The second keyword opens its own candidate: 1 each, not 1 in total.
    assert "「稻城亚丁 秋」1 候选 · 打开 1" in out
    with db.open_db(database_of(tmp_path)) as conn:
        assert db.note_row(conn, NOTE_C)["status"] == db.STATUS_SCREENED


def test_a_thin_body_is_marked_so_its_cover_gets_read() -> None:
    """The signal the two-layer screen hangs on.

    Measured 2026-10-07: two of ten notes kept for a 甘南 route question carried
    19 and 50 characters of body — the route map was on the cover. A reader
    judging from the text alone would have dropped both, so the row says out
    loud that the text is not the whole note.
    """
    row = {"noteId": NOTE_A, "title": "甘南路线图", "content": "路线都在图里，自取",
           "status": db.STATUS_SCREENED, "coverPath": "/tmp/xhs/cover.webp"}

    assert "[短正文 9 字·看图]" in search.digest_row(1, row, excerpt=150)[0]


def test_a_short_body_with_no_cover_says_so() -> None:
    """No picture to read is a different fact from "there is one, go look"."""
    row = {"noteId": NOTE_A, "title": "甘南路线图", "content": "见图",
           "status": db.STATUS_SCREENED, "coverPath": None}

    assert "[短正文 2 字·无封面]" in search.digest_row(1, row, excerpt=150)[0]


def test_an_unopened_candidate_is_not_called_short() -> None:
    """A candidate nobody opened has no body to be short, and its cover was
    never downloaded — flagging it would send the reader to a missing file."""
    row = {"noteId": NOTE_B, "title": "甘南路线图", "content": "",
           "status": db.STATUS_SEEN, "coverPath": None}

    assert "短正文" not in search.digest_row(1, row, excerpt=150)[0]


def test_a_body_with_something_to_say_is_not_marked() -> None:
    row = {"noteId": NOTE_A, "title": "甘南路线", "status": db.STATUS_SCREENED,
           "content": "第一天兰州出发走夏河，第二天桑科草原转扎尕那，全程约 400 公里，"
                      "郎木寺到扎尕那段在修路，建议早上出发。住宿建议提前订，"
                      "旺季价格翻倍。加油点在碌曲和迭部之间比较稀，出县城前加满。"
                      "导航信号部分路段会断，建议提前离线地图。",
           "coverPath": "/tmp/xhs/cover.webp"}

    assert "短正文" not in search.digest_row(1, row, excerpt=150)[0]


def test_the_filters_used_are_reported(monkeypatch, site, tmp_path, capsys) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色",
        "--filters", "排序依据=最新;笔记类型=图文", "--no-screen", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "已施加筛选：排序依据=最新(panel)、笔记类型=图文(channel)" in out
    assert site.active["排序依据"] == "最新"
    assert site.active["笔记类型"] == "图文"


def test_a_candidate_that_will_not_open_is_reported_not_fatal(
    monkeypatch, site, tmp_path, capsys
) -> None:
    """One broken page must not discard the other nineteen."""
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    broken = dict(site.notes)
    broken.pop(NOTE_A)
    site.notes = broken
    monkeypatch.setattr("discover.NOTE_OPEN_TIMEOUT", 0.05)

    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ]) == 0

    out = capsys.readouterr().out
    assert "条抓取失败" in out
    assert f"{NOTE_A}:" in out
    assert f"{NOTE_B}  川西晚秋" in out             # the other one still screened
    with db.open_db(database_of(tmp_path)) as conn:
        assert db.note_row(conn, NOTE_A) is None     # nothing invented
        assert db.note_row(conn, NOTE_B) is not None


def test_an_unknown_run_id_is_refused(monkeypatch, site, tmp_path) -> None:
    with pytest.raises(SystemExit, match="不存在"):
        run_cli(monkeypatch, site, [
            "run", "--tab-id", "1", "--keywords", "川西秋色", "--run-id", "99",
            "--no-screen", *data_args(tmp_path),
        ])


def test_missing_keywords_are_refused(monkeypatch, site, tmp_path) -> None:
    with pytest.raises(SystemExit, match="--keywords"):
        run_cli(monkeypatch, site, ["run", "--tab-id", "1", "--no-screen", *data_args(tmp_path)])


def test_an_empty_result_page_says_why_instead_of_returning_nothing(
    monkeypatch, site, tmp_path
) -> None:
    """Client-rendered results mean "no cards yet" and "no results" look alike.

    The wait is shortened to a second so the test does not sit out the real 45s
    render timeout; `time.sleep` stays patched for the same reason.
    """
    site.keyword_cards = {"川西秋色": []}
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    fast = runtime.load_locators()
    fast["scroll"]["search"]["render_timeout"] = 0
    monkeypatch.setattr(search, "load_locators", lambda: fast)

    with pytest.raises(RuntimeError, match="仍没有搜索结果卡片"):
        run_cli(monkeypatch, site, [
            "run", "--tab-id", "1", "--keywords", "川西秋色", "--no-screen",
            *data_args(tmp_path),
        ])


def test_the_second_run_appends_to_the_first_runs_ledger(monkeypatch, site, tmp_path) -> None:
    monkeypatch.setattr(search.time, "sleep", lambda _seconds: None)
    run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "川西秋色", *data_args(tmp_path),
    ])
    assert run_cli(monkeypatch, site, [
        "run", "--tab-id", "1", "--keywords", "稻城亚丁 秋", "--run-id", "1", *data_args(tmp_path),
    ]) == 0

    with db.open_db(database_of(tmp_path)) as conn:
        assert db.run_row(conn, 2) is None          # no second run was created
        assert len(db.run_items(conn, 1)) == 3
