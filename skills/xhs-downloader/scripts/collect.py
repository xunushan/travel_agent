#!/usr/bin/env python3
"""Collect one open Xiaohongshu note into its three output files.

The orchestrator, and nothing more: page semantics live in `locators.yaml`,
reading lives in `note.py` / `comments.py` / `media.py`, and the CLI below wires
them to the command line.

Per note directory:

* `note.json`     — the note itself, shaped for an agent to read;
* `comments.json` — the thread, plus how complete the read is known to be;
* `downloads.json` — what was discovered and downloaded, for verification.

Each of those is produced by a **part** that can be asked for on its own —
`body`, `media`, `comments`, `cover` — because the two phases of a research run
want different ones. Phase 1 wants `body` and `cover` and nothing else: reading
the thread costs thirty scroll rounds per note and answers no screening question.
Phase 2 then wants `media` and `comments`, and usually finds the body already
there.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from comments import collect_comments, recapture_comments, write_comments
from media import collect_cover, collect_media, failed_downloads, write_downloads
from note import read_content, read_meta, read_state_record, write_note
from runtime import comment_snapshot_limit, load_locators, snapshot, tab_source

NOTE_FILENAME = "note.json"
DOWNLOADS_FILENAME = "downloads.json"
SCHEMA_VERSION = 3

COVER = "cover"
BODY = "body"
MEDIA = "media"
COMMENTS = "comments"

# The order parts run in is not the order they are asked for. `comments` has to
# come last however it was spelled: scrolling the thread is what pushes the
# note's own containers out of a capped snapshot, so a part that reads them
# after it is reading a page that no longer holds them. `cover` leads because it
# is the cheapest and the only one a screening run needs on its own.
PART_ORDER = (COVER, BODY, MEDIA, COMMENTS)

# What `--part all` means. Deliberately without `cover`: a cover exists to be
# looked at while screening, and the archive has the note's real images.
ALL_PARTS = (BODY, MEDIA, COMMENTS)


def normalize_parts(value: str | None = None) -> tuple[str, ...]:
    """Turn `--part` into the ordered parts to run.

    Accepts one name, several comma-separated, the word `all`, or nothing at all
    (which means `all`). The result is always in PART_ORDER order, so
    `--part comments,media` still reads the thread last rather than doing what it
    says — a caller asking for both wants them both, and there is only one order
    in which that works.
    """
    if value is None or not value.strip():
        requested = set(ALL_PARTS)
    else:
        names = {item.strip().lower() for item in value.split(",") if item.strip()}
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
    parts: str | tuple[str, ...] | None = None,
    comment_limit: int | None = None,
    fallbacks: dict[str, str | None] | None = None,
    download_images: bool = False,
    download_media: bool = False,
    prefix: str = "xiaohongshu-note",
    with_comments: bool = True,
    comment_scrolls: int | None = None,
    skip_urls: set[str] | None = None,
    facts: dict | None = None,
) -> dict:
    """Collect the requested parts of the note open in `tab_id`.

    Shared by the CLI and the unattended batch runner so both resolve refs,
    scroll comments and download media through exactly one code path.

    Every part takes its OWN snapshot, which is what makes the parts independent
    of each other and of the order they were asked for. Each snapshot still has
    to be taken before the thread is scrolled — the snapshot keeps the first N
    elements in on-screen order (content.js `buildSnapshot`), so what a long
    thread pushes past the ceiling is whatever sits further down that order:
    measured on note 6a6df219, a snapshot taken with the thread scrolled to its
    end returned y ∈ [-12073, -7956] at `--limit 500`, which still held the note
    body (y=-12037) but no longer held the carousel at y=32.

    The three files are written at the end rather than as each part finishes,
    because `note.json` reports the download problems `media` found and
    `downloads.json` reports the body length `body` found. A part asked for on
    its own writes only its own file.

    `facts`, when given, is filled with what the parts learned that is NOT note
    content and so is in none of the three files: which URL the cover came from,
    where it landed, and every picture URL the carousel exposed. Phase one
    records those in the index — it must know the whole picture set, or a later
    run that downloads pictures would read all but the cover as newly added.
    Passing it is optional because phase two has no use for any of it.
    """
    fallbacks = dict(fallbacks or {})
    config = load_locators()
    requested = normalize_parts(
        parts if parts is None or isinstance(parts, str) else ",".join(parts)
    )
    if not with_comments:
        requested = tuple(part for part in requested if part != COMMENTS)
    if comment_limit is None:
        comment_limit = config["scroll"]["comments"].get("limit", 10)
    if comment_scrolls is not None:
        config["scroll"]["comments"]["max_steps"] = comment_scrolls

    captured_at = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []
    note: dict | None = None
    content: str | None = None
    content_truncated = False
    media: dict | None = None
    comments: dict | None = None
    comment_warnings: list[str] = []

    for part in requested:
        if part == COVER:
            cover = collect_cover(
                tab_id,
                config,
                note_dir=note_dir,
                prefix=prefix,
                fallback=fallbacks.get("image_media"),
            )
            warnings.extend(cover["warnings"])
            if facts is not None:
                facts["coverSrc"] = cover["src"]
                facts["imageUrls"] = [image["src"] for image in cover["images"]]
                facts["coverFilename"] = next(
                    (
                        item["filename"]
                        for item in cover["downloads"]
                        if item.get("filename") and item.get("state") == "complete"
                    ),
                    None,
                )

        elif part == BODY:
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

        elif part == MEDIA:
            page = snapshot(tab_id, comment_snapshot_limit(config))
            media = collect_media(
                tab_id,
                page,
                config,
                note_dir=note_dir,
                prefix=prefix,
                download_images=download_images,
                download_media=download_media,
                fallbacks=fallbacks,
                skip_urls=skip_urls,
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
                    "warnings": media["warnings"],
                },
            )

        elif part == COMMENTS:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab-id", required=True, type=int)
    parser.add_argument(
        "--part",
        default=None,
        help="Comma-separated: body, media, comments, cover, or all. "
        "Defaults to all (body, media, comments). Each part takes its own "
        "snapshot and writes only its own file.",
    )
    parser.add_argument(
        "--body-ref", help="Optional fallback; normally resolved from locators.yaml"
    )
    parser.add_argument(
        "--comments-ref", help="Optional fallback; normally resolved from locators.yaml"
    )
    parser.add_argument(
        "--images-ref", help="Optional fallback; normally resolved from locators.yaml"
    )
    parser.add_argument(
        "--media-ref", help="Optional fallback; normally resolved from locators.yaml"
    )
    parser.add_argument(
        "--comment-scrolls", type=int, help="Override locators.yaml comment scroll limit"
    )
    parser.add_argument(
        "--comments",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to read the comment thread at all (default: yes)",
    )
    parser.add_argument(
        "--comment-limit",
        type=int,
        help="Top-level comments to read; a comment and its replies count as one. "
        "Defaults to locators.yaml scroll.comments.limit (10)",
    )
    parser.add_argument(
        "--comments-only",
        action="store_true",
        help="Alias for --part comments: refresh only the comment section, "
        "leaving already-downloaded media untouched",
    )
    parser.add_argument("--download-images", action="store_true")
    parser.add_argument("--download-media", action="store_true")
    parser.add_argument("--prefix", default="xiaohongshu-note")
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        "--output", type=Path, help="JSON output path (legacy mode; media goes beside it)"
    )
    output_group.add_argument(
        "--output-dir", type=Path, help="Note directory; writes note.json plus images/ and videos/"
    )
    args = parser.parse_args()

    note_dir = args.output_dir or args.output.parent
    output_path = (args.output_dir / NOTE_FILENAME) if args.output_dir else args.output

    if args.comments_only:
        recapture_comments(
            args.tab_id,
            output_path=output_path,
            comment_limit=args.comment_limit,
            fallback=args.comments_ref,
            comment_scrolls=args.comment_scrolls,
        )
        print(output_path)
        return 0

    parts = normalize_parts(args.part)
    collect_note(
        args.tab_id,
        note_dir=note_dir,
        output_path=output_path,
        parts=parts,
        comment_limit=args.comment_limit,
        fallbacks={
            "body": args.body_ref,
            "comments": args.comments_ref,
            "image_media": args.images_ref,
            "video_media": args.media_ref,
        },
        download_images=args.download_images,
        download_media=args.download_media,
        prefix=args.prefix,
        with_comments=args.comments,
        comment_scrolls=args.comment_scrolls,
    )
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
