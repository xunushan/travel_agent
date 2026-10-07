#!/usr/bin/env python3
"""Rewrite a note directory an older version collected into today's contract.

Offline by design: every field the current `note.json` carries is either already
in the old file or derivable from it, so a migration never visits a page. The
old layout kept the media block, the tab source and the comment thread inside
`note.json`; this moves each to the file it belongs to now.

What cannot be recovered: the author, the IP location, the interaction counts and
the topic tags were never stored by the old collector, so they become null and
one warning says so. Reading them back needs the note's page, which is a
separate, deliberate pass.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from media import write_downloads
from note import published_at

NOTE_FILENAME = "note.json"
COMMENTS_FILENAME = "comments.json"
DOWNLOADS_FILENAME = "downloads.json"
SCHEMA_VERSION = 2

# Every title the old collector stored came from the browser tab and carries the
# site's own suffix (all 33 notes of one collection end with it), while today's
# `title` is the note's `h1`. Stripping the suffix is what makes the two agree.
TITLE_SUFFIX = " - 小红书"

MIGRATION_WARNING = (
    "本文件由旧格式（schemaVersion 1）就地迁移：作者/属地/互动数/标签当时未采集，"
    "字段为 null，需要重采才能补齐；正文、媒体与下载记录原样保留"
)
COMMENTS_MIGRATION_WARNING = (
    "本文件由旧格式迁移：评论树是 v1 结构（只有 index/author/text），"
    "没有嵌套回复与完整性字段，要升级需对这条笔记的评论区重采一次"
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def is_current(note: dict) -> bool:
    """Whether `note.json` is already in the shape `collect.py` writes now."""
    return (
        note.get("schemaVersion") == SCHEMA_VERSION
        and isinstance(note.get("content"), str)
        and "media" not in note
        and "source" not in note
    )


def is_comment_warning(warning: str) -> bool:
    """Whether a warning describes the comment read rather than the note."""
    return "评论" in warning or "展开回复" in warning


def migrate_comments(note_dir: Path, note: dict) -> str | None:
    """Move a thread embedded in `note.json` out to `comments.json`.

    Verbatim, and labelled `schemaVersion: 1`: the old thread has no reply tree
    and no completeness fields, so writing it as the current version would claim
    a shape it does not have. The note's comment diagnostics travel with it —
    they describe this thread, not the note.
    """
    block = note.get("comments")
    if not isinstance(block, dict):
        return None
    path = note_dir / COMMENTS_FILENAME
    if path.is_file():
        # A thread read after this block was written is the authority on the
        # note's comments; the stale block is superseded, not merged.
        return f"{COMMENTS_FILENAME} 更新，note.json 里的旧 comments 块已丢弃"
    write_json(
        path,
        {
            "schemaVersion": 1,
            "capturedAt": note.get("capturedAt"),
            "noteId": note.get("noteId") or note_dir.name,
            **block,
            "warnings": [
                COMMENTS_MIGRATION_WARNING,
                *[w for w in note.get("warnings", []) if is_comment_warning(w)],
            ],
        },
    )
    return None


def migrate_note_dir(note_dir: Path) -> dict:
    """Convert one note directory in place, and report what that took."""
    output_path = note_dir / NOTE_FILENAME
    note = read_json(output_path)
    if is_current(note):
        return {"noteId": note.get("noteId"), "migrated": False, "actions": []}

    actions: list[str] = []
    source = note.get("source") or {}
    media = note.get("media") or {}
    content = note.get("content")
    text = content if isinstance(content, str) else (content or {}).get("text") or ""
    note_id_value = note.get("noteId") or note_dir.name

    conflict = migrate_comments(note_dir, note)
    if conflict:
        actions.append(conflict)
    else:
        actions.append("评论移到 comments.json")

    # The old media block is byte-for-byte what downloads.json stores today
    # (same `images` / `audioVideo` / `downloads` items), so it moves as it is.
    write_downloads(
        note_dir / DOWNLOADS_FILENAME,
        {
            "schemaVersion": SCHEMA_VERSION,
            "capturedAt": note.get("capturedAt"),
            "source": source,
            "content": {
                "length": len(text),
                "truncated": bool((content or {}).get("truncated"))
                if isinstance(content, dict)
                else False,
            },
            "images": media.get("images", []),
            "audioVideo": media.get("audioVideo", []),
            "downloads": media.get("downloads", []),
            "warnings": media.get("warnings", []),
        },
    )
    actions.append("媒体与下载记录移到 downloads.json")

    title = note.get("title") or (source.get("title") or "").removesuffix(TITLE_SUFFIX)
    write_json(
        output_path,
        {
            "schemaVersion": SCHEMA_VERSION,
            "capturedAt": note.get("capturedAt"),
            "noteId": note_id_value,
            "url": note.get("url") or source.get("url") or "",
            "title": title,
            # The old collector only ever populated `audioVideo` for a note whose
            # detail page exposed a video player, so an empty list means 图文.
            "type": note.get("type") or ("video" if media.get("audioVideo") else "image"),
            "author": None,
            "authorId": None,
            "ipLocation": None,
            "publishedAt": note.get("publishedAt") or published_at(note_id_value),
            "stats": {"likes": None, "collects": None, "comments": None, "shares": None},
            "tags": None,
            "content": text,
            "warnings": [
                *[w for w in note.get("warnings", []) if not is_comment_warning(w)],
                MIGRATION_WARNING,
            ],
        },
    )
    actions.append("note.json 重写为新结构")
    return {"noteId": note_id_value, "migrated": True, "actions": actions}


def migrate_root(root: Path, only: set[str] | None = None) -> list[dict]:
    results = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if only and directory.name not in only:
            continue
        if not (directory / NOTE_FILENAME).is_file():
            continue
        results.append(migrate_note_dir(directory))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True, help="Directory of note folders")
    parser.add_argument("--only", help="Comma-separated noteIds to restrict the run to")
    parser.add_argument("--dry-run", action="store_true", help="Report without writing")
    args = parser.parse_args()

    only = {item.strip() for item in args.only.split(",") if item.strip()} if args.only else None
    if args.dry_run:
        for directory in sorted(p for p in args.root.iterdir() if p.is_dir()):
            path = directory / NOTE_FILENAME
            if path.is_file() and not is_current(read_json(path)):
                print(f"{directory.name} 需要迁移")
        return 0

    migrated = 0
    for result in migrate_root(args.root, only):
        if result["migrated"]:
            migrated += 1
        print(f"{result['noteId']} {'迁移' if result['migrated'] else '已是新结构'}")
    print(f"\n合计: {migrated} 篇迁移")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
