#!/usr/bin/env python3
"""Read the note itself — metadata and body — and write `note.json`.

Two sources, in a fixed order. The page's server-rendered state (`state.py`) is
preferred because it carries fields the DOM never prints — `lastUpdateTime`
above all, which is the evidence for deciding whether a stored note is stale.
Everything it does not answer falls back to the DOM through `locators.yaml`, and
`capturedFrom` records which source the metadata block came from so a reader can
tell a thin read from a note that really is thin.

The file is shaped for an agent to read, so it carries the note and nothing
about how it was collected (that goes to `downloads.json`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime import contains_rect, extract_text, find_all, find_first, note_id
from state import note_record, read_initial_state

# The footer prints "发布时间 [IP属地]" as one string, in one of four shapes
# measured live on 2026-10-07: "08-01", "09-29 河南", "2025-11-03" (older notes
# carry the year) and "4天前 西藏"; a note edited after publishing prints
# "编辑于 2025-09-24" instead. The place is optional — 6a6df219 printed the date
# alone — so an unmatched tail is reported as unknown, never guessed.
FOOTER_ROW = re.compile(
    r"^(?:编辑于\s*)?(?P<date>"
    r"\d{4}-\d{2}-\d{2}"
    r"|\d{2}-\d{2}"
    r"|\d+\s*(?:秒|分钟|分|小时|天|周|个月)前"
    r"|刚刚|今天|昨天|前天"
    r")\s*(?P<place>.*)$"
)

# Counts print as a bare number, or as "1.2万" once they pass five digits.
COUNT = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>万|w|W)?")

# A note ID's first eight hex digits are its creation time, and two independent
# checks agree: 6a1cf8f9 decodes to 2026-06-01, the publish date the reference
# scrape recorded for that note, and 6a6df219 decodes to 2026-08-01, the date
# that note's own footer prints as "08-01".
NOTE_ID_TIME_CHARS = 8
CHINA_TZ = timezone(timedelta(hours=8))

# The state's `time` and `lastUpdateTime` are milliseconds since the epoch;
# decoding one measured live (1785590297000) lands on 2026-08-01T21:18:17+08:00,
# which is the same instant as the seconds-precision stamp in the note's own ID.
EPOCH_MS = 1000

PROFILE_RE = re.compile(r"/user/profile/([0-9a-f]+)")


def parse_count(text: str | None) -> int | None:
    """Read an interaction count, or None when the page prints none.

    Xiaohongshu writes zero as the bare word ("赞"), which is a real zero rather
    than missing data once the element itself was found.
    """
    if text is None:
        return None
    match = COUNT.search(text)
    if not match:
        return 0
    number = float(match.group("number"))
    if match.group("unit"):
        number *= 10000
    return int(number)


def published_at(note: str | None) -> str | None:
    """The note's publish time, decoded from its ID.

    The page only ever prints a partial date ("08-01", "4天前"), which cannot be
    placed on a calendar on its own, so the timestamp the ID carries is what gets
    stored. Returns None for an ID that is not hexadecimal.
    """
    if not note:
        return None
    try:
        stamp = int(note[:NOTE_ID_TIME_CHARS], 16)
    except ValueError:
        return None
    return datetime.fromtimestamp(stamp, CHINA_TZ).isoformat()


def from_epoch_ms(value) -> str | None:
    """A state timestamp — milliseconds since the epoch — as ISO-8601 in +08:00.

    Negative and zero stamps are rejected rather than rendered as 1970: the state
    uses 0 for "never", which would otherwise read as a real edit time.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return datetime.fromtimestamp(number / EPOCH_MS, CHINA_TZ).isoformat()


def state_count(value) -> int | None:
    """An interaction count from the state, or None when the note has none.

    Counts arrive as strings — `{"likedCount": "2346", "niceCount": ""}` — and
    the empty string is the state's way of saying the page printed nothing, so it
    is null here rather than `parse_count`'s zero.
    """
    if value is None or str(value) == "":
        return None
    return parse_count(str(value))


def record_type(record: dict) -> str:
    """The note kind, as note.json's two-value `type`.

    The state says `normal` for a picture note (measured on 6a6df219) and
    `video` for a video one; the presence of a `video` block is the fallback
    signal for a build that words the type differently.
    """
    if str(record.get("type") or "").lower() == "video" or record.get("video"):
        return "video"
    return "image"


def record_tags(record: dict) -> list[str]:
    """The note's topic names, from the state's `tagList`.

    Entries are objects (`{"id":…, "name":…, "type":"topic"}`); a bare string is
    accepted too so a shape change degrades instead of crashing.
    """
    tags: list[str] = []
    for item in record.get("tagList") or []:
        name = (item or {}).get("name") if isinstance(item, dict) else item
        name = (name or "").strip()
        if name and name not in tags:
            tags.append(name)
    return tags


def record_images(record: dict) -> list[dict]:
    """The note's pictures in the note's own order, as `{"src", "width", "height"}`.

    The state's `imageList[].url` is an EMPTY STRING on every image measured
    live; the address is `urlDefault`, with `urlPre` — and the `infoList` scene
    variants — behind it. Those URLs carry a date segment in the path, so they
    are read as a pointer to which image is which, never stored as a download
    source.
    """
    images: list[dict] = []
    for item in record.get("imageList") or []:
        if not isinstance(item, dict):
            continue
        source = item.get("url") or item.get("urlDefault") or item.get("urlPre")
        if not source:
            continue
        images.append(
            {
                "src": source,
                "width": _int_or_none(item.get("width")),
                "height": _int_or_none(item.get("height")),
            }
        )
    return images


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def read_state_record(tab_id: int, page: dict) -> dict | None:
    """The state's record for the note open in `tab_id`, if the page has one.

    This is the note's own entry in `note.noteDetailMap`, keyed by the ID in the
    page URL. It answers None on a page that ships no state, on an in-page
    navigation that never re-injected one, and on a state that will not parse —
    all three of which `read_meta` then covers from the DOM.
    """
    return note_record(read_initial_state(tab_id, page), note_id(page.get("url")))


def meta_from_state(record: dict, url: str) -> dict:
    """Map a state record onto note.json's note-level fields.

    `ipLocation` is missing on purpose: the state does not carry it, so the
    caller fills it from the footer the way it always has.
    """
    user = record.get("user") or {}
    info = record.get("interactInfo") or {}
    note = record.get("noteId") or note_id(url)
    return {
        "noteId": note,
        "url": url,
        "title": (record.get("title") or "").strip(),
        "type": record_type(record),
        "author": (user.get("nickname") or "").strip(),
        "authorId": user.get("userId"),
        # The state's own `time` is the authority; the ID decode is the fallback
        # for an entry that omits it.
        "publishedAt": from_epoch_ms(record.get("time")) or published_at(note),
        "updatedAt": from_epoch_ms(record.get("lastUpdateTime")),
        "stats": {
            "likes": state_count(info.get("likedCount")),
            "collects": state_count(info.get("collectedCount")),
            "comments": state_count(info.get("commentCount")),
            # Present here and absent from the DOM, which is why the collect
            # path used to store null on every note.
            "shares": state_count(info.get("shareCount")),
        },
        "tags": record_tags(record),
    }


def scoped(page: dict, rules: dict, name: str, scope_name: str | None) -> list[dict]:
    """Every element matching `name`, narrowed to inside `scope_name` if given.

    Comment rows reuse the note's own class names (`like-wrapper`, `a.name`), so
    a rule that is not scoped by rectangle picks up a comment's numbers or its
    author instead of the note's.
    """
    found = find_all(page, rules[name])
    if not scope_name:
        return found
    scope = find_first(page, rules[scope_name])
    if not scope:
        return found
    return [element for element in found if contains_rect(scope, element)]


def first_text(page: dict, rules: dict, name: str, scope_name: str | None = None) -> str:
    for element in scoped(page, rules, name, scope_name):
        text = (element.get("text") or "").strip()
        if text:
            return text
    return ""


def read_stats(page: dict, rules: dict) -> dict:
    """The note's likes / collects / comments / shares.

    All four keys are always present. Xiaohongshu's web detail view has no share
    element at all — measured on 6a6df219, whose bar carries exactly
    `like-wrapper 2341`, `collect-wrapper 3159` and `chat-wrapper 610` — so
    `shares` stays null there rather than being filled with a guess.
    """
    stats: dict[str, int | None] = {}
    for key, rule in (
        ("likes", "like_count"),
        ("collects", "collect_count"),
        ("comments", "comment_count"),
        ("shares", "share_count"),
    ):
        found = scoped(page, rules, rule, "engage_bar")
        stats[key] = parse_count(found[0].get("text")) if found else None
    return stats


def read_tags(page: dict, rules: dict) -> list[str]:
    """The note's topic tags, without the `#`/`[话题]` the page prints on them."""
    tags = []
    for element in scoped(page, rules, "note_tags", "note_content"):
        text = (element.get("text") or "").strip().strip("#").strip()
        text = text.removesuffix("[话题]").strip()
        if text and text not in tags:
            tags.append(text)
    return tags


def read_author(page: dict, rules: dict) -> tuple[str, str | None]:
    """The note author's name and profile id."""
    found = scoped(page, rules, "note_author", "note_author_scope")
    if not found:
        return "", None
    element = found[0]
    profile = PROFILE_RE.search(element.get("href") or "")
    return (element.get("text") or "").strip(), profile.group(1) if profile else None


def read_footer(page: dict, rules: dict) -> tuple[str | None, str | None]:
    """Split the footer row into `(printed date, IP location)`.

    The location is what the page prints after the date, so a note that prints
    none — 6a6df219 shows "08-01" — yields None, and the caller stores null.
    """
    text = first_text(page, rules, "note_date", "note_footer")
    match = FOOTER_ROW.match(text)
    if not match:
        return (text or None), None
    place = match.group("place").strip()
    return match.group("date"), (place or None)


def read_meta(page: dict, config: dict, record: dict | None = None) -> dict:
    """Every note-level field `note.json` carries, from one snapshot.

    With a `record` — the state's entry for this note — the fields it answers
    come from there and only the IP location is read from the DOM. Without one
    the whole block is read from the DOM as it always was, and `updatedAt` is
    null: the page prints the day it was edited ("编辑于 2025-09-24") but never
    the second, so the DOM cannot supply the field either.

    A field neither source prints is null rather than guessed at.
    """
    rules = config["detail"]
    _date, place = read_footer(page, rules)
    if record:
        return {
            **meta_from_state(record, page.get("url", "")),
            "ipLocation": place,
            "capturedFrom": "state",
        }
    author, author_id = read_author(page, rules)
    return {
        "noteId": note_id(page.get("url")),
        "url": page.get("url", ""),
        "title": first_text(page, rules, "note_title"),
        "type": "video" if find_first(page, rules["video_media"]) else "image",
        "author": author,
        "authorId": author_id,
        "ipLocation": place,
        "publishedAt": published_at(note_id(page.get("url"))),
        "updatedAt": None,
        "stats": read_stats(page, rules),
        "tags": read_tags(page, rules),
        "capturedFrom": "dom",
    }


def read_content(
    tab_id: int,
    page: dict,
    config: dict,
    fallback: str | None,
    record: dict | None = None,
) -> tuple[str, bool]:
    """The note body as plain text, and whether the read hit its ceiling.

    The DOM is the primary source, because its text is what the note renders as;
    the state's `desc` is the fallback rather than the other way round, and that
    is deliberate — `desc` prints topic markers the page hides (`#川西[话题]#`
    against the rendered `#川西`), so preferring it would change what `content`
    means for every note.

    It still earns its place as a fallback: reading it needs no element ref, so
    it is what saves the body when a crowded snapshot has pushed the note's own
    containers out of reach.
    """
    element = find_first(page, config["detail"]["body"])
    ref = element["ref"] if element else fallback
    if ref:
        content = extract_text(tab_id, ref, 20000)
        if content["text"].strip():
            return content["text"], content["truncated"]
    desc = ((record or {}).get("desc") or "").strip()
    if desc:
        return desc, False
    if not ref:
        raise RuntimeError("未从 locators.yaml 或 --body-ref 找到正文容器")
    return "", False


def write_note(path: Path, note: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(note, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
