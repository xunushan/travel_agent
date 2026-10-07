"""Regression tests for configuration-driven Xiaohongshu Playbook rules."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import batch  # noqa: E402
import collect  # noqa: E402
import comments  # noqa: E402
import discover  # noqa: E402
import media  # noqa: E402
import migrate  # noqa: E402
import note  # noqa: E402
import runtime  # noqa: E402
from discover import close_detail_if_open, open_result  # noqa: E402
from runtime import load_locators, note_id  # noqa: E402

NOTE = "6ac48ca100000000140034ed"
OTHER = "6ac4c9f80000000015017110"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=token&xsec_source=pc_search"
OTHER_URL = f"https://www.xiaohongshu.com/explore/{OTHER}?xsec_token=token"
SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword=%25E4%25B9%259D%25E5%25AF%25A8"


def patch_browser(monkeypatch, browser) -> None:
    """Route every module's CLI calls to `browser`.

    Each module binds `chrome_agent` at import time (`from runtime import
    chrome_agent`), so patching `runtime` alone would leave the copies held by
    `comments`, `media` and `discover` pointing at the real CLI.
    """
    for module in (batch, discover, runtime, comments, media):
        monkeypatch.setattr(module, "chrome_agent", browser)


def element(ref: str, **kwargs: object) -> dict:
    result = {
        "ref": ref,
        "tag": "a",
        "className": "",
        "href": None,
        "text": None,
        "states": {"visible": True},
        "rect": {"x": 0, "y": 0, "width": 100, "height": 100},
    }
    result.update(kwargs)
    return result


def page(url: str, *elements: dict) -> dict:
    return {"url": url, "elements": list(elements)}


def search_page() -> dict:
    return page(SEARCH_URL, element("search", tag="input", className="search-input"))


def detail_page(
    url: str = NOTE_URL, *, with_close: bool = True, with_mask: bool = True
) -> dict:
    elements = [
        element(
            "container",
            className="note-container",
            rect={"x": 0, "y": 0, "width": 600, "height": 740},
        ),
        element("body", className="note-text"),
    ]
    if with_close:
        elements.append(element("close", tag="button", className="reds-button-new close-icon"))
    if with_mask:
        elements.append(element("mask", className="note-detail-mask"))
    return page(url, *elements)


class FakeBrowser:
    """Serve queued snapshots in order; the last one repeats."""

    def __init__(self, *pages: dict) -> None:
        self._queue = list(pages)
        self.calls: list[tuple] = []

    def __call__(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:2] == ("page", "snapshot"):
            return self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        return {"success": True, "clicked": True, "keypressed": args[-1]}

    def modes(self) -> list[str]:
        return [args[0] + "." + args[1] for args in self.calls if args[:2] != ("page", "snapshot")]


@pytest.fixture
def config() -> dict:
    return load_locators()


# --- search discovery -------------------------------------------------------


def test_search_discovery_keeps_visible_signed_result_cards_and_title(config) -> None:
    href = f"https://www.xiaohongshu.com/search_result/{NOTE}?xsec_token=token&xsec_source=pc_search"
    found = discover.discover(
        page(
            SEARCH_URL,
            element(
                "bad",
                href=f"https://www.xiaohongshu.com/explore/{NOTE}",
                className="overlay",
                states={"visible": False},
                rect={"x": 0, "y": 0, "width": 0, "height": 0},
            ),
            element("card", tag="section", className="note-item", text="卡片回退标题"),
            element("cover", href=href, className="cover mask ld"),
            element("title", href=href, className="title", text="准确标题"),
        ),
        config,
        limit=10,
    )

    assert found == [
        {
            "rank": 1,
            "ref": "cover",
            "title": "准确标题",
            "href": href,
            "noteId": NOTE,
            "hasAccessContext": True,
        }
    ]


def test_search_discovery_falls_back_to_enclosing_card_text(config) -> None:
    href = f"https://www.xiaohongshu.com/search_result/{NOTE}?xsec_token=token"
    found = discover.discover(
        page(
            SEARCH_URL,
            element(
                "card",
                tag="section",
                className="note-item",
                text="卡片回退标题",
                rect={"x": 0, "y": 0, "width": 200, "height": 300},
            ),
            element(
                "cover",
                href=href,
                className="cover mask ld",
                rect={"x": 10, "y": 10, "width": 150, "height": 200},
            ),
        ),
        config,
        limit=1,
    )

    assert found[0]["title"] == "卡片回退标题"


# --- helpers -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        NOTE_URL,
        f"https://www.xiaohongshu.com/search_result/{NOTE}?xsec_token=token",
        f"https://www.xiaohongshu.com/discovery/item/{NOTE}",
    ],
)
def test_note_id_reads_every_url_shape(url) -> None:
    assert note_id(url) == NOTE


@pytest.mark.parametrize(
    ("url", "expected"),
    [(None, None), (SEARCH_URL, None), ("https://www.xiaohongshu.com/explore", None)],
)
def test_note_id_ignores_non_note_urls(url, expected) -> None:
    assert note_id(url) is expected


# --- closing an open detail -------------------------------------------------


def test_close_detail_is_a_noop_when_no_detail_is_open(monkeypatch, config) -> None:
    fake = FakeBrowser(search_page())
    monkeypatch.setattr(discover, "chrome_agent", fake)

    assert close_detail_if_open(1, config) is None
    assert fake.calls == [("page", "snapshot", "--tab-id", "1", "--scope", "full")]


def test_close_detail_clicks_the_close_control(monkeypatch, config) -> None:
    fake = FakeBrowser(detail_page(), search_page())
    monkeypatch.setattr(discover, "chrome_agent", fake)

    assert close_detail_if_open(1, config) == "close_control"
    assert fake.modes() == ["page.click"]


def test_close_detail_falls_back_to_keypress_when_control_is_hidden(monkeypatch, config) -> None:
    """The narrow layout hides the close button; Escape is what dismisses it."""
    fake = FakeBrowser(detail_page(with_close=False), detail_page(with_close=False), search_page())
    monkeypatch.setattr(discover, "chrome_agent", fake)

    assert close_detail_if_open(1, config) == "keypress:Escape"
    assert fake.modes() == ["page.keypress"]


def test_close_detail_handles_a_layout_without_note_container(monkeypatch, config) -> None:
    """Wide windows render the note as a standalone page: mask, no container."""
    standalone = page(NOTE_URL, element("mask", className="note-detail-mask"))
    fake = FakeBrowser(standalone, standalone, search_page())
    monkeypatch.setattr(discover, "chrome_agent", fake)

    assert close_detail_if_open(1, config) == "keypress:Escape"


def test_close_detail_raises_when_nothing_dismisses_it(monkeypatch, config) -> None:
    fake = FakeBrowser(detail_page(with_close=False))
    monkeypatch.setattr(discover, "chrome_agent", fake)
    monkeypatch.setattr(discover, "DETAIL_CLOSE_TIMEOUT", 0.0)

    with pytest.raises(RuntimeError, match="详情遮罩仍开着"):
        close_detail_if_open(1, config)


# --- opening a result -------------------------------------------------------


def selected(rank: int = 1, ref: str = "cover", target: str = NOTE) -> dict:
    return {
        "rank": rank,
        "ref": ref,
        "noteId": target,
        "title": "t",
        "href": f"https://www.xiaohongshu.com/search_result/{target}?xsec_token=token",
    }


def test_open_result_confirms_the_landing_note(monkeypatch, config) -> None:
    fake = FakeBrowser(detail_page())
    monkeypatch.setattr(discover, "chrome_agent", fake)

    opened = open_result(1, selected(), "click", config)

    assert opened["noteId"] == NOTE
    assert opened["mode"] == "click"
    # The tab is foregrounded first: a backgrounded tab's lazy loading is
    # throttled by Chrome, so the comment thread would never grow.
    assert fake.modes() == ["tabs.activate", "page.click"]


def test_open_result_falls_back_to_navigate_when_the_click_mislands(monkeypatch, config) -> None:
    """A click that hits the overlay reports success but stays on the old note."""
    fake = FakeBrowser(detail_page(OTHER_URL), detail_page(NOTE_URL))
    monkeypatch.setattr(discover, "chrome_agent", fake)
    monkeypatch.setattr(discover, "NOTE_OPEN_TIMEOUT", 0.0)

    opened = open_result(1, selected(), "click", config)

    assert opened["mode"] == "navigate"
    assert fake.modes() == ["tabs.activate", "page.click", "tabs.navigate"]


def test_open_result_raises_instead_of_collecting_the_wrong_note(monkeypatch, config) -> None:
    fake = FakeBrowser(detail_page(OTHER_URL))
    monkeypatch.setattr(discover, "chrome_agent", fake)
    monkeypatch.setattr(discover, "NOTE_OPEN_TIMEOUT", 0.0)

    with pytest.raises(RuntimeError, match="没有落在目标笔记"):
        open_result(1, selected(), "click", config)


# --- comments ---------------------------------------------------------------
#
# The texts and rects below are captured verbatim from a live 1440px window on
# note 69fad4b5 (71 comments). They are kept literal because the whole defect
# being guarded against is a property of this exact text: a reply's innerText
# has the same line shape as a top-level comment's, so only the DOM containers
# can say which is which.

COMMENT_PINNED = "\n".join([
    "椰子岛🏝", "作者",
    "大家注意川西7-8月雨季泥石流高发，出发之前一定要看好天气、查看沿途泥石流风险，避开高危地段。",
    "置顶评论", "07-05湖南", "3", "回复",
])
COMMENT_PLAIN = "\n".join(["老虎吃鸡", "请问轿车可以去吗", "06-07重庆", "1", "4"])
COMMENT_THIRD = "\n".join(["Hui.", "会高反吗", "09-19江苏", "1", "1"])
REPLY_ONE = "\n".join([
    "椰子岛🏝", "作者",
    "丹道秘境轿车可能走不了，那边路不太好走，容易刮底盘",
    "06-07湖南", "2", "回复",
])
REPLY_TWO = "\n".join([
    "椰子岛🏝", "作者",
    "看个人体质，我们几个小伙伴有部分高反的，也有适应很好的",
    "09-20湖南", "赞", "回复",
])

COMMENT_TEXTS = {
    "e555": "共 71 条评论\n" + COMMENT_PINNED,
    "e559": COMMENT_PINNED,
    "e559i": COMMENT_PINNED,
    "e583": COMMENT_PLAIN,
    "e583i": COMMENT_PLAIN,
    "e628": COMMENT_THIRD,
    "e628i": COMMENT_THIRD,
    "e605": REPLY_ONE,
    "e605i": REPLY_ONE,
    "e650": REPLY_TWO,
}


def rect(x: int, y: int, width: int, height: int) -> dict:
    return {"x": x, "y": y, "width": width, "height": height}


def comment_thread_page() -> dict:
    """Three top-level comments, the second and third each carrying one reply.

    The rects mirror the live tree, where a parent wraps its own comment and
    the replies' `comment-inner-container`s. Only the FIRST reply of a parent
    also carries `comment-item-sub`; the fixture keeps that asymmetry, because
    matching replies on `comment-item-sub` is exactly what dropped every later
    one.
    """
    return page(
        NOTE_URL,
        element("e555", tag="div", className="comments-container", rect=rect(820, -541, 439, 2658)),
        element("e558", tag="div", className="parent-comment", rect=rect(836, -497, 407, 179)),
        element("e559", tag="div", className="comment-item", rect=rect(836, -497, 407, 140)),
        element("e559i", tag="div", className="comment-inner-container",
                rect=rect(836, -489, 407, 124)),
        element("e582", tag="div", className="parent-comment", rect=rect(836, -302, 407, 267)),
        element("e583", tag="div", className="comment-item", rect=rect(836, -302, 407, 108)),
        element("e583i", tag="div", className="comment-inner-container",
                rect=rect(836, -294, 407, 100)),
        element("e603", tag="div", className="reply-container", rect=rect(888, -194, 355, 159)),
        element("e605", tag="div", className="comment-item comment-item-sub",
                rect=rect(888, -194, 355, 127)),
        element("e605i", tag="div", className="comment-inner-container",
                rect=rect(888, -186, 355, 115)),
        element("e627", tag="div", className="parent-comment", rect=rect(836, -19, 407, 235)),
        element("e628", tag="div", className="comment-item", rect=rect(836, -19, 407, 108)),
        element("e628i", tag="div", className="comment-inner-container",
                rect=rect(836, -11, 407, 100)),
        element("e648", tag="div", className="reply-container", rect=rect(888, 89, 355, 127)),
        # No `comment-item-sub` wrapper at all: a parent's second reply is a
        # bare `comment-inner-container`, as measured live.
        element("e650", tag="div", className="comment-inner-container",
                rect=rect(888, 89, 355, 127)),
    )


class FakeCommentBrowser:
    """One repeating snapshot plus per-ref text, enough to drive extraction."""

    def __init__(self, snapshot_page: dict, texts: dict[str, str]) -> None:
        self.page = snapshot_page
        self.texts = texts
        self.calls: list[tuple] = []

    def __call__(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:2] == ("tabs", "list"):
            return {"tabs": [{"id": 1, "url": NOTE_URL, "title": "笔记"}]}
        if args[:2] == ("page", "snapshot"):
            return self.page
        if args[:2] == ("page", "text"):
            text = self.texts.get(args[args.index("--ref") + 1], "")
            return {"text": text, "length": len(text), "truncated": False}
        if args[:2] == ("page", "scroll"):
            return {"moved": False, "scrollY": 0, "maxScrollY": 0}
        return {"success": True, "clicked": True}

    def methods(self) -> list[str]:
        return [args[0] + "." + args[1] for args in self.calls]


@pytest.fixture
def comment_browser(monkeypatch):
    browser = FakeCommentBrowser(comment_thread_page(), COMMENT_TEXTS)
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)
    return browser


def test_matches_names_the_top_level_comment_by_what_it_lacks(config) -> None:
    """A reply carries both classes, so class_excludes is the only separator."""
    rules = config["detail"]
    top = element("t", tag="div", className="comment-item")
    reply = element("r", tag="div", className="comment-item comment-item-sub")
    assert runtime.matches(top, rules["comment_item"]) is True
    assert runtime.matches(reply, rules["comment_item"]) is False


def test_a_reply_is_matched_by_its_inner_container_not_its_wrapper(config) -> None:
    """`comment-item-sub` marks only a parent's first reply.

    Measured live: under one parent, reply 1's row div was
    `comment-item comment-item-sub` while reply 2's was a bare
    `comment-inner-container` — so a locator keyed on `comment-item-sub` drops
    every later reply. Every row, top-level or reply, has exactly one
    `comment-inner-container`, and that is what names a reply.
    """
    rules = config["detail"]
    row = element("r", tag="div", className="comment-inner-container")
    assert runtime.matches(row, rules["comment_reply"]) is True
    # The wrapper alone is not a row, so it must not be picked up as one.
    wrapper = element("w", tag="div", className="comment-item comment-item-sub")
    assert runtime.matches(wrapper, rules["comment_reply"]) is False


def test_parse_comment_keeps_the_body_and_its_flags() -> None:
    parsed = comments.parse_comment(COMMENT_PINNED)
    assert parsed["author"] == "椰子岛🏝"
    assert parsed["text"] == (
        "大家注意川西7-8月雨季泥石流高发，出发之前一定要看好天气、"
        "查看沿途泥石流风险，避开高危地段。"
    )
    assert parsed["pinned"] is True
    assert parsed["byAuthor"] is True


def test_parse_comment_drops_like_counts_and_place_lines() -> None:
    parsed = comments.parse_comment(COMMENT_PLAIN)
    assert parsed == {"author": "老虎吃鸡", "text": "请问轿车可以去吗",
                      "pinned": False, "byAuthor": False, "replyTo": None}


@pytest.mark.parametrize(
    "line",
    ["昨天 22:37上海", "前天 09:12北京", "刚刚", "10-13四川",
     "2026-10-07四川", "7天前广东", "22:37"],
)
def test_a_recent_comment_still_ends_at_its_time_line(line) -> None:
    """Xiaohongshu writes a recent comment's time as a relative word.

    Matching only `N天前` / `MM-DD` / `YYYY-MM-DD` lets "昨天 22:37上海" leak into
    the body, which is what a delivered sample showed.
    """
    parsed = comments.parse_comment(f"嗯嗯\n九寨沟哪个更好\n{line}\n赞\n回复")
    assert parsed["text"] == "九寨沟哪个更好"


@pytest.mark.parametrize("body", ["今天去了四姑娘山，很美", "前天发的这条", "刚刚拍的"])
def test_a_body_opening_with_a_day_word_is_not_cut(body) -> None:
    """A day word is the time line only together with the clock time."""
    parsed = comments.parse_comment(f"老虎吃鸡\n{body}\n昨天 22:37上海\n赞\n回复")
    assert parsed["text"] == body


def test_a_body_that_looks_like_a_date_is_not_taken_for_the_time_line() -> None:
    """`70-200的头` is a lens, not a date, and used to be read as one.

    A loose `\\d{2}-\\d{2}` matched `70-20` with `0的头` left over for a place,
    which made this reply — observed live on note 6a6df219 — come back with an
    empty body.
    """
    parsed = comments.parse_comment(
        "西部的海胖胖\n作者\n70-200的头\n08-05四川\n赞\n回复", is_reply=True
    )
    assert parsed["text"] == "70-200的头"


def test_only_the_line_above_the_ui_tail_ends_the_body() -> None:
    """A date-shaped line followed by more body is body, not the time line.

    The tight date shapes alone still match a body opening like "10-05去的…";
    what settles it is that the real time line sits directly above `赞` and
    `回复`, and nothing else does.
    """
    lines = ["老虎吃鸡", "10-05去的，路况不错", "08-05四川", "赞", "回复"]
    assert comments.comment_body_end(lines) == 2
    parsed = comments.parse_comment("\n".join(lines))
    assert parsed["text"] == "10-05去的，路况不错"


def test_a_reply_parses_identically_to_a_comment_which_is_the_whole_problem() -> None:
    """Documents why nesting must come from the DOM: the text cannot tell them apart."""
    reply = comments.parse_comment(REPLY_ONE)
    assert set(reply) == set(comments.parse_comment(COMMENT_PLAIN))
    assert reply["author"] == "椰子岛🏝"
    assert reply["text"] == "丹道秘境轿车可能走不了，那边路不太好走，容易刮底盘"


def test_extract_comments_nests_each_reply_under_its_own_comment(comment_browser, config) -> None:
    items, warnings = comments.extract_comments(1, config, 3)

    assert warnings == []
    assert [item["author"] for item in items] == ["椰子岛🏝", "老虎吃鸡", "Hui."]
    assert [item["index"] for item in items] == [1, 2, 3]
    # The pinned note-author comment has no replies of its own.
    assert items[0]["pinned"] is True
    assert items[0]["replyCount"] == 0 and items[0]["replies"] == []
    # Each reply lands under the comment it belongs to, not the one above it.
    assert items[1]["replies"] == [{
        "author": "椰子岛🏝",
        "text": "丹道秘境轿车可能走不了，那边路不太好走，容易刮底盘",
        "pinned": False,
        "byAuthor": True,
        "replyTo": None,
        "index": 1,
        "depth": 1,
        "replyCount": 0,
        "replies": [],
    }]
    assert items[2]["replies"][0]["text"] == (
        "看个人体质，我们几个小伙伴有部分高反的，也有适应很好的"
    )


def row(author: str, reply_to: str | None = None) -> dict:
    return {"author": author, "text": f"{author}的话", "replyTo": reply_to,
            "pinned": False, "byAuthor": False}


def test_a_reply_to_a_reply_nests_under_that_reply() -> None:
    """The DOM is flat, so the tree comes from the `回复 X ：` prefix.

    Measured live: a parent's three replies were siblings at the same x inside
    one `reply-container`, with nothing in the containers to say which answered
    which.
    """
    roots, total = comments.nest_replies([row("甲"), row("乙", "甲"), row("甲", "乙")])

    assert total == 3
    # The comment's own direct reply is depth 1, and the two rows below it go
    # one level deeper each.
    assert [node["depth"] for node in roots] == [1]
    assert roots[0]["replyCount"] == 2
    second = roots[0]["replies"][0]
    assert (second["author"], second["depth"], second["index"]) == ("乙", 2, 1)
    third = second["replies"][0]
    assert (third["author"], third["depth"], third["index"]) == ("甲", 3, 1)
    assert third["replyCount"] == 0 and third["replies"] == []


def test_naming_the_top_level_commenter_attaches_at_the_top() -> None:
    """There is no row by that author to answer, so the row stays at depth 1.

    The commenter wrote the comment, not a reply; `latest` only holds rows.
    """
    roots, _ = comments.nest_replies([row("甲", "题主")])
    assert [node["depth"] for node in roots] == [1]
    assert roots[0]["replies"] == []


def test_naming_someone_outside_this_thread_attaches_at_the_top() -> None:
    """A deleted reply, or a name that never renders here, proves nothing.

    Guessing a parent for it would invent a depth the page never showed.
    """
    roots, _ = comments.nest_replies([row("甲", "查无此人")])
    assert [node["depth"] for node in roots] == [1]


def test_a_question_and_its_answer_nest_three_deep() -> None:
    """The real chain from note 6a6df219, which the top-level author's name broke.

    A commenter asks, the note author answers, the commenter follows up, and the
    author answers again — Xiaohongshu prints `回复 <问的人>：` on that last row.
    Special-casing the top-level commenter's name made it a sibling of the
    answer it was replying to.
    """
    roots, total = comments.nest_replies([
        row("作者", None),
        row("提问者", "作者"),
        row("作者", "提问者"),
    ])

    assert total == 3
    assert [node["depth"] for node in roots] == [1]
    assert roots[0]["replies"][0]["depth"] == 2
    assert roots[0]["replies"][0]["replies"][0]["depth"] == 3


def test_a_reply_attaches_to_the_most_recent_row_by_that_author() -> None:
    """Rows are chronological, so the newest match is the one being answered.

    Without this, a thread where the same two people answer each other would
    collapse onto the first row that author ever wrote.
    """
    roots, _ = comments.nest_replies(
        [row("甲"), row("乙", "甲"), row("甲", "乙"), row("乙", "甲")]
    )

    first = roots[0]
    assert first["replies"][0]["author"] == "乙"
    back = first["replies"][0]["replies"][0]
    assert back["author"] == "甲" and back["depth"] == 3
    # The second 乙 answers the second 甲, not the first 乙.
    assert back["replies"][0]["author"] == "乙"
    assert back["replyCount"] == 1


def test_a_comment_and_its_replies_count_as_one_against_the_budget(
    comment_browser, config
) -> None:
    """--comment-limit 1 means one comment, even when it carries replies."""
    items, _ = comments.extract_comments(1, config, 1)
    assert len(items) == 1
    assert items[0]["author"] == "椰子岛🏝"


def test_collect_comments_reports_an_incomplete_read(comment_browser, config) -> None:
    payload, warnings = comments.collect_comments(1, config, 5)
    assert payload["declaredTotal"] == 71
    assert payload["requested"] == 5
    assert payload["collected"] == 3
    assert payload["possiblyIncomplete"] is True
    assert any("评论" in warning for warning in warnings)
    # The container's own text is a debugging aid, not a result to read back.
    assert "rawText" not in payload


def test_collect_comments_is_complete_once_the_budget_is_met(comment_browser, config) -> None:
    payload, _ = comments.collect_comments(1, config, 3)
    assert payload["collected"] == 3
    assert payload["possiblyIncomplete"] is False


def test_reading_comments_brings_the_tab_to_the_front(comment_browser, config) -> None:
    """Chrome throttles background tabs' event loop.

    The thread only appends its next batch from that loop, so a backgrounded
    tab reads the first page and stops no matter how far it is scrolled.
    """
    comments.collect_comments(1, config, 1)
    assert ("tabs", "activate", "1") in comment_browser.calls


def test_a_thread_read_in_full_is_not_flagged_incomplete(monkeypatch, config) -> None:
    """The page's counter includes replies.

    Comparing it against the top-level count — rather than every row read —
    invents a warning for a thread that was in fact read whole.
    """
    texts = {
        "cont": "共 3 条评论\n" + COMMENT_PINNED,
        "c": COMMENT_PINNED,
        "c-i": COMMENT_PINNED,
        "c2": COMMENT_PLAIN,
        "c2-i": COMMENT_PLAIN,
        "r": REPLY_ONE,
    }
    thread = page(
        NOTE_URL,
        element("cont", tag="div", className="comments-container", rect=rect(820, 0, 439, 600)),
        element("p1", tag="div", className="parent-comment", rect=rect(836, 0, 407, 140)),
        element("c", tag="div", className="comment-item", rect=rect(836, 0, 407, 140)),
        element("c-i", tag="div", className="comment-inner-container", rect=rect(836, 8, 407, 124)),
        element("p2", tag="div", className="parent-comment", rect=rect(836, 150, 407, 220)),
        element("c2", tag="div", className="comment-item", rect=rect(836, 150, 407, 108)),
        element("c2-i", tag="div", className="comment-inner-container",
                rect=rect(836, 158, 407, 100)),
        element("r", tag="div", className="comment-inner-container", rect=rect(888, 258, 355, 100)),
    )
    browser = FakeCommentBrowser(thread, texts)
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)

    payload, warnings = comments.collect_comments(1, config, 5)

    assert payload["collected"] == 2
    assert payload["repliesCollected"] == 1
    assert payload["declaredTotal"] == 3
    assert payload["possiblyIncomplete"] is False
    assert not any("只读到" in warning for warning in warnings)


def test_the_end_of_thread_marker_settles_completeness(comment_browser, config) -> None:
    """'- THE END -' is the page saying nothing more will load, so a thread that
    looks short next to the counter is still a thread that was read to its end."""
    comment_browser.texts["e555"] = "共 71 条评论\n" + COMMENT_PINNED + "\n- THE END -"

    payload, warnings = comments.collect_comments(1, config, 5)

    assert payload["threadEnded"] is True
    assert payload["possiblyIncomplete"] is False
    assert not any("只读到" in warning for warning in warnings)


def test_a_note_with_no_comments_is_not_reported_as_incomplete(monkeypatch, config) -> None:
    """A note with comments switched off renders an empty state, not a counter.

    Without reading that state a zero-comment note is indistinguishable from a
    thread that failed to load, and gets warned about forever.
    """
    empty = page(
        NOTE_URL,
        element("e", tag="div", className="no-comments", rect=rect(836, 300, 407, 200)),
        element("e-t", tag="div", className="no-comments-text", rect=rect(902, 535, 200, 40)),
    )
    browser = FakeCommentBrowser(empty, {})
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)

    payload, warnings = comments.collect_comments(1, config, 10)

    assert payload["collected"] == 0
    assert payload["noComments"] is True
    assert payload["possiblyIncomplete"] is False
    assert warnings == []


def test_expand_replies_clicks_until_no_control_is_left(monkeypatch, config) -> None:
    """Collapsed replies are absent from the DOM; the control must be pressed."""
    browser = FakeBrowser(
        page(NOTE_URL, element("more", tag="div", className="show-more")),
        page(NOTE_URL),
    )
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)

    assert comments.expand_replies(1, config) == []
    assert browser.modes().count("page.click") == 1


def test_recapture_comments_keeps_downloaded_media(comment_browser, tmp_path) -> None:
    """Fixing comments must not cost a second round of image downloads."""
    output = tmp_path / "note.json"
    output.write_text(json.dumps({
        "noteId": note_id(NOTE_URL),
        "url": NOTE_URL,
        "content": {"text": "正文", "length": 2, "truncated": False},
        # The pre-v2 layout kept the comment block, the media block and the tab
        # source inside the note file; a re-read has to leave none of them there.
        "comments": {"items": [{"index": 1, "author": "旧", "text": "旧"}]},
        "source": {"tabId": 1, "url": NOTE_URL, "title": "旧标题 - 小红书"},
        "media": {"images": [{"src": "https://example.com/a.webp"}], "downloads": []},
        "warnings": [
            "评论仅包含当前 Web 页面已加载和已展开的范围",
            "正文达到采集上限，可能不完整",
        ],
    }, ensure_ascii=False))

    payload = comments.recapture_comments(1, output_path=output, comment_limit=3)

    # The thread lands in its own file, ready to be replaced on its own later.
    assert payload["collected"] == 3
    assert [item["author"] for item in payload["items"]] == [
        "椰子岛🏝", "老虎吃鸡", "Hui."
    ]
    assert "source" not in payload
    written = json.loads((tmp_path / "comments.json").read_text())
    assert written["noteId"] == note_id(NOTE_URL)
    assert written["collected"] == 3 and written["requested"] == 3
    assert written["warnings"] == []

    # The note keeps the note itself — a comment pass never re-downloads media,
    # and the media now lives in downloads.json, moved there, not re-fetched.
    note = json.loads(output.read_text())
    assert note["content"] == "正文"
    assert note["url"] == NOTE_URL
    for key in ("comments", "commentsFile", "media", "source"):
        assert key not in note
    assert json.loads((tmp_path / "downloads.json").read_text())["images"] == [
        {"src": "https://example.com/a.webp"}
    ]
    # The comment diagnostic moved to the comment file with the thread; the note
    # keeps its own warning and gains one line saying what the migration left out.
    assert note["warnings"] == [
        "正文达到采集上限，可能不完整",
        migrate.MIGRATION_WARNING,
    ]


def test_recapture_comments_refuses_a_note_that_was_never_collected(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="不存在"):
        comments.recapture_comments(1, output_path=tmp_path / "missing" / "note.json")


REPLY_TO_REPLY = "\n".join(["北斋", "回复 椰子岛🏝 : 电车SUV好走不", "10-06北京", "1", "回复"])


def test_a_reply_aimed_at_a_reply_names_its_target_separately() -> None:
    """Otherwise every reply-to-a-reply reads as if it began "回复 某人 :"."""
    parsed = comments.parse_comment(REPLY_TO_REPLY, is_reply=True)
    assert parsed["author"] == "北斋"
    assert parsed["replyTo"] == "椰子岛🏝"
    assert parsed["text"] == "电车SUV好走不"
    # The same text is left alone when it is not known to be a reply.
    assert comments.parse_comment(REPLY_TO_REPLY)["text"] == "回复 椰子岛🏝 : 电车SUV好走不"


def test_scroll_anchor_prefers_the_comments_container(config) -> None:
    """`note-scroller` falls out of the snapshot window once the page moves."""
    both = page(
        NOTE_URL,
        element("scroller", tag="div", className="note-scroller"),
        element("comments", tag="div", className="comments-container"),
    )
    only_scroller = page(NOTE_URL, element("scroller", tag="div", className="note-scroller"))
    only_body = page(NOTE_URL, element("text", tag="span", className="note-text"))
    assert comments.resolve_scroll_anchor(both, config)["ref"] == "comments"
    assert comments.resolve_scroll_anchor(only_scroller, config)["ref"] == "scroller"
    # The comments container is often still crowded out of the 500-element window
    # on a note that has just opened; the body anchors the same right-half scroller.
    assert comments.resolve_scroll_anchor(only_body, config)["ref"] == "text"
    assert comments.resolve_scroll_anchor(page(NOTE_URL), config) is None


def test_scroll_anchor_is_re_resolved_after_an_unmoved_retry(monkeypatch, config) -> None:
    """A just-opened note can report "not moved" before its thread has laid out."""
    holder = page(NOTE_URL, element("comments", tag="div", className="comments-container"))
    browser = FakeCommentBrowser(holder, {})
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)

    step = comments.scroll_comments(1, holder, config)

    assert step["moved"] is False
    # One retry, and the anchor is re-resolved from a fresh snapshot for it.
    assert browser.methods().count("page.scroll") == 2
    assert browser.methods().count("page.snapshot") == 1


class StallingCommentBrowser:
    """A scroller stuck at its end while the thread keeps growing behind it.

    Captured live on note 69fad4b5: `moved` was already False while the
    scroller's maximum grew 3281 -> 5509 -> 6964 -> 7938 and the thread went
    from 19 to 55 rows. Stopping on the first `moved: false` is what lost the
    tail of the thread, so this browser reproduces exactly that shape.
    """

    def __init__(self, pages: list[dict], rows: list[int], texts: dict[str, str]) -> None:
        self.pages = pages
        self.rows = rows
        self.texts = texts
        self.calls: list[tuple] = []
        self.scrolls = 0

    def __call__(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:2] == ("page", "snapshot"):
            return self.pages[min(self.scrolls, len(self.pages) - 1)]
        if args[:2] == ("page", "text"):
            ref = args[args.index("--ref") + 1]
            if ref == "e555":  # the comments container: the thread's own size
                count = self.rows[min(self.scrolls, len(self.rows) - 1)]
                return {"text": "\n".join(f"{i:02d}-01某地" for i in range(count))}
            return {"text": self.texts.get(ref, ""), "length": 0, "truncated": False}
        if args[:2] == ("page", "scroll"):
            self.scrolls += 1
            return {"moved": False, "scrollY": 3281, "maxScrollY": 3281}
        return {"success": True, "clicked": True}


def one_comment_page() -> dict:
    return page(
        NOTE_URL,
        element("e555", tag="div", className="comments-container", rect=rect(820, 0, 439, 600)),
        element("e558", tag="div", className="parent-comment", rect=rect(836, 0, 407, 179)),
        element("e559", tag="div", className="comment-item", rect=rect(836, 0, 407, 179)),
    )


def test_thread_size_counts_what_the_container_rendered(comment_browser, config) -> None:
    assert comments.thread_size(1, config) == 1
    # An unresolvable container is unknown, not zero — the caller must not read
    # it as "the thread stopped growing".
    assert comments.thread_size(1, config, page(NOTE_URL)) is None


def test_a_plain_snapshot_leaves_the_element_ceiling_alone(monkeypatch) -> None:
    browser = FakeCommentBrowser(page(NOTE_URL), {})
    patch_browser(monkeypatch, browser)

    runtime.snapshot(1)

    assert browser.calls[-1] == ("page", "snapshot", "--tab-id", "1", "--scope", "full")


def test_the_comment_path_raises_the_snapshot_ceiling(comment_browser, config) -> None:
    """500 elements is about 22 comment rows, which is under half a long thread."""
    comments.thread_size(1, config)

    snapshots = [call for call in comment_browser.calls if call[:2] == ("page", "snapshot")]
    assert snapshots, "thread_size 应当先取快照"
    assert "--limit" in snapshots[-1]
    assert str(config["scroll"]["comments"]["snapshot_limit"]) in snapshots[-1]


def test_the_thread_keeps_loading_after_the_scroller_reports_it_cannot_move(
    monkeypatch, config
) -> None:
    """The end is declared by the thread text, not by the scroll position."""
    browser = StallingCommentBrowser(
        pages=[one_comment_page(), comment_thread_page()],
        rows=[1, 2, 3, 3, 3, 3, 3, 3],
        texts=COMMENT_TEXTS,
    )
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)

    items, _ = comments.extract_comments(1, config, 5)

    # Two of these only exist on the second page, so they can only have been read
    # by pressing on past the first `moved: false`.
    assert [item["author"] for item in items] == ["椰子岛🏝", "老虎吃鸡", "Hui."]
    assert browser.scrolls >= 5


# --- note metadata (note.py) -------------------------------------------------


def test_the_note_id_is_the_publish_time() -> None:
    """The page prints only a partial date, so the ID is what can be stored.

    Two live notes agree: 6a6df219 decodes to 2026-08-01, the date that note's
    own footer prints as "08-01".
    """
    assert note.published_at("6a6df2194d1f000000000000") == "2026-08-01T21:18:17+08:00"


def test_a_non_hex_id_yields_no_publish_time() -> None:
    assert note.published_at("zzzz") is None
    assert note.published_at(None) is None


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("2341", 2341),
        ("1.2万", 12000),
        ("3.5w", 35000),
        # Xiaohongshu writes a count of zero as the bare word.
        ("赞", 0),
        ("", 0),
        (None, None),
    ],
)
def test_interaction_counts(printed, expected) -> None:
    assert note.parse_count(printed) == expected


@pytest.mark.parametrize(
    ("printed", "date", "place"),
    [
        ("08-01", "08-01", None),
        ("09-29 河南", "09-29", "河南"),
        ("2025-11-03", "2025-11-03", None),
        ("4天前 西藏", "4天前", "西藏"),
        # A note edited after publishing prints this instead (measured live).
        ("编辑于 2025-09-24", "2025-09-24", None),
    ],
)
def test_the_footer_row_splits_into_date_and_ip_location(
    printed, date, place, monkeypatch
) -> None:
    """All four shapes were measured live on 2026-10-07; the place is optional."""
    browser = FakeCommentBrowser(
        page(
            NOTE_URL,
            element("footer", tag="div", className="bottom-container",
                    rect=rect(836, 200, 407, 20)),
            element("date", tag="span", className="date", text=printed,
                    rect=rect(836, 200, 200, 20)),
        ),
        {},
    )
    patch_browser(monkeypatch, browser)

    assert note.read_footer(browser.page, load_locators()["detail"]) == (date, place)


def note_detail_page() -> dict:
    """A note detail carrying every field note.json is built from."""
    return page(
        NOTE_URL,
        element("container", tag="div", className="note-container",
                rect=rect(32, 0, 1376, 900)),
        element("content", tag="div", className="note-content",
                rect=rect(820, 60, 439, 400)),
        element("title", tag="h1", className="title",
                text="徒步雀儿山主峰冰川，解锁新路海的不同视角",
                rect=rect(836, 60, 407, 30)),
        element("body", tag="div", className="note-text", rect=rect(836, 100, 407, 300)),
        element("tag1", tag="a", className="tag", text="#户外[话题]#",
                rect=rect(836, 360, 60, 20)),
        element("tag2", tag="a", className="tag", text="#川西[话题]#",
                rect=rect(900, 360, 60, 20)),
        element("author-scope", tag="div", className="author-container",
                rect=rect(836, 400, 407, 40)),
        element("author", tag="a", className="name", text="椰子岛🏝",
                href="https://www.xiaohongshu.com/user/profile/5f0a1b2c3d4e5f6a7b8c9d0e",
                rect=rect(880, 405, 100, 20)),
        element("footer", tag="div", className="bottom-container",
                rect=rect(836, 450, 407, 20)),
        element("date", tag="span", className="date", text="08-01",
                rect=rect(836, 450, 200, 20)),
        element("engage", tag="div", className="engage-bar interactions",
                rect=rect(820, 800, 439, 40)),
        element("like", tag="span", className="like-wrapper", text="2341",
                rect=rect(900, 805, 40, 20)),
        element("collect", tag="span", className="collect-wrapper", text="3159",
                rect=rect(950, 805, 40, 20)),
        element("chat", tag="span", className="chat-wrapper", text="610",
                rect=rect(1000, 805, 40, 20)),
    )


def test_tags_lose_the_markers_the_page_prints_on_them(config) -> None:
    """The body prints a topic as `#户外[话题]#`; the stored tag is the bare name."""
    assert note.read_tags(note_detail_page(), config["detail"]) == ["户外", "川西"]


def test_the_tag_rule_picks_topic_links_not_author_badges(config) -> None:
    """Live, a topic is `a.tag` and a comment's 作者 badge is `span.tag`."""
    rules = config["detail"]

    assert runtime.matches(
        element("t", tag="a", className="tag", text="#旅游"), rules["note_tags"]
    )
    assert not runtime.matches(
        element("b", tag="span", className="tag", text="作者"), rules["note_tags"]
    )


def test_a_field_the_page_does_not_print_is_null(config) -> None:
    """No share element exists on the web detail view, and some notes print no
    IP location; both are reported as null rather than guessed at."""
    meta = note.read_meta(note_detail_page(), config)

    assert meta["stats"] == {"likes": 2341, "collects": 3159, "comments": 610, "shares": None}
    assert meta["ipLocation"] is None
    assert meta["author"] == "椰子岛🏝"
    assert meta["authorId"] == "5f0a1b2c3d4e5f6a7b8c9d0e"
    assert meta["title"] == "徒步雀儿山主峰冰川，解锁新路海的不同视角"
    # `publishedAt` is decoded from the fixture's own note ID, not the printed
    # "08-01" — the footer's date alone cannot be placed on a calendar.
    assert meta["publishedAt"] == "2026-10-06T13:52:33+08:00"
    assert meta["type"] == "image"
    assert meta["tags"] == ["户外", "川西"]


# --- the three output files (collect.py) -------------------------------------


class FakeNoteBrowser(FakeCommentBrowser):
    """The comment fixture plus a note detail's metadata and body."""

    def __init__(self) -> None:
        page_ = note_detail_page()
        page_["elements"].extend(comment_thread_page()["elements"])
        texts = dict(COMMENT_TEXTS)
        texts["body"] = "徒步雀儿山主峰冰川的正文"
        super().__init__(page_, texts)


def test_collect_note_writes_the_three_files(monkeypatch, tmp_path) -> None:
    """note.json is content only; everything needed to verify a download lives
    in downloads.json, and the thread in comments.json."""
    browser = FakeNoteBrowser()
    patch_browser(monkeypatch, browser)
    monkeypatch.setattr(comments.time, "sleep", lambda _seconds: None)
    note_dir = tmp_path / NOTE

    note_payload = collect.collect_note(
        1,
        note_dir=note_dir,
        output_path=note_dir / "note.json",
        comment_limit=3,
        download_images=False,
        with_comments=True,
    )

    assert set(note_payload) == {
        "schemaVersion", "capturedAt", "noteId", "url", "title", "type", "author",
        "authorId", "ipLocation", "publishedAt", "stats", "tags", "content", "warnings",
    }
    assert note_payload["content"] == "徒步雀儿山主峰冰川的正文"
    assert note_payload["warnings"] == []

    downloads = json.loads((note_dir / "downloads.json").read_text())
    assert downloads["source"] == {"tabId": 1, "url": NOTE_URL, "title": "笔记"}
    assert downloads["content"] == {"length": 12, "truncated": False}
    assert downloads["images"] == [] and downloads["downloads"] == []

    written = json.loads((note_dir / "comments.json").read_text())
    assert written["noteId"] == note_id(NOTE_URL)
    assert written["collected"] == 3
    assert "source" not in written


# --- note images (media.py) --------------------------------------------------

NOTE_SLIDE = {
    "src": "https://sns-webpic-qc.xhscdn.com/202610070935/35cd7f34f8cf04f301f6eb178ebb44d2"
           "/notes_pre_post/1040g3k031mpa84m64s1g5ntn8tfg8h5oso1je4g!nd_dft_wgth_webp_3",
    "width": 1920,
    "height": 1080,
}
# Measured live inside the carousel container, downloaded as slide 1 until the
# size floor stopped it.
PLATFORM_ICON = {
    "src": "https://fe-platform.xhscdn.com/platform/48x48.png",
    "width": 48,
    "height": 48,
}
# A slide that has not mounted yet reports no size at all and must be kept.
UNMOUNTED_SLIDE = {
    "src": "https://sns-webpic-qc.xhscdn.com/202610070935/5eca7b2a931da074c0f2f9f27cf858cc"
           "/notes_pre_post/1040g3k831mp9k5ln4s005ntn8tfg8h5oq4uerhg!nd_dft_wlteh_webp_3",
    "width": None,
    "height": None,
}


def test_the_carousel_keeps_real_slides_including_unmounted_ones() -> None:
    assert media.is_note_image(NOTE_SLIDE, "image_media") is True
    assert media.is_note_image(UNMOUNTED_SLIDE, "image_media") is True


def test_a_thumbnail_sized_page_asset_inside_the_carousel_is_not_a_slide() -> None:
    """Being inside the carousel is not enough: this one really was in there."""
    assert media.is_note_image(PLATFORM_ICON, "image_media") is False


def test_the_broader_fallback_scope_needs_a_note_image_url() -> None:
    """Size cannot carry this check: avatars are 360px and comment pictures 640px."""
    avatar = "https://sns-webpic-qc.xhscdn.com/202610070935/aa/notes/whatever.webp"
    assert media.is_note_image({**NOTE_SLIDE, "src": avatar}, "container") is False
    assert media.is_note_image(NOTE_SLIDE, "container") is True
    assert media.is_note_image(UNMOUNTED_SLIDE, None) is True


# --- batch.py -----------------------------------------------------------------


def test_the_run_log_measures_the_body_not_the_old_dict_container() -> None:
    """Found live: a `--comments-only` run reported 正文3 for every legacy note.

    Those files keep the body as `{"text": ..., "length": ...}`, so `len()` was
    counting the container's keys instead of the text.
    """
    legacy = {"content": {"text": "六月的川西", "length": 5, "truncated": False}}
    assert batch.body_chars(legacy) == 5
    assert batch.body_chars({"content": "六月的川西"}) == 5


def test_a_note_without_a_readable_body_logs_zero() -> None:
    assert batch.body_chars({}) == 0
    assert batch.body_chars({"content": {"truncated": False}}) == 0
    assert batch.body_chars({"content": None}) == 0


# --- migrating a directory the old version wrote (migrate.py) -----------------

LEGACY_IMAGE = {
    "alt": None,
    "width": 1080,
    "height": 1440,
    "source": "img",
    "src": "https://sns-webpic-qc.xhscdn.com/20261006/aa/notes_pre_post/1040g3k8.webp",
}
LEGACY_DOWNLOAD = {
    "id": 1167,
    "url": LEGACY_IMAGE["src"],
    "state": "complete",
    "filename": "/tmp/notes/x/images/note-001.webp",
    "originalFilename": "/Users/me/Downloads/chrome-agent/note-001.webp",
}


def legacy_note_dir(tmp_path, note: str = NOTE, **overrides: object):
    """A note directory in the pre-v2 layout, media and all."""
    directory = tmp_path / note
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "capturedAt": "2026-10-06T15:17:31+00:00",
        "source": {
            "tabId": 2223711,
            "url": f"https://www.xiaohongshu.com/explore/{note}?xsec_token=token",
            "title": "川西的秋天 - 小红书",
        },
        "content": {"text": "川西的秋天", "length": 5, "truncated": False},
        "comments": {"items": [{"index": 1, "author": "旧", "text": "旧"}], "scope": "all"},
        "media": {"images": [LEGACY_IMAGE], "audioVideo": [], "downloads": [LEGACY_DOWNLOAD]},
        "warnings": ["评论仅包含当前 Web 页面已加载和已展开的范围"],
    }
    payload.update(overrides)
    (directory / "note.json").write_text(json.dumps(payload, ensure_ascii=False))
    return directory


def test_a_legacy_note_becomes_the_current_shape(tmp_path) -> None:
    """The pre-v2 file carried the media block and the tab source inside it."""
    directory = legacy_note_dir(tmp_path)

    result = migrate.migrate_note_dir(directory)

    assert result["migrated"] is True
    note_payload = json.loads((directory / "note.json").read_text())
    assert list(note_payload) == [
        "schemaVersion", "capturedAt", "noteId", "url", "title", "type", "author",
        "authorId", "ipLocation", "publishedAt", "stats", "tags", "content", "warnings",
    ]
    assert note_payload["content"] == "川西的秋天"
    assert note_payload["url"].endswith(f"/explore/{NOTE}?xsec_token=token")
    # The old title came from the browser tab; the current one is the note's h1.
    assert note_payload["title"] == "川西的秋天"
    # Decoded from the note ID, like a fresh collection would.
    assert note_payload["publishedAt"] == "2026-10-06T13:52:33+08:00"
    assert note_payload["stats"] == {
        "likes": None, "collects": None, "comments": None, "shares": None
    }
    # Fields the old collector never stored stay null, and say why.
    assert note_payload["author"] is None and note_payload["tags"] is None
    assert note_payload["warnings"] == [migrate.MIGRATION_WARNING]


def test_the_media_block_moves_to_downloads_json_unchanged(tmp_path) -> None:
    directory = legacy_note_dir(tmp_path)

    migrate.migrate_note_dir(directory)

    downloads = json.loads((directory / "downloads.json").read_text())
    assert downloads["images"] == [LEGACY_IMAGE]
    assert downloads["downloads"] == [LEGACY_DOWNLOAD]
    assert downloads["source"]["tabId"] == 2223711
    assert downloads["content"] == {"length": 5, "truncated": False}
    # Migration reads and writes JSON only: it moves no file on disk.
    assert not (directory / "images").exists()


def test_a_video_note_is_typed_video(tmp_path) -> None:
    """An empty `audioVideo` is the only thing that separates 图文 from 视频."""
    directory = legacy_note_dir(
        tmp_path, media={"images": [LEGACY_IMAGE], "audioVideo": [{"url": "https://e.com/v.mp4"}],
                         "downloads": [LEGACY_DOWNLOAD]},
    )

    migrate.migrate_note_dir(directory)

    assert json.loads((directory / "note.json").read_text())["type"] == "video"


def test_a_legacy_thread_moves_out_labelled_as_v1(tmp_path) -> None:
    """The old thread has no reply tree, so it must not claim the current shape."""
    directory = legacy_note_dir(tmp_path)

    migrate.migrate_note_dir(directory)

    thread = json.loads((directory / "comments.json").read_text())
    assert thread["schemaVersion"] == 1
    assert thread["items"] == [{"index": 1, "author": "旧", "text": "旧"}]
    assert thread["scope"] == "all"
    # The note's comment diagnostic describes the thread, so it travels with it.
    assert thread["warnings"] == [migrate.COMMENTS_MIGRATION_WARNING,
                                 "评论仅包含当前 Web 页面已加载和已展开的范围"]


def test_a_fresher_comments_file_wins_over_the_stale_block(tmp_path) -> None:
    directory = legacy_note_dir(tmp_path)
    fresh = {"schemaVersion": 2, "noteId": NOTE, "items": [], "collected": 0}
    (directory / "comments.json").write_text(json.dumps(fresh, ensure_ascii=False))

    result = migrate.migrate_note_dir(directory)

    assert json.loads((directory / "comments.json").read_text()) == fresh
    assert any("丢弃" in action for action in result["actions"])
    assert "comments" not in json.loads((directory / "note.json").read_text())


def test_migrating_a_current_directory_changes_nothing(tmp_path) -> None:
    directory = legacy_note_dir(tmp_path)
    migrate.migrate_note_dir(directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}

    result = migrate.migrate_note_dir(directory)

    assert result["migrated"] is False
    after = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    assert before == after
