#!/usr/bin/env python3
"""Collect the requested content parts of one open Xiaohongshu note.

The orchestrator, and nothing more: page semantics live in `locators.yaml`,
reading lives in `note.py` / `comments.py` / `media.py`. `download.py` drives it
one note at a time; it has no command line of its own, so there is exactly one
way to ask for a download.

A note directory holds, per content part:

* `note.json`     — the note itself: title, author, times, counts, tags, body;
* `cover.<ext>`   — the first picture, beside `images/` rather than inside it;
* `images/`       — the gallery, with `downloads.json` recording what landed;
* `videos/`       — the video, likewise;
* `comments.json` — the thread, plus how complete the read is known to be;
* `downloads.json` — every media URL seen, its state, and its file.

**The parts are independent.** Each one is a file an agent can check for, and
each answers "have I got this?" on its own — which is why the cover is not
stored inside `images/`, and why asking for one part never disturbs another.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable

from comments import collect_comments, write_comments
from media import (
    KIND_IMAGE,
    KIND_VIDEO,
    MEDIA_KINDS,
    collect_cover,
    collect_media,
    failed_downloads,
    write_downloads,
)
from note import read_content, read_meta, read_state_record, write_note
from runtime import comment_snapshot_limit, load_locators, snapshot, tab_source

NOTE_FILENAME = "note.json"
DOWNLOADS_FILENAME = "downloads.json"
SCHEMA_VERSION = 4

NOTE = "note"
COVER = "cover"
IMAGE = "image"
VIDEO = "video"
COMMENT = "comment"

# The order parts run in is not the order they are asked for. `comment` has to
# come last however it was spelled: scrolling the thread is what pushes the
# note's own containers out of a capped snapshot, so a part that reads them
# after it is reading a page that no longer holds them. The cover leads because
# it is the cheapest, and the note's own text is read before anything else.
PART_ORDER = (COVER, NOTE, IMAGE, VIDEO, COMMENT)

# What `--part all` means: everything. `image` and `video` are separate parts
# but share one discovery pass when both are asked for (see `collect_note`).
ALL_PARTS = (NOTE, COVER, IMAGE, VIDEO, COMMENT)


def normalize_parts(value: str | Iterable[str] | None = None) -> tuple[str, ...]:
    """Turn a `--part` value into the ordered parts to collect.

    Accepts one name, several comma-separated, the word `all`, or nothing at all
    (which means `all`). The result is always in PART_ORDER order, so
    `--part comment,image` still reads the thread last rather than doing what it
    says — a caller asking for both wants them both, and there is only one order
    in which that works.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        requested = set(ALL_PARTS)
    else:
        names = (
            {item.strip().lower() for item in value.split(",") if item.strip()}
            if isinstance(value, str)
            else {str(item).strip().lower() for item in value if str(item).strip()}
        )
        if "all" in names:
            requested = set(ALL_PARTS)
        else:
            unknown = names - set(PART_ORDER)
            if unknown:
                raise ValueError(
                    f"未知的 part: {', '.join(sorted(unknown))}；"
                    f"可用：{', '.join(PART_ORDER)}, all"
                )
            requested = names
    return tuple(part for part in PART_ORDER if part in requested)


def media_kinds(parts: Iterable[str]) -> tuple[str, ...]:
    """Which media kinds a set of parts asks for, in `MEDIA_KINDS` order."""
    requested = set(parts)
    return tuple(kind for kind in MEDIA_KINDS if kind in requested)


def existing_body_chars(output_path: Path) -> int:
    """The body length already recorded in this note's note.json, if any.

    A media-only run never reads the body, and `downloads.json`'s `content`
    block describes the pairing rather than this run — so it reports what is
    actually on disk instead of a zero that would read as an empty note.
    """
    if not output_path.is_file():
        return 0
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    content = payload.get("content")
    return len(content) if isinstance(content, str) else 0


def collect_note(
    tab_id: int,
    *,
    note_dir: Path,
    output_path: Path,
    parts: str | Iterable[str] | None = None,
    comment_limit: int | None = None,
    fallbacks: dict[str, str | None] | None = None,
    prefix: str = "xiaohongshu-note",
    comment_scrolls: int | None = None,
    known_files: dict[str, str] | None = None,
    force: bool = False,
) -> dict:
    """Collect the requested parts of the note open in `tab_id`.

    Every part takes its OWN snapshot, which is what makes the parts independent
    of each other and of the order they were asked for. Each snapshot still has
    to be taken before the thread is scrolled — the snapshot keeps the first N
    elements in on-screen order (content.js `buildSnapshot`), so what a long
    thread pushes past the ceiling is whatever sits further down that order:
    measured on note 6a6df219, a snapshot taken with the thread scrolled to its
    end returned y ∈ [-12073, -7956] at `--limit 500`, which still held the note
    body (y=-12037) but no longer held the carousel at y=32.

    `image` and `video` are separate parts but share ONE discovery pass when
    both are asked for: the media reading is one snapshot and one set of
    commands, and splitting them would open the same carousel twice.

    The files are written at the end rather than as each part finishes, because
    `note.json` reports the download problems `media` found and `downloads.json`
    reports the body length `note` found. A part asked for on its own writes
    only its own file.

    `force` re-downloads pictures that are already on disk; `known_files` is
    what makes them skippable in the first place. Both are passed straight
    through to `media.collect_media`.
    """
    fallbacks = dict(fallbacks or {})
    config = load_locators()
    requested = normalize_parts(parts)
    if comment_limit is None:
        comment_limit = config["scroll"]["comments"].get("limit", 10)
    if comment_scrolls is not None:
        config["scroll"]["comments"]["max_steps"] = comment_scrolls

    captured_at = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []
    note: dict | None = None
    content: str | None = None
    content_truncated = False
    comments: dict | None = None
    comment_warnings: list[str] = []

    if COVER in requested:
        cover = collect_cover(
            tab_id,
            config,
            note_dir=note_dir,
            prefix=prefix,
            fallback=fallbacks.get("image_media"),
        )
        warnings.extend(cover["warnings"])

    if NOTE in requested:
        page = snapshot(tab_id, comment_snapshot_limit(config))
        record = read_state_record(tab_id, page)
        content, content_truncated = read_content(
            tab_id, page, config, fallbacks.get("body"), record
        )
        if content_truncated:
            warnings.append("正文达到采集上限，可能不完整")
        note = {
            "schemaVersion": SCHEMA_VERSION,
            "capturedAt": captured_at,
            **read_meta(page, config, record),
            "content": content,
            "warnings": warnings,  # replaced with the final list before writing
        }

    kinds = media_kinds(requested)
    if kinds:
        page = snapshot(tab_id, comment_snapshot_limit(config))
        media = collect_media(
            tab_id,
            page,
            config,
            note_dir=note_dir,
            prefix=prefix,
            kinds=kinds,
            fallbacks=fallbacks,
            known_files=known_files,
            force=force,
        )
        warnings.extend(media["warnings"])
        # A failed download is a result the reader has to see; the per-item
        # detail — URL, state, filename — is in downloads.json.
        for item in failed_downloads(media["downloads"]):
            warnings.append(
                f"下载未完成：state={item.get('state')} {item.get('url', '')[:120]}"
            )
        write_downloads(
            note_dir / DOWNLOADS_FILENAME,
            {
                "schemaVersion": SCHEMA_VERSION,
                "capturedAt": captured_at,
                "source": tab_source(tab_id),
                "content": {
                    "length": (
                        len(content)
                        if content is not None
                        else existing_body_chars(output_path)
                    ),
                    "truncated": content_truncated,
                },
                "images": media["images"],
                "audioVideo": media["audioVideo"],
                "downloads": media["downloads"],
                "skipped": media["skipped"],
                # Which kinds the page gave an answer for. Without it, "no video
                # entries" cannot be told from "nobody looked for a video", and
                # `media.media_missing` would read a pictures-only run as proof
                # that the note has no video.
                "examined": media["examined"],
                "warnings": media["warnings"],
            },
        )

    if COMMENT in requested:
        comments, comment_warnings = collect_comments(
            tab_id, config, max(0, comment_limit), fallbacks.get("comments")
        )

    if note is not None:
        note["warnings"] = list(warnings)
        write_note(output_path, note)
    if comments is not None:
        write_comments(
            output_path,
            note_id_value=(note.get("noteId") if note else None) or note_dir.name,
            comments=comments,
            warnings=comment_warnings,
        )
    return note or {}


__all__ = [
    "ALL_PARTS",
    "COMMENT",
    "COVER",
    "DOWNLOADS_FILENAME",
    "IMAGE",
    "KIND_IMAGE",
    "KIND_VIDEO",
    "NOTE",
    "NOTE_FILENAME",
    "PART_ORDER",
    "SCHEMA_VERSION",
    "VIDEO",
    "collect_note",
    "existing_body_chars",
    "media_kinds",
    "normalize_parts",
]
