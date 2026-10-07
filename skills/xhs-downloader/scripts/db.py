#!/usr/bin/env python3
"""The index and dedup ledger, over sqlite.

**This is not where notes live.** The note itself is `note.json`, the thread is
`comments.json`, what was downloaded is `downloads.json` — all on disk, readable
without this module. The database holds only what a *decision* needs: which note
ids have been seen, what was decided about them, what each one said at the time
in one line, and which files are already on disk. Delete it and nothing is lost
except the record of what was already judged; every note can be re-read.

Two questions it exists to answer, both asked on every run:

1. *Have I already looked at this note?* — asked for up to twenty candidates per
   search, before any page is opened. Getting this wrong costs twenty page loads.
2. *Has this note changed since I stored it?* — asked per note, after the page
   is open and the server-rendered state is readable. `lastUpdateTime` answers it
   at second precision; a content fingerprint answers it when that is absent.

Answers to (2) come out of `compare()` as one of six actions, which is the whole
point of the table: `batch.py` turns the action into work rather than re-deriving
it, and `--dedup` only has to override the result.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from runtime import media_name, one_line

# The database's own shape, independent of the note files'. `note-output`'s
# schemaVersion tracks what `note.json` looks like; this tracks the tables. A
# fresh database is created at this version, and the mismatch case — an existing
# file from an older build — is refused rather than migrated, because the ledger
# can always be rebuilt by re-running phase one while a half-migrated one cannot
# be trusted to say what was already judged.
#
# 2: `notes.excerpt_kind` (what the excerpt is: full body, a cut one, a tag pile,
#    or nothing). Databases at version 1 are refused, not migrated — the notes
#    on disk are unaffected and the ledger is rebuilt by re-running phase one.
SCHEMA_VERSION = 2

# Note lifecycle in `notes.status`: the latest thing known about a note, across
# all runs. `run_items.decision` is the per-run record; this is the summary that
# makes "已筛过" answerable in one lookup.
STATUS_SEEN = "seen"            # discovered on a search page, page not opened
STATUS_SCREENED = "screened"    # opened, body and cover read
STATUS_APPROVED = "approved"    # judged useful
STATUS_REJECTED = "rejected"    # judged useless
STATUS_COLLECTED = "collected"  # phase two finished
STATUS_FAILED = "failed"

DECISION_PENDING = "pending"
DECISION_KEEP = "keep"
DECISION_DROP = "drop"

STATUSES = (
    STATUS_SEEN,
    STATUS_SCREENED,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_COLLECTED,
    STATUS_FAILED,
)
DECISIONS = (DECISION_PENDING, DECISION_KEEP, DECISION_DROP)

# What `compare()` returns. Each names a piece of work; the first two differ in
# whether media is re-fetched, and the last three in whether anything is read at
# all beyond the state already in hand.
NEW = "new"
RECOLLECT = "recollect"
REDOWNLOAD_MEDIA = "redownload_media"
REFRESH_COMMENTS = "refresh_comments"
REFRESH_STATS = "refresh_stats"
SKIP_DUP = "skip_dup"

ACTIONS = (NEW, RECOLLECT, REDOWNLOAD_MEDIA, REFRESH_COMMENTS, REFRESH_STATS, SKIP_DUP)

MEDIA_COMPLETE = "complete"
MEDIA_DISCOVERED = "discovered"

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    note_id           TEXT PRIMARY KEY,
    url               TEXT,
    title             TEXT,
    author            TEXT,
    author_id         TEXT,
    type              TEXT,
    published_at      TEXT,
    updated_at        TEXT,
    updated_at_source TEXT,   -- 'state' when it came from lastUpdateTime, else NULL
    tags_json         TEXT,
    likes             INTEGER,
    collects          INTEGER,
    comment_count     INTEGER,
    shares            INTEGER,
    content_hash      TEXT,
    content_len       INTEGER,
    media_count       INTEGER,
    status            TEXT NOT NULL,
    schema_version    INTEGER,
    note_dir          TEXT,
    excerpt           TEXT,
    excerpt_kind      TEXT,   -- full | truncated | tags-only | empty
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    last_screened_at  TEXT,
    last_collected_at TEXT,
    last_media_at     TEXT,
    last_comments_at  TEXT
);

CREATE TABLE IF NOT EXISTS media (
    note_id       TEXT NOT NULL,
    url           TEXT NOT NULL,
    kind          TEXT,       -- image | cover | video | audio
    filename      TEXT,       -- where it landed, as written by media.py
    state         TEXT,       -- complete | discovered | interrupted | failed
    discovered_at TEXT,
    downloaded_at TEXT,
    PRIMARY KEY (note_id, url)
);

CREATE TABLE IF NOT EXISTS comments_meta (
    note_id            TEXT PRIMARY KEY,
    declared_total     INTEGER,
    collected          INTEGER,
    replies_collected  INTEGER,
    thread_ended       INTEGER,
    possibly_incomplete INTEGER,
    captured_at        TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    data_root   TEXT,
    keywords    TEXT,
    filters_json TEXT,
    args_json   TEXT,
    totals_json TEXT
);

CREATE TABLE IF NOT EXISTS run_items (
    run_id     INTEGER NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    note_id    TEXT NOT NULL,
    rank       INTEGER,      -- position in the search result page, 1-based
    href       TEXT,         -- the URL with its xsec_token, replayed by phase two
    keyword    TEXT,
    decision   TEXT NOT NULL,
    reason     TEXT,
    decided_at TEXT,
    PRIMARY KEY (run_id, note_id)
);

CREATE INDEX IF NOT EXISTS run_items_decision ON run_items(run_id, decision);
CREATE INDEX IF NOT EXISTS notes_status ON notes(status);
"""


def now() -> str:
    """A timestamp for every `*_at` column: UTC, ISO-8601, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open (and create) the index. Does not create tables — see `open_db`.

    WAL because a phase-one run writes while a shell query reads, and because a
    crash mid-run must not leave a lock behind. Returned connection is the
    caller's to close; note that `with conn:` is a *transaction* context in
    sqlite3 and will not close it, which is why `open_db` exists.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the tables if absent; refuse a database from another build.

    Refusing rather than migrating is deliberate: this file is a cache of
    judgements, so the recovery is always "re-run phase one", and guessing at
    the shape of a half-known table would silently mis-report what was already
    screened — the one error that costs a full re-read.
    """
    conn.executescript(SCHEMA)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    elif version != SCHEMA_VERSION:
        raise RuntimeError(
            f"数据库版本 {version} 与当前 {SCHEMA_VERSION} 不符：{conn.execute('PRAGMA database_list').fetchone()[2]}；"
            "这是判断台账而非正文，删除后重跑阶段一即可重建"
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


# --- the screening excerpt -------------------------------------------------

EXCERPT_LIMIT = 600
EXCERPT_FULL = "full"            # the whole body fits
EXCERPT_TRUNCATED = "truncated"  # cut at the limit
EXCERPT_TAGS_ONLY = "tags-only"  # nothing but hashtags — the note is its pictures
EXCERPT_EMPTY = "empty"          # a body that was read and is empty

TRAILING_TAGS_RE = re.compile(r"(?:\s*#[^\s#]+)+\s*$")


def strip_trailing_tags(text: str) -> str:
    """Drop the hashtag block a body ends with.

    Measured 2026-10-07 on the 13 notes then in the index: 4 had bodies under 100
    characters and every one of them was mostly tags — one 19-character body was
    exactly `#甘南旅游 #自驾游旅游 #自由行旅游`, 88 characters were 12 tags plus the
    words `自驾攻略 路况轿车`. A screening excerpt full of tags reads as if the
    note said something about 甘南旅游 when it said nothing at all.

    Only the TRAILING run goes. A `#` inside a sentence is prose ("第3天#桑科草原
    扎营"), and cutting those would lose text a reader wants.
    """
    return TRAILING_TAGS_RE.sub("", text)


def excerpt_of(content: str | None, limit: int = EXCERPT_LIMIT) -> tuple[str | None, str | None]:
    """The screening excerpt for a body, and what kind of excerpt it is.

    Returns `(text, kind)`. `content` of `None` means this read did not look at
    the body — a media- or comment-only run — and returns `(None, None)` so both
    columns keep what they had rather than recording "no text" as a fact.

    The kind is the part a later phase reads instead of opening `note.json`:
    `tags-only` and `empty` say the note carries no prose at all, which is why its
    pictures matter; `full` says the excerpt is the whole body; `truncated` warns
    that something was cut.
    """
    if content is None:
        return None, None
    text = one_line(strip_trailing_tags(content))
    if not text:
        return None, EXCERPT_TAGS_ONLY if content.strip() else EXCERPT_EMPTY
    kind = EXCERPT_FULL if len(text) <= limit else EXCERPT_TRUNCATED
    return one_line(text, limit), kind


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
    URLs to hash — its fingerprint would differ from the one a screening run
    stored for the same untouched note, reporting a phantom edit every time.
    Swapped pictures are detected properly by `media_urls_changed`, which
    compares URL sets directly and knows about adds and removals both.
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


def _stat(note: dict, key: str) -> int | None:
    stats = note.get("stats") or {}
    value = stats.get(key)
    return None if value is None else int(value)


def _stored_stat(stored: dict, key: str) -> int | None:
    value = stored.get({"comments": "comment_count"}.get(key, key))
    return None if value is None else int(value)


def content_changed(stored: dict, fresh: dict) -> bool:
    """Whether the note's own text or pictures changed since it was stored.

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


def comment_count_grew(stored: dict, fresh: dict) -> bool:
    """Whether the note has more comments than when it was stored.

    A comment count only ever rises, so a difference means new replies are worth
    re-reading; a *drop* means the site recounted, which is not new information.
    """
    fresh_count = _stat(fresh, "comments")
    stored_count = _stored_stat(stored, "comments")
    if fresh_count is None or stored_count is None:
        return False
    return fresh_count > stored_count


def stats_changed(stored: dict, fresh: dict) -> bool:
    """Whether any interaction count differs. Drift alone is not an edit."""
    for key in ("likes", "collects", "comments", "shares"):
        fresh_value, stored_value = _stat(fresh, key), _stored_stat(stored, key)
        if fresh_value is None or stored_value is None:
            continue
        if fresh_value != stored_value:
            return True
    return False


def compare(
    stored: dict | None,
    fresh: dict,
    *,
    media_changed: bool = False,
    want_comments: bool = True,
) -> str:
    """Decide what a note already in the index needs, from its fresh state.

    The six answers, in the order they are tested — the order is the design, not
    an accident: a changed note outranks a growing thread, because re-reading the
    body already refreshes the counts that came with it.

    `media_changed` and `want_comments` are arguments rather than lookups so this
    stays a pure function of what is known; `media_urls_changed` does the query.
    `want_comments=False` demotes a growing thread to `refresh_stats`, which is
    truthful: the counts do move, and this run was not going to read the thread
    anyway.
    """
    if stored is None:
        return NEW
    if content_changed(stored, fresh):
        # The body is being rewritten either way, so new pictures ride along.
        return REDOWNLOAD_MEDIA if media_changed else RECOLLECT
    if media_changed:
        return REDOWNLOAD_MEDIA
    if want_comments and comment_count_grew(stored, fresh):
        return REFRESH_COMMENTS
    if stats_changed(stored, fresh):
        return REFRESH_STATS
    return SKIP_DUP


# --- notes -----------------------------------------------------------------


def note_row(conn: sqlite3.Connection, note_id: str) -> dict | None:
    return as_dict(conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone())


def known_notes(conn: sqlite3.Connection, note_ids: Iterable[str]) -> dict[str, dict]:
    """Look up several candidates at once, keyed by id.

    One query rather than one per candidate: this is asked for every card on the
    page, before deciding whether any page needs opening at all.
    """
    ids = [note_id for note_id in note_ids if note_id]
    if not ids:
        return {}
    found: dict[str, dict] = {}
    # Chunked to stay under sqlite's variable limit on a long result page.
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
    excerpt: str | None = None,
    excerpt_kind: str | None = None,
    status: str | None = None,
    media: Iterable[dict] | None = None,
    stamp: str | None = None,
) -> None:
    """Insert or refresh a note's row, keeping the first-seen time.

    A field the fresh read could not supply (`None`) does not erase what is
    already stored — that is how a body-only re-read avoids blanking the author
    or the counts it never looked at. `status` defaults to `seen`, and an
    explicit one is what phase two passes to record `collected`.

    `COALESCE` per column rather than a plain `INSERT OR REPLACE`, which would
    drop `first_seen_at` and every `last_*` stamp on every touch.
    """
    note_id = note.get("noteId")
    if not note_id:
        raise ValueError("note 缺少 noteId，无法入库")
    stamp = stamp or now()
    stats = note.get("stats") or {}
    tags = note.get("tags")
    media = list(media or [])
    # How many pictures the note claims: from the note dict when it carries them,
    # otherwise from the media the caller discovered separately. Neither means
    # this read found out, which is not the same as "no pictures".
    images = note.get("images")
    if images is None and media:
        images = media
    # A `content` of None means this read did not look at the body — a media- or
    # comment-only run. Hashing nothing would produce a fingerprint that differs
    # from the stored one and record a length of zero, so the content columns
    # stay NULL and the COALESCE below leaves the real values alone. An empty
    # string is a body that was read and is empty, which is not the same thing.
    has_body = note.get("content") is not None
    row = {
        "note_id": note_id,
        "url": note.get("url"),
        "title": note.get("title"),
        "author": note.get("author"),
        "author_id": note.get("authorId"),
        "type": note.get("type"),
        "published_at": note.get("publishedAt"),
        "updated_at": note.get("updatedAt"),
        "updated_at_source": note.get("capturedFrom") if note.get("updatedAt") else None,
        "tags_json": json.dumps(tags, ensure_ascii=False) if tags is not None else None,
        "likes": stats.get("likes"),
        "collects": stats.get("collects"),
        "comment_count": stats.get("comments"),
        "shares": stats.get("shares"),
        "content_hash": content_hash(note) if has_body else None,
        "content_len": len(note.get("content")) if has_body else None,
        "media_count": len(images) if images is not None else None,
        "status": status,
        "schema_version": note.get("schemaVersion"),
        "note_dir": str(note_dir) if note_dir else None,
        "excerpt": excerpt,
        "excerpt_kind": excerpt_kind,
        "seen": stamp,
    }
    conn.execute(
        """
        INSERT INTO notes (note_id, url, title, author, author_id, type,
            published_at, updated_at, updated_at_source, tags_json,
            likes, collects, comment_count, shares, content_hash, content_len,
            media_count, status, schema_version, note_dir, excerpt, excerpt_kind,
            first_seen_at, last_seen_at, last_screened_at)
        VALUES (:note_id, :url, :title, :author, :author_id, :type,
            :published_at, :updated_at, :updated_at_source, :tags_json,
            :likes, :collects, :comment_count, :shares, :content_hash, :content_len,
            :media_count, COALESCE(:status, 'seen'), :schema_version, :note_dir,
            :excerpt, :excerpt_kind, :seen, :seen,
            CASE WHEN :status IS NOT NULL AND :status != 'seen' THEN :seen END)
        ON CONFLICT(note_id) DO UPDATE SET
            url            = COALESCE(excluded.url, notes.url),
            title          = COALESCE(excluded.title, notes.title),
            author         = COALESCE(excluded.author, notes.author),
            author_id      = COALESCE(excluded.author_id, notes.author_id),
            type           = COALESCE(excluded.type, notes.type),
            published_at   = COALESCE(excluded.published_at, notes.published_at),
            updated_at     = COALESCE(excluded.updated_at, notes.updated_at),
            updated_at_source = COALESCE(excluded.updated_at_source, notes.updated_at_source),
            tags_json      = COALESCE(excluded.tags_json, notes.tags_json),
            likes          = COALESCE(excluded.likes, notes.likes),
            collects       = COALESCE(excluded.collects, notes.collects),
            comment_count  = COALESCE(excluded.comment_count, notes.comment_count),
            shares         = COALESCE(excluded.shares, notes.shares),
            content_hash   = COALESCE(excluded.content_hash, notes.content_hash),
            content_len    = COALESCE(excluded.content_len, notes.content_len),
            media_count    = COALESCE(excluded.media_count, notes.media_count),
            status         = COALESCE(:status, notes.status),
            schema_version = COALESCE(excluded.schema_version, notes.schema_version),
            note_dir       = COALESCE(excluded.note_dir, notes.note_dir),
            excerpt        = COALESCE(excluded.excerpt, notes.excerpt),
            excerpt_kind   = COALESCE(excluded.excerpt_kind, notes.excerpt_kind),
            last_seen_at   = excluded.last_seen_at,
            last_screened_at = CASE
                WHEN :status IS NOT NULL AND :status != 'seen'
                THEN excluded.last_screened_at ELSE notes.last_screened_at END
        """,
        row,
    )
    if media:
        store_media(conn, note_id, media, stamp=stamp)


def set_status(conn: sqlite3.Connection, note_id: str, status: str, *, stamp: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"未知状态：{status}；可用：{', '.join(STATUSES)}")
    conn.execute(
        "UPDATE notes SET status = ?, last_seen_at = ? WHERE note_id = ?",
        (status, stamp or now(), note_id),
    )


def mark_seen(conn: sqlite3.Connection, note_ids: Iterable[str], *, stamp: str | None = None) -> None:
    """Note that these ids came up in a search, without opening any of them.

    Separate from `store_note` because it is the one thing a run learns about a
    candidate it deliberately did NOT open, and separate from `set_status`
    because a sighting must never move a judgement: a note dropped last month
    that appears in today's results is still dropped until someone says
    otherwise.
    """
    ids = [note_id for note_id in note_ids if note_id]
    if not ids:
        return
    stamp = stamp or now()
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        placeholders = ",".join("?" * len(chunk))
        conn.execute(
            f"UPDATE notes SET last_seen_at = ? WHERE note_id IN ({placeholders})",
            [stamp, *chunk],
        )


def touch(conn: sqlite3.Connection, note_id: str, column: str, *, stamp: str | None = None) -> None:
    """Stamp one of the `last_*` columns — what a part just did to this note."""
    allowed = {
        "collected": "last_collected_at",
        "media": "last_media_at",
        "comments": "last_comments_at",
        "screened": "last_screened_at",
    }
    if column not in allowed:
        raise ValueError(f"未知时间戳：{column}；可用：{', '.join(allowed)}")
    conn.execute(
        f"UPDATE notes SET {allowed[column]} = ? WHERE note_id = ?", (stamp or now(), note_id)
    )


# --- media -----------------------------------------------------------------


def store_media(
    conn: sqlite3.Connection,
    note_id: str,
    media: Iterable[dict],
    *,
    stamp: str | None = None,
) -> None:
    """Record discovered media, and that downloaded media is on disk.

    The `url` column holds the picture's *name*, not the URL it was fetched from
    (`runtime.media_name`): URLs are re-signed on every read, names are not, and
    the name is what the two dedup questions are actually about. Rows written
    before this rule hold whole URLs and still answer correctly, because every
    read path normalises them back to the name before comparing.

    `filename` only counts as a download when the browser said `complete` *and*
    named a file — the same rule `downloads.json` is read by, applied here so the
    index never claims a file the disk does not have.
    """
    stamp = stamp or now()
    for item in media:
        url = media_name(item.get("url") or item.get("src"))
        if not url:
            continue
        state = item.get("state")
        complete = state == MEDIA_COMPLETE and bool(item.get("filename"))
        conn.execute(
            """
            INSERT INTO media (note_id, url, kind, filename, state, discovered_at, downloaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id, url) DO UPDATE SET
                kind       = COALESCE(excluded.kind, media.kind),
                filename   = COALESCE(excluded.filename, media.filename),
                state      = COALESCE(excluded.state, media.state),
                downloaded_at = COALESCE(excluded.downloaded_at, media.downloaded_at)
            """,
            (
                note_id,
                url,
                item.get("kind") or item.get("type"),
                item.get("filename"),
                state or MEDIA_DISCOVERED,
                stamp,
                stamp if complete else None,
            ),
        )


def stored_media_urls(conn: sqlite3.Connection, note_id: str) -> set[str]:
    """Every picture already known for this note, whatever became of it.

    Includes ones that failed or were never requested: a picture that failed last
    time is still a picture this note has, and treating its absence as "new
    media" would make a retry look like an edit.

    Names, not URLs — see `store_media`. Normalising here is what lets rows from
    before the rule change compare equal to today's page.
    """
    rows = conn.execute("SELECT url FROM media WHERE note_id = ?", (note_id,)).fetchall()
    return {media_name(row["url"]) for row in rows}


def downloaded_files(conn: sqlite3.Connection, note_id: str) -> dict[str, str]:
    """`{name: filename}` for media this note has on disk, per index and filesystem.

    Keyed by picture NAME (`runtime.media_name`), so a caller holding a freshly
    signed URL looks it up with `media_name(url)` and finds the file the last run
    saved.

    The disk check is the point. A row saying `complete` proves the download
    finished once, not that the file is still there; a deleted or moved
    `images/` would otherwise make every later run skip a picture that is gone.
    A path that no longer resolves therefore counts as not downloaded, which
    costs a re-fetch and never a silently empty archive.

    The filename comes back with the URL because a run that skips a picture
    still has to *record* it as downloaded. Without the path there is nothing to
    write but a bare `discovered`, which forgets the file and makes the next run
    fetch the same picture again — and the one after that, for ever.
    """
    rows = conn.execute(
        "SELECT url, filename FROM media WHERE note_id = ? AND state = ?",
        (note_id, MEDIA_COMPLETE),
    ).fetchall()
    return {
        media_name(row["url"]): row["filename"]
        for row in rows
        if row["filename"] and Path(row["filename"]).is_file()
    }


def downloaded_urls(conn: sqlite3.Connection, note_id: str) -> set[str]:
    """The names from `downloaded_files`, for callers that only need the set."""
    return set(downloaded_files(conn, note_id))


def media_urls_changed(conn: sqlite3.Connection, note_id: str, fresh_urls: Iterable[str]) -> bool:
    """Whether the note's picture set differs from what was stored.

    Compared by picture name, so a re-signed URL is not a changed picture.

    Both directions count. A new name means added pictures; a stored name that is
    gone means removed ones — and either way the note as archived no longer
    describes the note as it is.
    """
    stored = stored_media_urls(conn, note_id)
    fresh = {media_name(url) for url in fresh_urls if url}
    return stored != fresh


# --- comments --------------------------------------------------------------


def store_comments_meta(conn: sqlite3.Connection, note_id: str, payload: dict) -> None:
    """Record how complete a thread read was, which `comments.json` owns in full.

    Only the diagnostics: `declared_total` against `collected` is what says
    whether a refresh is worth another thirty scroll rounds.
    """
    conn.execute(
        """
        INSERT INTO comments_meta (note_id, declared_total, collected, replies_collected,
            thread_ended, possibly_incomplete, captured_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(note_id) DO UPDATE SET
            declared_total = excluded.declared_total,
            collected = excluded.collected,
            replies_collected = excluded.replies_collected,
            thread_ended = excluded.thread_ended,
            possibly_incomplete = excluded.possibly_incomplete,
            captured_at = excluded.captured_at
        """,
        (
            note_id,
            payload.get("declaredTotal"),
            payload.get("collected"),
            payload.get("repliesCollected"),
            payload.get("threadEnded"),
            payload.get("possiblyIncomplete"),
            payload.get("capturedAt") or now(),
        ),
    )


def comments_row(conn: sqlite3.Connection, note_id: str) -> dict | None:
    return as_dict(
        conn.execute("SELECT * FROM comments_meta WHERE note_id = ?", (note_id,)).fetchone()
    )


# --- runs and the selection ------------------------------------------------


def start_run(
    conn: sqlite3.Connection,
    *,
    kind: str,
    data_root: str | Path | None = None,
    keywords: Iterable[str] | None = None,
    filters: dict | None = None,
    args: dict | None = None,
) -> int:
    """Open a run and return its id. `kind` is `screen` or `collect`."""
    cursor = conn.execute(
        "INSERT INTO runs (kind, started_at, data_root, keywords, filters_json, args_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            kind,
            now(),
            str(data_root) if data_root else None,
            ";".join(keywords) if keywords else None,
            json.dumps(filters, ensure_ascii=False) if filters else None,
            json.dumps(args, ensure_ascii=False) if args else None,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, totals: dict | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, totals_json = ? WHERE run_id = ?",
        (now(), json.dumps(totals, ensure_ascii=False) if totals else None, run_id),
    )
    conn.commit()


def add_run_items(conn: sqlite3.Connection, run_id: int, items: Iterable[dict]) -> None:
    """Record a page's candidates, in the order the page showed them.

    Re-recording an id keeps the judgement already made about it: a run can be
    re-screened, and losing a `keep` because the same note reappeared at a
    different rank would be a silent regression.
    """
    for item in items:
        conn.execute(
            """
            INSERT INTO run_items (run_id, note_id, rank, href, keyword, decision)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, note_id) DO UPDATE SET
                rank    = COALESCE(excluded.rank, run_items.rank),
                href    = COALESCE(excluded.href, run_items.href),
                keyword = COALESCE(excluded.keyword, run_items.keyword)
            """,
            (
                run_id,
                item["noteId"],
                item.get("rank"),
                item.get("href"),
                item.get("keyword"),
                item.get("decision") or DECISION_PENDING,
            ),
        )
    conn.commit()


def decide(
    conn: sqlite3.Connection,
    run_id: int,
    note_ids: Iterable[str],
    decision: str,
    *,
    reason: str | None = None,
) -> int:
    """Record keep/drop for notes in a run, and mirror it onto the note's status.

    Returns how many items were actually in the run. An id that was never a
    candidate is skipped rather than inserted — the ledger records decisions
    about this run's candidates, and inventing a row would make `run_items` stop
    describing what the search returned.
    """
    if decision not in (DECISION_KEEP, DECISION_DROP):
        raise ValueError(f"判断只能是 {DECISION_KEEP} 或 {DECISION_DROP}，收到：{decision}")
    status = STATUS_APPROVED if decision == DECISION_KEEP else STATUS_REJECTED
    stamp = now()
    changed = 0
    for note_id in note_ids:
        cursor = conn.execute(
            "UPDATE run_items SET decision = ?, reason = COALESCE(?, reason), decided_at = ? "
            "WHERE run_id = ? AND note_id = ?",
            (decision, reason, stamp, run_id, note_id),
        )
        if cursor.rowcount:
            changed += 1
            conn.execute(
                "UPDATE notes SET status = ?, last_seen_at = ? WHERE note_id = ?",
                (status, stamp, note_id),
            )
    conn.commit()
    return changed


def run_items(
    conn: sqlite3.Connection, run_id: int, *, decision: str | None = None
) -> list[dict]:
    """A run's candidates, in page order, optionally only the kept ones.

    `href` is carried because it is the only URL that still works: a note URL
    without its `xsec_token` is refused by the site, so phase two replays the
    href phase one recorded rather than rebuilding a URL from the id.
    """
    sql = "SELECT * FROM run_items WHERE run_id = ?"
    params: list[object] = [run_id]
    if decision:
        sql += " AND decision = ?"
        params.append(decision)
    sql += " ORDER BY rank IS NULL, rank, note_id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def run_row(conn: sqlite3.Connection, run_id: int) -> dict | None:
    return as_dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())
