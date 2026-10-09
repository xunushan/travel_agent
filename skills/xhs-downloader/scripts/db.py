#!/usr/bin/env python3
"""The note index, over sqlite — one table, and nothing that is already in a file.

**This is not where notes live.** The note itself is `note.json`, the thread is
`comments.json`, what was downloaded is `downloads.json` — all on disk, readable
without this module. Whether a note has been downloaded is answered by those
files (see `download.downloaded_parts`), never by a flag here.

What the table holds is the three things a file cannot answer cheaply:

* **have we seen this note at all** — so a re-run does not open the same note's
  page again just to learn it is already on disk;
* **where its directory is** — the directory is named `<note_id>_<title>`, so it
  is not derivable from the id alone;
* **which version we last read** (`updated_at`, `content_hash`) — the memory
  that makes "did it change?" answerable at all, once a page has been opened.

Delete this file and no note is lost: every row is rebuildable from `note.json`
and a re-read of the page. That is deliberate — it is a cache of what has been
opened, not an asset.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

# The database's own shape, independent of the note files'. `note-output`'s
# schemaVersion tracks what `note.json` looks like; this tracks the table.
#
# 3: two tables (`notes`, `candidates`), because the unit of judgement was a
#    question a person had asked.
# 4: back to one. The tool no longer judges anything — choosing keywords,
#    deciding which note is worth keeping, and saying when to stop are the
#    caller's, so `candidates` (and the whole question ledger) is gone. `notes`
#    loses `collected_at`, because "downloaded" is a fact about the files and a
#    column that mirrors them can only ever disagree with them.
SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    note_id      TEXT PRIMARY KEY,   -- 24 hex chars, parsed out of the href
    url          TEXT,               -- the detail link, replayed to reopen it
    title        TEXT,               -- the note's own title, as note.json has it
    note_dir     TEXT,               -- where its files are on disk
    updated_at   TEXT,               -- the site's own edit time (lastUpdateTime)
    content_hash TEXT                -- sha256 of title+body+tags
);
"""


def now() -> str:
    """A timestamp for every `*_at` column: UTC, ISO-8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and create) the index. Does not create tables — see `open_db`.

    WAL because a run writes while a shell query reads, and because a crash
    mid-run must not leave a lock behind. Returned connection is the caller's to
    close; note that `with conn:` is a *transaction* context in sqlite3 and will
    not close it, which is why `open_db` exists.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the table if absent; refuse a database from another build.

    Refusing rather than migrating is deliberate: every row here is rebuildable
    by re-reading the notes, so the recovery is always "delete it and run
    download again", while guessing at the shape of a half-known table would
    silently mis-report what is already on disk — the one error that costs a
    whole re-download.
    """
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {version} 与当前 {SCHEMA_VERSION} 不符："
            f"{conn.execute('PRAGMA database_list').fetchone()[2]}；"
            "这是索引而非正文，删掉它再跑一次 download 即可由盘上的 note.json 重建"
        )
    conn.commit()


@contextmanager
def open_db(path: str | Path) -> Iterator[sqlite3.Connection]:
    """The way to use this module: connect, ensure schema, commit, close."""
    conn = connect(path)
    try:
        ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def as_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# --- content identity ------------------------------------------------------


def content_hash(note: dict) -> str:
    """A fingerprint of the text a reader would notice change.

    Title, body and tags: what a re-edit usually touches.

    Interaction counts are excluded because they drift on every note constantly,
    so folding them in would make every note look edited on every run.

    Pictures are excluded too, and that one is less obvious. The obvious design
    folds the image URLs in, but it cannot be computed consistently: `note.json`
    forbids extra properties, so the note dict never carries an image list (the
    pictures live in `downloads.json`), and a body-only re-read therefore has no
    URLs to hash — its fingerprint would differ from the one a full read stored
    for the same untouched note, reporting a phantom edit every time. Swapped
    pictures are caught by comparing the name sets in `downloads.json`
    directly, which knows about adds and removals both.
    """
    tags = note.get("tags") or []
    material = json.dumps(
        {
            "title": note.get("title") or "",
            "content": note.get("content") or "",
            "tags": sorted(str(tag) for tag in tags),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def content_changed(stored: dict, fresh: dict) -> bool:
    """Whether the note's own text changed since it was stored.

    `updatedAt` is **positive evidence only**. It is the site's own edit time
    (`lastUpdateTime`, second precision) and beats any fingerprint, but when
    either side lacks it — an old row, a note whose page had no state — it
    proves nothing in either direction, so the comparison falls through to the
    fingerprint instead of concluding "unchanged" from one blank field.

    The comparison is lexicographic on ISO-8601 strings, which is chronological
    because every value written here comes from `note.from_epoch_ms` and carries
    the same `+08:00` offset.
    """
    fresh_updated = fresh.get("updatedAt")
    stored_updated = stored.get("updated_at")
    if fresh_updated and stored_updated and fresh_updated > stored_updated:
        return True
    stored_hash = stored.get("content_hash")
    return bool(stored_hash) and content_hash(fresh) != stored_hash


# --- notes -----------------------------------------------------------------


def note_row(conn: sqlite3.Connection, note_id: str) -> dict | None:
    return as_dict(conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone())


def known_notes(conn: sqlite3.Connection, note_ids: Iterable[str]) -> dict[str, dict]:
    """Look up several cards at once, keyed by id.

    This is the question "have I opened this one before", asked for every card on
    a result page before deciding whether any page needs opening at all. One
    query rather than one per candidate, chunked to stay under sqlite's variable
    limit on a long result page.
    """
    ids = [note_id for note_id in note_ids if note_id]
    if not ids:
        return {}
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT * FROM notes WHERE note_id IN ({placeholders})", chunk
        ).fetchall()
        found.update({row["note_id"]: dict(row) for row in rows})
    return found


def store_note(
    conn: sqlite3.Connection,
    note: dict,
    *,
    note_dir: str | Path | None = None,
) -> None:
    """Insert or refresh a note's row.

    A field the fresh read could not supply (`None`) does not erase what is
    already stored — that is how a read that only downloaded pictures avoids
    blanking the title or the edit time it never looked at. `COALESCE` per column
    rather than a plain `INSERT OR REPLACE`.

    There is no "collected" flag to pass: whether the note's files are complete
    is a fact about the files, and `download.py` reads it from them.
    """
    note_id = note.get("noteId")
    if not note_id:
        raise ValueError("note 缺少 noteId，无法入库")
    # A `content` of None means this read did not look at the body. Hashing
    # nothing would produce a fingerprint that differs from the stored one, so
    # the column stays NULL and the COALESCE below leaves the real value alone.
    # An empty string is a body that was read and is empty — not the same thing.
    has_body = note.get("content") is not None
    conn.execute(
        """
        INSERT INTO notes (note_id, url, title, note_dir, updated_at, content_hash)
        VALUES (:note_id, :url, :title, :note_dir, :updated_at, :content_hash)
        ON CONFLICT(note_id) DO UPDATE SET
            url          = COALESCE(excluded.url, notes.url),
            title        = COALESCE(excluded.title, notes.title),
            note_dir     = COALESCE(excluded.note_dir, notes.note_dir),
            updated_at   = COALESCE(excluded.updated_at, notes.updated_at),
            content_hash = COALESCE(excluded.content_hash, notes.content_hash)
        """,
        {
            "note_id": note_id,
            "url": note.get("url"),
            "title": note.get("title"),
            "note_dir": str(note_dir) if note_dir else None,
            "updated_at": note.get("updatedAt"),
            "content_hash": content_hash(note) if has_body else None,
        },
    )


def set_note_dir(conn: sqlite3.Connection, note_id: str, note_dir: str | Path) -> None:
    """Point a note at its directory, creating no row for a note never read.

    Used when a note is first downloaded (the directory is named after the
    title, so it is only known once the page has been read).
    """
    conn.execute(
        "UPDATE notes SET note_dir = ? WHERE note_id = ?", (str(note_dir), note_id)
    )
