"""Regression tests for reading Xiaohongshu's server-rendered state.

The blob is a JS object literal rather than JSON, and the whole reason this
module exists is that the difference is invisible until it isn't: the first bare
`undefined` (measured at `"pwaAddDesktopPrompt":undefined` on a live note) makes
`json.loads` fail 2,947 characters in. So the normaliser gets tested against the
shapes that break a naive string replace, and the reader against the pages that
have no state at all.
"""

from __future__ import annotations

import json

import pytest
import state
from test_playbook import FakeCommentBrowser, element, page, patch_browser

# Verbatim from a live note detail page on 2026-10-07: the assignment is followed
# by another statement, which is why the parse cannot use `json.loads`.
LIVE_BLOB = (
    'window.__INITIAL_STATE__={"note":{"noteDetailMap":{"6a6df2190000000025008e2b":'
    '{"note":{"noteId":"6a6df2190000000025008e2b","title":"为什么我总是推荐走省道434",'
    '"desc":"去了几十次高原","type":"normal","time":1785590297000,'
    '"lastUpdateTime":1785590297000,'
    '"interactInfo":{"likedCount":"2346","collectedCount":"3158",'
    '"commentCount":"610","shareCount":"1422","niceCount":""},'
    '"tagList":[{"id":"a","name":"自驾游旅游","type":"topic"}],'
    '"imageList":[{"url":"","urlDefault":"https://cdn/notes_pre_post/1040g3","width":"1440",'
    '"height":"2400"}],"user":{"userId":"5faf","nickname":"西部的海胖胖"}}}}},'
    '"pwaAddDesktopPrompt":undefined};window.__SSR__=true'
)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # The literal itself.
        ('{"a":undefined,"b":1}', {"a": None, "b": 1}),
        # A literal that is DATA, not syntax. A naive str.replace eats this.
        ('{"a":"undefined","b":undefined}', {"a": "undefined", "b": None}),
        # An escaped quote must not be mistaken for the end of the string.
        ('{"a":"he said \\"undefined\\"","b":undefined}',
         {"a": 'he said "undefined"', "b": None}),
        # A key that merely starts with the word is not the word.
        ('{"undefinedKey":1,"a":undefined}', {"undefinedKey": 1, "a": None}),
        ('{"a":NaN,"b":Infinity,"c":-Infinity}', {"a": None, "b": None, "c": None}),
        # Numbers must survive the token scan untouched, minus signs included.
        ('{"a":1.5e-3,"b":-1,"c":0}', {"a": 0.0015, "b": -1, "c": 0}),
        # Nested.
        ('{"a":{"b":[{"c":undefined}]}}', {"a": {"b": [{"c": None}]}}),
    ],
)
def test_js_literals_become_json_without_touching_strings(source, expected) -> None:
    assert json.loads(state._js_literal_to_json(source)) == expected


def test_parse_state_ignores_what_follows_the_assignment() -> None:
    """The real blob ends with `;window.__SSR__=true`, so trailing data is normal."""
    assert state.parse_state('window.__INITIAL_STATE__={"a":1};window.__SSR__=true') == {
        "a": 1
    }


def test_parse_state_refuses_text_with_no_object() -> None:
    with pytest.raises(ValueError):
        state.parse_state("no object here")


def test_the_live_blob_parses_and_carries_the_edit_time() -> None:
    record = state.note_record(state.parse_state(LIVE_BLOB))

    assert record["title"] == "为什么我总是推荐走省道434"
    assert record["lastUpdateTime"] == 1785590297000


def test_note_record_picks_by_id_and_refuses_to_guess() -> None:
    parsed = state.parse_state(LIVE_BLOB)
    note = "6a6df2190000000025008e2b"

    # No ID asks for the sole entry, which is what a detail page always has.
    assert state.note_record(parsed)["noteId"] == note
    assert state.note_record(parsed, note)["noteId"] == note
    assert state.note_record(parsed, "0" * 24) is None


def test_note_record_does_not_guess_between_several_notes() -> None:
    """A page carrying more than one note has no defensible default."""
    parsed = {"note": {"noteDetailMap": {"a": {"note": {"noteId": "a"}},
                                         "b": {"note": {"noteId": "b"}}}}}

    assert state.note_record(parsed) is None
    assert state.note_record(parsed, "b")["noteId"] == "b"


def test_a_page_without_a_state_answers_none(monkeypatch) -> None:
    """A search page ships a state; a page with no script at all does not."""
    browser = FakeCommentBrowser(page("https://www.xiaohongshu.com/explore/" + "a" * 24), {})
    patch_browser(monkeypatch, browser)

    assert state.read_initial_state(1) is None


def test_the_script_element_is_found_by_its_marker_not_its_position(monkeypatch) -> None:
    """Several <script> elements are in the snapshot; only one holds the store."""
    blob = 'window.__INITIAL_STATE__={"a":1}'
    browser = FakeCommentBrowser(
        page(
            "https://www.xiaohongshu.com/explore/" + "a" * 24,
            element("s1", tag="script", text="!function(e,r){try{var n=\"__FST__\""),
            element("s2", tag="script", text=blob[:200]),
        ),
        {"s2": blob},
    )
    patch_browser(monkeypatch, browser)

    assert state.read_initial_state(1) == {"a": 1}
    # One read, not one per script: the marker is inside the snapshot excerpt.
    assert browser.methods().count("page.text") == 1


def test_an_unparseable_state_answers_none_instead_of_raising(monkeypatch) -> None:
    """A read that fails must not sink a collection the DOM could still do."""
    browser = FakeCommentBrowser(
        page(
            "https://www.xiaohongshu.com/explore/" + "a" * 24,
            element("s", tag="script", text="window.__INITIAL_STATE__={"),
        ),
        {"s": "window.__INITIAL_STATE__={oops"},
    )
    patch_browser(monkeypatch, browser)

    assert state.read_initial_state(1) is None
