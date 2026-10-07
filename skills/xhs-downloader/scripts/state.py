#!/usr/bin/env python3
"""Read Xiaohongshu's server-rendered state off the page.

The site ships its whole store as one `<script>` whose text is
`window.__INITIAL_STATE__={...}`, and the state carries things the DOM never
prints — most importantly `lastUpdateTime`, which is the primary evidence for
deciding whether a stored note is out of date. `chrome-agent` has no JS
evaluation, but the script element is reachable as DOM text, so the store can be
read with `page text`.

Three properties of that blob shape everything here:

* **It is frozen at document load.** A note opened by clicking a result card
  does not re-inject it — measured on a live search page, `search.feeds` stays
  `[]` and `note.noteDetailMap` stays `{}` after in-page navigation. Anything
  read from here requires a real `tabs navigate`.
* **It is a JS object literal, not JSON.** `undefined` appears bare (the first
  one on note 6a6df219 sits at `"pwaAddDesktopPrompt":undefined`), so it is
  normalised before parsing.
* **The marker sits near the start of the element's text** (measured: index 0 of
  `window.__INITIAL_STATE__={`), so a 200-character snapshot excerpt is enough
  to identify the right element and only one `page text` call is needed.

Only extraction lives here — the mapping to `note.json` fields is in `note.py`,
so this module stays a faithful view of what the page actually said.
"""

from __future__ import annotations

import json

from runtime import extract_text, snapshot

STATE_MARKER = "__INITIAL_STATE__"
STATE_MAX_CHARS = 200_000

# A script element's snapshot text is capped at 200 characters. The marker is at
# the start of the state element, so this normally identifies it outright; the
# fallback below exists for a build that prepends something long.
MARKER_EXCERPT_CHARS = 200
SCRIPT_FALLBACK_READS = 12

# Bare JS literals that are not JSON. `-Infinity` has to be listed before
# `Infinity` is unreachable, which is why the scan below matches whole tokens
# rather than searching for substrings.
JS_LITERALS = {
    "undefined": "null",
    "NaN": "null",
    "Infinity": "null",
    "-Infinity": "null",
}

_IDENTIFIER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$.-")
_QUOTES = "\"'"


def _js_literal_to_json(text: str) -> str:
    """Turn the state blob into strict JSON by nulling its bare JS literals.

    String-aware on purpose: `"undefined"` and `'undefined'` are data, and a
    naive `str.replace` would corrupt them. Quotes are tracked as they open and
    close so an escaped quote does not end a string early.
    """
    start = text.find("{")
    if start < 0:
        raise ValueError("状态文本里没有对象的起始花括号")
    body = text[start:]
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(body):
        char = body[index]
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(body):
                output.append(body[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in _QUOTES:
            quote = char
            output.append(char)
            index += 1
            continue
        if char in _IDENTIFIER_CHARS:
            end = index
            while end < len(body) and body[end] in _IDENTIFIER_CHARS:
                end += 1
            token = body[index:end]
            output.append(JS_LITERALS.get(token, token))
            index = end
            continue
        output.append(char)
        index += 1
    return "".join(output)


def parse_state(text: str) -> dict:
    """Parse the state blob, ignoring whatever the site appends after it."""
    # `raw_decode` rather than `loads`: the assignment can be followed by more
    # statements, and `loads` rejects trailing data outright.
    value, _end = json.JSONDecoder().raw_decode(_js_literal_to_json(text).lstrip())
    if not isinstance(value, dict):
        raise ValueError("状态文本解析出来不是对象")
    return value


def state_script(page: dict) -> dict | None:
    """The snapshot element holding the state, if the page exposes one."""
    scripts = [
        element for element in page.get("elements", []) if element.get("tag") == "script"
    ]
    for element in scripts:
        if STATE_MARKER in (element.get("text") or ""):
            return element
    return scripts[0] if len(scripts) == 1 else None


def read_initial_state(tab_id: int, page: dict | None = None) -> dict | None:
    """The page's state store, or None when the page does not carry one.

    Returns None rather than raising: a note whose state cannot be read is still
    collectable from the DOM, and `note.py` falls back field by field. The
    caller can see which source won through `capturedFrom`.
    """
    page = page if page is not None else snapshot(tab_id)
    element = state_script(page)
    if element is None:
        return None
    text = extract_text(tab_id, element["ref"], STATE_MAX_CHARS)["text"]
    if not text.strip():
        return None
    try:
        return parse_state(text)
    except (ValueError, json.JSONDecodeError):
        return None


def note_records(state: dict | None) -> dict:
    """Every note the page's state carries, keyed by note ID.

    On a note detail page this holds exactly one entry — the note that was
    navigated to, and no other.
    """
    if not state:
        return {}
    detail = state.get("note") or {}
    return detail.get("noteDetailMap") or {}


def note_record(state: dict | None, note_id: str | None = None) -> dict | None:
    """The state's record for one note.

    With no `note_id` the sole entry is used, which is what a detail page has;
    a page carrying several notes and no requested ID answers None rather than
    picking one arbitrarily.
    """
    records = note_records(state)
    if note_id is not None:
        entry = records.get(note_id) or {}
    elif len(records) == 1:
        entry = next(iter(records.values()))
    else:
        return None
    return (entry or {}).get("note") or None
