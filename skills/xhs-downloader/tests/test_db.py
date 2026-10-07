"""Tests for the index and dedup ledger.

The six `compare()` answers are the part worth testing hardest: they are what
decides whether a page is opened again, and each one is a claim about a specific
kind of change. They are also pure, so each branch gets a case rather than a
mocked-up database.
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
        "schemaVersion": 3,
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
        "images": [{"src": "https://cdn/a.webp"}, {"src": "https://cdn/b.webp"}],
    }
    note.update(overrides)
    return note


# --- schema ----------------------------------------------------------------


def test_tables_are_created_and_reopening_is_harmless(tmp_path) -> None:
    path = tmp_path / "xhs.db"
    with db.open_db(path) as first:
        names = {
            row[0]
            for row in first.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"notes", "media", "comments_meta", "runs", "run_items"} <= names

    with db.open_db(path) as second:  # must not raise
        assert second.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_a_database_from_another_build_is_refused_not_migrated(tmp_path) -> None:
    """The ledger can be rebuilt by re-running phase one; a guess cannot."""
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


# --- storing ---------------------------------------------------------------


def test_a_new_note_lands_with_its_fields_and_stamps(conn) -> None:
    db.store_note(conn, a_note(), note_dir="/tmp/notes/x", excerpt="摘要", status=db.STATUS_SCREENED)

    row = stored(conn)
    assert row["title"] == "川西赏秋时间表"
    assert row["likes"] == 91 and row["shares"] == 4
    assert row["content_len"] == len("10月中下旬开始，川西进入最佳观赏期。")
    assert row["media_count"] == 2
    assert row["status"] == db.STATUS_SCREENED
    assert row["first_seen_at"] == row["last_screened_at"]


def test_a_second_read_keeps_first_seen_and_does_not_blank_fields(conn) -> None:
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED, stamp="2026-10-01T00:00:00+00:00")
    # A comment-only run: it knows the id and nothing else.
    db.store_note(conn, {"noteId": NOTE_ID}, stamp="2026-10-02T00:00:00+00:00")

    row = db.note_row(conn, NOTE_ID)
    assert row["first_seen_at"] == "2026-10-01T00:00:00+00:00"
    assert row["last_seen_at"] == "2026-10-02T00:00:00+00:00"
    assert row["title"] == "川西赏秋时间表"      # survived
    assert row["content_len"] == len("10月中下旬开始，川西进入最佳观赏期。")
    assert row["status"] == db.STATUS_SCREENED  # a bare touch does not demote it


def test_a_bodyless_read_does_not_zero_the_content_columns(conn) -> None:
    """Otherwise a media-only run reports every stored note as empty."""
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)
    before = db.note_row(conn, NOTE_ID)

    db.store_note(conn, {"noteId": NOTE_ID, "images": [{"src": "https://cdn/a.webp"}]})

    after = db.note_row(conn, NOTE_ID)
    assert after["content_len"] == before["content_len"]
    assert after["content_hash"] == before["content_hash"]


def test_an_empty_body_is_not_the_same_as_no_body(conn) -> None:
    db.store_note(conn, a_note())
    db.store_note(conn, a_note(content=""))

    assert db.note_row(conn, NOTE_ID)["content_len"] == 0


def test_storing_without_a_note_id_is_refused(conn) -> None:
    with pytest.raises(ValueError, match="noteId"):
        db.store_note(conn, {"title": "没有 id"})


def test_known_notes_answers_for_several_ids_at_once(conn) -> None:
    db.store_note(conn, a_note())
    found = db.known_notes(conn, [NOTE_ID, "0" * 24, ""])

    assert set(found) == {NOTE_ID}


# --- the six answers -------------------------------------------------------


def stored(conn) -> dict:
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)
    return db.note_row(conn, NOTE_ID)


def test_not_in_the_index_is_new(conn) -> None:
    assert db.compare(None, a_note()) == db.NEW


def test_an_untouched_note_is_skipped(conn) -> None:
    assert db.compare(stored(conn), a_note()) == db.SKIP_DUP


def test_a_later_edit_time_is_a_change_even_with_an_identical_body(conn) -> None:
    """The site's own edit time outranks the fingerprint."""
    row = stored(conn)
    fresh = a_note(updatedAt="2026-08-01T21:18:17+08:00")

    assert db.content_changed(row, fresh)
    assert db.compare(row, fresh) == db.RECOLLECT


def test_an_earlier_or_absent_edit_time_proves_nothing(conn) -> None:
    """`updatedAt` is positive evidence only — a None must not read as 'same'."""
    row = stored(conn)

    # Same body, no edit time on the fresh side: the fingerprint carries it.
    assert db.compare(row, a_note(updatedAt=None)) == db.SKIP_DUP
    # And a body that really did change is caught by the fingerprint alone.
    assert db.compare(row, a_note(updatedAt=None, content="换了一版")) == db.RECOLLECT
    # An edit time older than the stored one is not a change either.
    assert db.compare(row, a_note(updatedAt="2020-01-01T00:00:00+08:00")) == db.SKIP_DUP


def test_a_changed_note_whose_pictures_also_moved_redownloads_media(conn) -> None:
    fresh = a_note(content="换了一版")

    assert db.compare(stored(conn), fresh, media_changed=True) == db.REDOWNLOAD_MEDIA


def test_an_unchanged_body_with_new_pictures_still_redownloads(conn) -> None:
    assert db.compare(stored(conn), a_note(), media_changed=True) == db.REDOWNLOAD_MEDIA


def test_new_comments_refresh_the_thread_only_if_this_run_reads_it(conn) -> None:
    row = stored(conn)
    fresh = a_note(stats={"likes": 91, "collects": 32, "comments": 25, "shares": 4})

    assert db.compare(row, fresh, want_comments=True) == db.REFRESH_COMMENTS
    # A run that was never going to read the thread reports the drift it will
    # actually write, rather than scheduling work it will not do.
    assert db.compare(row, fresh, want_comments=False) == db.REFRESH_STATS


def test_a_falling_comment_count_is_not_new_comments(conn) -> None:
    row = stored(conn)
    fresh = a_note(stats={"likes": 91, "collects": 32, "comments": 3, "shares": 4})

    assert db.compare(row, fresh) == db.REFRESH_STATS


def test_drifting_counts_alone_are_a_stats_refresh(conn) -> None:
    row = stored(conn)
    fresh = a_note(stats={"likes": 120, "collects": 32, "comments": 18, "shares": 4})

    assert db.compare(row, fresh) == db.REFRESH_STATS


def test_a_field_the_fresh_read_lacks_does_not_count_as_drift(conn) -> None:
    """A note whose page had no state has no counts; that is not a change."""
    row = stored(conn)
    fresh = a_note(stats={})

    assert db.compare(row, fresh) == db.SKIP_DUP


def test_the_fingerprint_covers_title_body_and_tags_but_not_counts(conn) -> None:
    base = a_note()
    assert db.content_hash(base) != db.content_hash(a_note(title="另一版"))
    assert db.content_hash(base) != db.content_hash(a_note(content="另一版正文"))
    assert db.content_hash(base) != db.content_hash(a_note(tags=["川西"]))
    # Interaction counts move on every note constantly; folding them in would
    # make every note look edited on every run.
    assert db.content_hash(base) == db.content_hash(
        a_note(stats={"likes": 999, "collects": 999, "comments": 999, "shares": 999})
    )
    # Tag order is not content.
    assert db.content_hash(base) == db.content_hash(a_note(tags=["秋色", "川西"]))


def test_the_fingerprint_ignores_pictures_so_a_body_only_reread_agrees(conn) -> None:
    """Pictures are `media_urls_changed`'s business, not the fingerprint's.

    `note.json` forbids extra properties, so no note dict carries an image list —
    a body-only re-read has none to hash. Were pictures in the fingerprint, that
    read would disagree with the screening run that stored the same untouched
    note and report a phantom edit on every run.
    """
    assert db.content_hash(a_note(images=[])) == db.content_hash(a_note())
    assert db.content_hash(a_note(images=[{"src": "https://cdn/c.webp"}])) == db.content_hash(
        a_note()
    )


# --- media -----------------------------------------------------------------


def test_downloaded_urls_need_the_file_to_still_be_there(conn, tmp_path) -> None:
    """A row saying complete proves it finished once, not that it is still here."""
    kept = tmp_path / "keep.webp"
    kept.write_bytes(b"x")
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)
    db.store_media(
        conn,
        NOTE_ID,
        [
            {"url": "https://cdn/a.webp", "state": "complete", "filename": str(kept)},
            {"url": "https://cdn/b.webp", "state": "complete", "filename": str(tmp_path / "gone.webp")},
            {"url": "https://cdn/c.webp", "state": "discovered"},
            {"url": "https://cdn/d.webp", "state": "failed"},
        ],
    )

    assert db.downloaded_urls(conn, NOTE_ID) == {"cdn/a.webp"}
    # But all four are known to belong to this note, so none of them reads as
    # newly added media on the next run.
    assert db.stored_media_urls(conn, NOTE_ID) == {
        "cdn/a.webp", "cdn/b.webp", "cdn/c.webp", "cdn/d.webp",
    }


def test_downloaded_files_carry_the_path_a_skip_needs(conn, tmp_path) -> None:
    """The map, not just the set.

    A run that skips a picture because it already has it must still be able to
    write down *where* the file is. With only the URL set, all it can record is
    "discovered" — and then the next run fetches the same picture again.
    """
    kept = tmp_path / "keep.webp"
    kept.write_bytes(b"x")
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)
    db.store_media(
        conn,
        NOTE_ID,
        [
            {"url": "https://cdn/a.webp", "state": "complete", "filename": str(kept)},
            {"url": "https://cdn/b.webp", "state": "complete",
             "filename": str(tmp_path / "gone.webp")},
        ],
    )

    assert db.downloaded_files(conn, NOTE_ID) == {"cdn/a.webp": str(kept)}
    assert db.downloaded_urls(conn, NOTE_ID) == set(db.downloaded_files(conn, NOTE_ID))


def test_one_picture_under_two_signatures_is_still_one_picture(conn, tmp_path) -> None:
    """The CDN re-signs on every read; the ledger must not call that an edit.

    Measured 2026-10-07: one picture came back as `/202610071719/259fbf23…/NAME`
    in one run and `/202610071730/61550091…/NAME` in the next. Storing whole URLs
    made the second look like newly added media, so `--dedup refresh` fetched
    every picture again and landed it beside the last one as `-2`, `-3`, …
    """
    first = "https://sns-webpic-qc.xhscdn.com/202610071719/259fbf23/1040g2sg.jpg"
    second = "https://sns-webpic-qc.xhscdn.com/202610071730/61550091/1040g2sg.jpg"
    landed = tmp_path / "1040g2sg.jpg"
    landed.write_bytes(b"x")
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)

    db.store_media(conn, NOTE_ID, [{"url": first, "state": "complete", "filename": str(landed)}])
    # The same picture, re-signed, is not a change…
    assert not db.media_urls_changed(conn, NOTE_ID, [second])
    # …and a lookup with the fresh URL finds the file the first run saved, so the
    # download step can skip it instead of writing a second copy.
    assert db.downloaded_files(conn, NOTE_ID) == {
        "sns-webpic-qc.xhscdn.com/1040g2sg.jpg": str(landed),
    }
    # A row written for the re-signed URL does not add a second picture.
    db.store_media(conn, NOTE_ID, [{"url": second, "state": "complete", "filename": str(landed)}])
    assert db.stored_media_urls(conn, NOTE_ID) == {"sns-webpic-qc.xhscdn.com/1040g2sg.jpg"}


def test_a_video_and_a_picture_may_share_a_file_name(conn) -> None:
    """Different hosts keep them apart; the host is part of the identity."""
    db.store_media(conn, NOTE_ID, [{"url": "https://img/1.webp"}, {"url": "https://vid/1.webp"}])

    assert db.stored_media_urls(conn, NOTE_ID) == {"img/1.webp", "vid/1.webp"}


def test_media_change_is_detected_in_both_directions(conn) -> None:
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)
    db.store_media(conn, NOTE_ID, [{"url": "https://cdn/a.webp"}, {"url": "https://cdn/b.webp"}])

    assert not db.media_urls_changed(conn, NOTE_ID, ["https://cdn/a.webp", "https://cdn/b.webp"])
    assert db.media_urls_changed(conn, NOTE_ID, ["https://cdn/a.webp", "https://cdn/c.webp"])
    # A removed picture is a change too: the archive no longer describes the note.
    assert db.media_urls_changed(conn, NOTE_ID, ["https://cdn/a.webp"])


def test_a_discovered_url_never_requested_is_still_recorded(conn) -> None:
    db.store_media(conn, NOTE_ID, [{"url": "https://cdn/a.webp"}])

    assert db.stored_media_urls(conn, NOTE_ID) == {"cdn/a.webp"}
    assert db.downloaded_urls(conn, NOTE_ID) == set()


# --- runs and decisions ----------------------------------------------------


def a_run(conn, **kwargs) -> int:
    return db.start_run(conn, kind="screen", keywords=["川西秋色"], **kwargs)


def test_a_run_records_its_query_and_finishes(conn) -> None:
    run_id = a_run(conn, data_root="/tmp/xhs", filters={"排序": "最新"})
    db.finish_run(conn, run_id, totals={"candidates": 20})

    row = db.run_row(conn, run_id)
    assert row["kind"] == "screen"
    assert row["keywords"] == "川西秋色"
    assert row["data_root"] == "/tmp/xhs"
    assert row["finished_at"] is not None


def test_candidates_keep_page_order_and_start_pending(conn) -> None:
    run_id = a_run(conn)
    db.add_run_items(
        conn,
        run_id,
        [
            {"noteId": "a" * 24, "rank": 2, "href": "https://x/2", "keyword": "川西秋色"},
            {"noteId": "b" * 24, "rank": 1, "href": "https://x/1"},
        ],
    )

    assert [item["note_id"] for item in db.run_items(conn, run_id)] == ["b" * 24, "a" * 24]
    assert all(item["decision"] == db.DECISION_PENDING for item in db.run_items(conn, run_id))


def test_re_screening_a_run_does_not_lose_a_judgement(conn) -> None:
    run_id = a_run(conn)
    db.add_run_items(conn, run_id, [{"noteId": "a" * 24, "rank": 1, "href": "https://x/1"}])
    db.store_note(conn, a_note(noteId="a" * 24), status=db.STATUS_SCREENED)
    db.decide(conn, run_id, ["a" * 24], db.DECISION_KEEP)

    # The same note reappears, at a different rank.
    db.add_run_items(conn, run_id, [{"noteId": "a" * 24, "rank": 7, "href": "https://x/1"}])

    item = db.run_items(conn, run_id)[0]
    assert item["decision"] == db.DECISION_KEEP
    assert item["rank"] == 7


def test_keeping_a_note_also_moves_the_note_status(conn) -> None:
    run_id = a_run(conn)
    db.add_run_items(conn, run_id, [{"noteId": NOTE_ID, "rank": 1, "href": URL}])
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)

    assert db.decide(conn, run_id, [NOTE_ID], db.DECISION_KEEP, reason="有时间表和机位") == 1

    item = db.run_items(conn, run_id, decision=db.DECISION_KEEP)[0]
    assert item["reason"] == "有时间表和机位"
    assert item["decided_at"] is not None
    assert db.note_row(conn, NOTE_ID)["status"] == db.STATUS_APPROVED


def test_deciding_about_a_note_that_was_never_a_candidate_is_a_no_op(conn) -> None:
    """`run_items` must keep describing what the search actually returned."""
    run_id = a_run(conn)
    db.add_run_items(conn, run_id, [{"noteId": NOTE_ID, "rank": 1, "href": URL}])

    assert db.decide(conn, run_id, ["f" * 24], db.DECISION_KEEP) == 0
    assert [item["note_id"] for item in db.run_items(conn, run_id)] == [NOTE_ID]


def test_a_reason_is_kept_when_a_later_call_omits_one(conn) -> None:
    run_id = a_run(conn)
    db.add_run_items(conn, run_id, [{"noteId": NOTE_ID, "rank": 1, "href": URL}])
    db.store_note(conn, a_note())
    db.decide(conn, run_id, [NOTE_ID], db.DECISION_DROP, reason="只有风景照")
    db.decide(conn, run_id, [NOTE_ID], db.DECISION_DROP)

    assert db.run_items(conn, run_id)[0]["reason"] == "只有风景照"


def test_only_keep_and_drop_are_judgements(conn) -> None:
    run_id = a_run(conn)
    with pytest.raises(ValueError, match="判断"):
        db.decide(conn, run_id, [NOTE_ID], "maybe")


def test_an_unknown_status_is_refused(conn) -> None:
    db.store_note(conn, a_note())
    with pytest.raises(ValueError, match="未知状态"):
        db.set_status(conn, NOTE_ID, "almost-done")


def test_each_part_leaves_its_own_timestamp(conn) -> None:
    db.store_note(conn, a_note(), status=db.STATUS_SCREENED)
    for column in ("media", "comments", "collected"):
        db.touch(conn, NOTE_ID, column)

    row = stored(conn)
    assert row["last_media_at"] and row["last_comments_at"] and row["last_collected_at"]


def test_an_unknown_timestamp_is_refused(conn) -> None:
    db.store_note(conn, a_note())
    with pytest.raises(ValueError, match="未知时间戳"):
        db.touch(conn, NOTE_ID, "whenever")


def test_deleting_a_run_takes_its_items_with_it(conn) -> None:
    run_id = a_run(conn)
    db.add_run_items(conn, run_id, [{"noteId": NOTE_ID, "rank": 1, "href": URL}])

    conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    assert db.run_items(conn, run_id) == []


# --- comments --------------------------------------------------------------


def test_a_thread_read_records_how_complete_it_was(conn) -> None:
    db.store_comments_meta(
        conn,
        NOTE_ID,
        {"declaredTotal": 610, "collected": 10, "repliesCollected": 4,
         "threadEnded": False, "possiblyIncomplete": True, "capturedAt": "2026-10-07T00:00:00+00:00"},
    )

    row = db.comments_row(conn, NOTE_ID)
    assert row["declared_total"] == 610
    assert row["collected"] == 10
    assert row["possibly_incomplete"] == 1

    # A second read replaces the first rather than accumulating.
    db.store_comments_meta(conn, NOTE_ID, {"collected": 30})
    assert db.comments_row(conn, NOTE_ID)["collected"] == 30
