#!/usr/bin/env python3
"""Collect one open Xiaohongshu note into its three output files.

The orchestrator, and nothing more: page semantics live in `locators.yaml`,
reading lives in `note.py` / `comments.py` / `media.py`, and the CLI below wires
them to the command line.

Per note directory:

* `note.json`     — the note itself, shaped for an agent to read;
* `comments.json` — the thread, plus how complete the read is known to be;
* `downloads.json` — what was discovered and downloaded, for verification.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from comments import collect_comments, recapture_comments, write_comments
from media import collect_media, failed_downloads, write_downloads
from note import read_content, read_meta, write_note
from runtime import comment_snapshot_limit, load_locators, snapshot, tab_source

NOTE_FILENAME = "note.json"
DOWNLOADS_FILENAME = "downloads.json"
SCHEMA_VERSION = 2


def collect_note(
    tab_id: int,
    *,
    note_dir: Path,
    output_path: Path,
    comment_limit: int | None = None,
    fallbacks: dict[str, str | None] | None = None,
    download_images: bool = False,
    download_media: bool = False,
    prefix: str = "xiaohongshu-note",
    with_comments: bool = True,
    comment_scrolls: int | None = None,
) -> dict:
    """Collect the note currently open in `tab_id` and write its files.

    Shared by the CLI and the unattended batch runner so both resolve refs,
    scroll comments and download media through exactly one code path.

    Every note-level ref — body, media, metadata — is resolved from ONE snapshot
    taken right after the note opens, because the comment thread is read next
    and brings thousands of elements with it. The snapshot keeps the first N
    elements in on-screen order (content.js `buildSnapshot`), so what a long
    thread pushes past the ceiling is whatever sits further down that order:
    measured on note 6a6df219, a snapshot taken with the thread scrolled to its
    end returned y ∈ [-12073, -7956] at `--limit 500`, which still held the note
    body (y=-12037) but no longer held the carousel at y=32.
    """
    fallbacks = dict(fallbacks or {})
    config = load_locators()
    if comment_limit is None:
        comment_limit = config["scroll"]["comments"].get("limit", 10)
    if comment_scrolls is not None:
        config["scroll"]["comments"]["max_steps"] = comment_scrolls

    opening = snapshot(tab_id, comment_snapshot_limit(config))
    warnings: list[str] = []

    content, content_truncated = read_content(
        tab_id, opening, config, fallbacks.get("body")
    )
    if content_truncated:
        warnings.append("正文达到采集上限，可能不完整")
    meta = read_meta(opening, config)

    media = collect_media(
        tab_id,
        opening,
        config,
        note_dir=note_dir,
        prefix=prefix,
        download_images=download_images,
        download_media=download_media,
        fallbacks=fallbacks,
    )
    warnings.extend(media["warnings"])
    # A failed download is a result the reader has to see; the per-item detail —
    # URL, state, filename — is in downloads.json.
    for item in failed_downloads(media["downloads"]):
        warnings.append(f"下载未完成：state={item.get('state')} {item.get('url', '')[:120]}")

    comments: dict | None = None
    comment_warnings: list[str] = []
    if with_comments:
        comments, comment_warnings = collect_comments(
            tab_id, config, max(0, comment_limit), fallbacks.get("comments")
        )

    captured_at = datetime.now(timezone.utc).isoformat()
    note = {
        "schemaVersion": SCHEMA_VERSION,
        "capturedAt": captured_at,
        **meta,
        "content": content,
        "warnings": warnings,
    }
    write_note(output_path, note)
    write_downloads(
        note_dir / DOWNLOADS_FILENAME,
        {
            "schemaVersion": SCHEMA_VERSION,
            "capturedAt": captured_at,
            "source": tab_source(tab_id),
            "content": {"length": len(content), "truncated": content_truncated},
            "images": media["images"],
            "audioVideo": media["audioVideo"],
            "downloads": media["downloads"],
            "warnings": media["warnings"],
        },
    )
    if comments is not None:
        write_comments(
            output_path,
            note_id_value=meta["noteId"] or note_dir.name,
            comments=comments,
            warnings=comment_warnings,
        )
    return note


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab-id", required=True, type=int)
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
        help="Refresh only the comment section of an existing note.json, "
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

    collect_note(
        args.tab_id,
        note_dir=note_dir,
        output_path=output_path,
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
