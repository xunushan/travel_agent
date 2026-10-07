#!/usr/bin/env python3
"""Read the note itself — metadata and body — and write `note.json`.

Everything here comes from the detail page's DOM and is driven by
`locators.yaml`; the file is shaped for an agent to read, so it carries the note
and nothing about how it was collected (that goes to `downloads.json`).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime import contains_rect, extract_text, find_all, find_first, note_id

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


def read_meta(page: dict, config: dict) -> dict:
    """Every note-level field `note.json` carries, from one snapshot.

    A field the page does not print is null: the note's IP location and share
    count are genuinely absent on some notes.
    """
    rules = config["detail"]
    author, author_id = read_author(page, rules)
    _date, place = read_footer(page, rules)
    return {
        "noteId": note_id(page.get("url")),
        "url": page.get("url", ""),
        "title": first_text(page, rules, "note_title"),
        "type": "video" if find_first(page, rules["video_media"]) else "image",
        "author": author,
        "authorId": author_id,
        "ipLocation": place,
        "publishedAt": published_at(note_id(page.get("url"))),
        "stats": read_stats(page, rules),
        "tags": read_tags(page, rules),
    }


def read_content(
    tab_id: int, page: dict, config: dict, fallback: str | None
) -> tuple[str, bool]:
    """The note body as plain text, and whether the read hit its ceiling."""
    element = find_first(page, config["detail"]["body"])
    ref = element["ref"] if element else fallback
    if not ref:
        raise RuntimeError("未从 locators.yaml 或 --body-ref 找到正文容器")
    content = extract_text(tab_id, ref, 20000)
    return content["text"], content["truncated"]


def write_note(path: Path, note: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(note, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
