"""Tests for the index — one table, and nothing that is already in a file.

The index exists to answer three questions without opening a page: have we seen
this note, where do its files live, and which version did we last read. What is
tested hardest is the third: a wrong answer there either re-downloads a whole
note for nothing or leaves a stale copy on disk believing it is current.

Nothing here tests whether a note is *downloaded* — that is a fact about the
files, and `download.downloaded_parts` reads it from them.
"""

from __future__ import annotations

import sqlite3

import db
import pytest

NOTE_ID = "6ac48ca100000000140034ed"
URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=token"


@pytest.fixture
def conn(tmp_path):
    with db.open_db(tmp_path / "xhs.db") as connection:
        yield connection


def a_note(**overrides) -> dict:
    """A note as `note.py` produces it, with individual fields override-able."""
    note = {
        "schemaVersion": 4,
        "noteId": NOTE_ID,
        "url": URL,
        "title": "川西赏秋时间表",
        "author": "冷三岁",
        "authorId": "5faf",
        "type": "image",
        "publishedAt": "2025-09-27T12:00:00+08:00",
        "updatedAt": "2025-09-27T12:00:00+08:00",
        "capturedFrom": "state",
        "stats": {"likes": 91, "collects": 32, "comments": 18, "shares": 4},
        "tags": ["川西", "秋色"],
        "content": "10月中下旬开始，川西进入最佳观赏期。",
    }
    note.update(overrides)
    return note


# --- schema ----------------------------------------------------------------


def test_the_index_is_one_table_and_no_mirrors(tmp_path) -> None:
    """`media` and `comments_meta` mirrored two JSON files field for field, and
    the `candidates` ledger existed to hold a question's verdicts. Nothing here
    judges anything any more, and two copies of one fact drift apart."""
    path = tmp_path / "xhs.db"
    with db.open_db(path) as first:
        names = {
            row[0]
            for row in first.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "notes" in names
    assert not names & {"candidates", "media", "comments_meta", "runs", "run_items"}

    with db.open_db(path) as second:  # must not raise
        assert second.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_a_database_from_another_build_is_refused_not_migrated(tmp_path) -> None:
    """Every row is rebuildable from `note.json`, so the recovery is "delete it";
    a guess at a half-known table is not."""
    path = tmp_path / "xhs.db"
    with db.open_db(path):
        pass
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="数据库版本"):
        with db.open_db(path):
            pass


def test_open_db_actually_closes(tmp_path) -> None:
    """`with conn:` is a transaction in sqlite3, so this must not be one."""
    path = tmp_path / "xhs.db"
    with db.open_db(path) as conn:
        pass

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


# --- storing a note --------------------------------------------------------


def test_a_note_lands_with_the_fields_a_page_open_needs(conn) -> None:
    """Five columns, not twenty: the link to replay, the edit time to compare,
    the fingerprint to fall back on, where its files are, and its title."""
    db.store_note(conn, a_note(), note_dir="/tmp/notes/x")

    row = db.note_row(conn, NOTE_ID)
    assert row["url"] == URL
    assert row["title"] == "川西赏秋时间表"
    assert row["updated_at"] == "2025-09-27T12:00:00+08:00"
    assert row["content_hash"] == db.content_hash(a_note())
    assert row["note_dir"] == "/tmp/notes/x"


def test_there_is_no_downloaded_flag_to_disagree_with_the_disk(conn) -> None:
    """A column mirroring what the files say could only ever disagree with
    them — and "downloaded" is read off the files (`download.downloaded_parts`)."""
    db.store_note(conn, a_note())

    columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)")}
    assert columns == {"note_id", "url", "title", "note_dir", "updated_at", "content_hash"}


def test_a_second_read_keeps_what_it_did_not_look_at(conn) -> None:
    """`COALESCE` per column, not `INSERT OR REPLACE`: a run that only downloaded
    pictures has no title to offer and must not blank the one already stored."""
    db.store_note(conn, a_note(), note_dir="/tmp/notes/x")
    db.store_note(conn, {"noteId": NOTE_ID, "content": "换了一版"})

    row = db.note_row(conn, NOTE_ID)
    assert row["url"] == URL
    assert row["title"] == "川西赏秋时间表"
    assert row["updated_at"] == "2025-09-27T12:00:00+08:00"
    assert row["note_dir"] == "/tmp/notes/x"


def test_set_note_dir_points_at_a_note_that_has_no_row_yet(conn) -> None:
    """The directory is named after the title, so it is only known after a read;
    the update must not invent a row for a note that was never stored."""
    db.set_note_dir(conn, "0" * 24, "/tmp/notes/never")
    assert db.note_row(conn, "0" * 24) is None

    db.store_note(conn, a_note())
    db.set_note_dir(conn, NOTE_ID, "/tmp/notes/y")
    assert db.note_row(conn, NOTE_ID)["note_dir"] == "/tmp/notes/y"


def test_a_bodyless_read_leaves_the_fingerprint_alone(conn) -> None:
    """Otherwise a media-only run would report every stored note as edited: it
    hashes "no body" against the real one."""
    db.store_note(conn, a_note())
    before = db.note_row(conn, NOTE_ID)

    db.store_note(conn, {"noteId": NOTE_ID, "content": None})

    assert db.note_row(conn, NOTE_ID)["content_hash"] == before["content_hash"]


def test_an_empty_body_is_hashed_and_is_not_the_same_as_no_body(conn) -> None:
    """`""` is a body that was read and says nothing; `None` is a read that never
    looked. Only the first one is a fact about the note."""
    db.store_note(conn, a_note())
    stored = db.note_row(conn, NOTE_ID)["content_hash"]

    db.store_note(conn, a_note(content=""))

    assert db.note_row(conn, NOTE_ID)["content_hash"] not in (None, stored)


def test_storing_without_a_note_id_is_refused(conn) -> None:
    with pytest.raises(ValueError, match="noteId"):
        db.store_note(conn, {"title": "没有 id"})


def test_known_notes_answers_for_several_ids_at_once(conn) -> None:
    db.store_note(conn, a_note())
    found = db.known_notes(conn, [NOTE_ID, "0" * 24, ""])

    assert set(found) == {NOTE_ID}
    assert db.known_notes(conn, []) == {}


def test_known_notes_chunks_a_long_result_page(conn) -> None:
    """sqlite has a variable limit, and a result page is only 20 cards — but the
    caller may hand in every id it has ever seen."""
    ids = [f"{index:024x}" for index in range(1200)]
    for note_id in ids:
        db.store_note(conn, {"noteId": note_id, "content": "正文"})

    assert set(db.known_notes(conn, ids)) == set(ids)


# --- did the note change? ---------------------------------------------------


def test_a_later_edit_time_is_a_change_even_with_an_identical_body(conn) -> None:
    """The site's own edit time outranks the fingerprint."""
    db.store_note(conn, a_note())
    row = db.note_row(conn, NOTE_ID)

    assert db.content_changed(row, a_note(updatedAt="2026-08-01T21:18:17+08:00"))


def test_an_earlier_or_absent_edit_time_proves_nothing(conn) -> None:
    """`updatedAt` is positive evidence only — a None must not read as 'same',
    it must fall through to the fingerprint."""
    db.store_note(conn, a_note())
    row = db.note_row(conn, NOTE_ID)

    # Same body, no edit time on the fresh side: the fingerprint carries it.
    assert not db.content_changed(row, a_note(updatedAt=None))
    # And a body that really did change is caught by the fingerprint alone.
    assert db.content_changed(row, a_note(updatedAt=None, content="换了一版"))
    # An edit time older than the stored one is not a change either.
    assert not db.content_changed(row, a_note(updatedAt="2020-01-01T00:00:00+08:00"))


def test_interaction_counts_are_not_part_of_the_fingerprint(conn) -> None:
    """They move on every note constantly; folding them in would make every note
    look edited on every run."""
    base = a_note()
    assert db.content_hash(base) == db.content_hash(
        a_note(stats={"likes": 999, "collects": 999, "comments": 999, "shares": 999})
    )


def test_the_fingerprint_covers_title_body_and_tags(conn) -> None:
    base = a_note()
    assert db.content_hash(base) != db.content_hash(a_note(title="另一版"))
    assert db.content_hash(base) != db.content_hash(a_note(content="另一版正文"))
    assert db.content_hash(base) != db.content_hash(a_note(tags=["川西"]))
    # Tag order is not content.
    assert db.content_hash(base) == db.content_hash(a_note(tags=["秋色", "川西"]))


def test_pictures_are_not_in_the_fingerprint(conn) -> None:
    """They cannot be: `note.json` carries no image list, so a body-only re-read
    could not compute the same hash as a full one. Swapped pictures are caught by
    comparing the name sets in `downloads.json` instead."""
    base = a_note()
    assert db.content_hash(base) == db.content_hash({**base, "images": ["a", "b"]})


def test_a_row_without_a_fingerprint_does_not_read_as_unchanged(conn) -> None:
    """An old row's `content_hash` can be NULL, and a blank must not answer the
    question the fingerprint was there to ask."""
    db.store_note(conn, {"noteId": NOTE_ID, "content": None})
    row = db.note_row(conn, NOTE_ID)

    assert row["content_hash"] is None
    assert db.content_changed({"updated_at": None, "content_hash": None}, a_note()) is False
