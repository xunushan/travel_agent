"""Phase two: what it collects, and what it refuses to collect twice.

The interesting behaviour is all in the decision that precedes the browser work,
so these tests drive `batch.collect_all` through a fake site and then read the
index back. Two of them are the requirement this phase exists for: a second run
downloads no picture it already has, and a run whose parts are all on disk does
not open a page at all.
"""

from __future__ import annotations

import json
from urllib.parse import quote

import batch
import collect
import db
import pytest
import runtime
import search
from test_playbook import patch_browser
from test_search import NOTE_A, FakeSite, data_args, note_page

RUN = 1
NOTE_FILE = "note.json"
DOWNLOADS_FILE = "downloads.json"
COMMENTS_FILE = "comments.json"


@pytest.fixture
def site(tmp_path) -> FakeSite:
    """One note carrying two pictures, found under one keyword."""
    return FakeSite(
        keywords={"川西秋色": [(NOTE_A, "川西赏秋时间表", "09-27")]},
        notes={NOTE_A: note_page(NOTE_A, title="川西赏秋时间表", body="10月中下旬开始，川西进入最佳观赏期。")},
        tmp_path=tmp_path,
    )


def note_href(site: FakeSite) -> str:
    """The href of the one card, in the shape phase one stores it."""
    site.navigate(runtime.SEARCH_URL_TEMPLATE.format(keyword=quote("川西秋色")))
    page = site.snapshot()
    return next(
        element["href"] for element in page["elements"]
        if (element.get("className") or "").startswith("cover")
    )


def note_dir_of(tmp_path):
    return runtime.notes_dir(tmp_path / "xhs-batch") / NOTE_A


def open_db(tmp_path):
    return db.open_db(runtime.db_path(runtime.data_root(str(tmp_path / "xhs-batch"))))


def run_batch(monkeypatch, site, argv: list[str]) -> int:
    patch_browser(monkeypatch, site)
    monkeypatch.setattr("sys.argv", ["batch.py", *argv])
    return batch.main()


def batch_args(tmp_path, **overrides) -> list[str]:
    flags = {
        "--tab-id": "1",
        "--run-id": str(RUN),
        "--interval": "0",
        "--part": "body,media",
        **overrides,
    }
    out: list[str] = []
    for key, value in flags.items():
        out.extend([key, str(value)])
    return out + data_args(tmp_path, "xhs-batch")


def seeded(monkeypatch, site, tmp_path) -> FakeSite:
    """The state phase one leaves behind: a run with the note approved, and the
    note screened — body and cover read, pictures not downloaded, in the index.

    Built by running phase one's own collection against the fake rather than by
    hand-writing a row, so the stored content hash and counts are exactly what
    phase two will read back. A hand-written row that quietly disagreed would
    turn every dedup test into a test of `REFRESH_STATS`.
    """
    patch_browser(monkeypatch, site)
    href = note_href(site)
    note_dir = note_dir_of(tmp_path)
    facts: dict = {}
    site.navigate(href)
    note = collect.collect_note(
        1, note_dir=note_dir, output_path=note_dir / NOTE_FILE,
        parts=("body", "cover"), prefix=NOTE_A, facts=facts,
    )
    with open_db(tmp_path) as conn:
        run_id = db.start_run(conn, kind="screen", data_root=None,
                              keywords=["川西秋色"], filters={})
        db.add_run_items(conn, run_id, [{"noteId": NOTE_A, "rank": 1, "href": href,
                                         "keyword": "川西秋色"}])
        db.decide(conn, run_id, [NOTE_A], db.DECISION_KEEP, reason="有时间表")
        assert run_id == RUN
        db.store_note(conn, note, note_dir=note_dir, status=db.STATUS_SCREENED,
                      media=search.screening_media({"facts": facts}))
    return site


# --- the input -------------------------------------------------------------


def test_the_run_supplies_the_notes_a_plan_would_have(monkeypatch, site, tmp_path) -> None:
    seeded(monkeypatch, site, tmp_path)
    with open_db(tmp_path) as conn:
        entries = batch.keep_entries(conn, RUN)

    assert entries == [{"noteId": NOTE_A, "url": note_href(site), "keywords": ["川西秋色"]}]


def test_an_unknown_run_is_refused(tmp_path) -> None:
    with open_db(tmp_path) as conn:
        with pytest.raises(SystemExit, match="不存在"):
            batch.keep_entries(conn, 99)


def test_only_approved_notes_are_collected(monkeypatch, site, tmp_path) -> None:
    """A drop is a decision too, and phase two has to honour it."""
    seeded(monkeypatch, site, tmp_path)
    with open_db(tmp_path) as conn:
        db.decide(conn, RUN, [NOTE_A], db.DECISION_DROP, reason="只有风景照")

        assert batch.keep_entries(conn, RUN) == []


# --- which parts still need doing ------------------------------------------


def test_missing_artifacts_names_what_is_not_on_disk(tmp_path) -> None:
    note_dir = tmp_path / NOTE_A
    note_dir.mkdir()
    (note_dir / NOTE_FILE).write_text("{}", encoding="utf-8")

    assert batch.artifacts_missing(note_dir, ("body", "media", "comments")) == [
        "media", "comments",
    ]
    assert batch.artifacts_missing(note_dir, ("body",)) == []


def test_nothing_changed_still_finishes_what_was_never_done() -> None:
    """A screened note has an unchanged body AND no pictures yet."""
    requested = ("body", "media", "comments")

    assert batch.parts_to_run(requested, db.SKIP_DUP, batch.DEDUP_SKIP, ["media"]) == ("media",)
    assert batch.parts_to_run(requested, db.SKIP_DUP, batch.DEDUP_SKIP, []) == ()
    # The body is never in the answer: it has already been read by the time the
    # comparison exists, and reading it is what produced the comparison.
    assert batch.parts_to_run(("body",), db.RECOLLECT, batch.DEDUP_SKIP, []) == ()
    # A change means run everything that was asked for, not only what is absent.
    assert batch.parts_to_run(requested, db.RECOLLECT, batch.DEDUP_SKIP, ["comments"]) == (
        "media", "comments",
    )
    assert batch.parts_to_run(requested, db.SKIP_DUP, batch.DEDUP_REFRESH, []) == (
        "media", "comments",
    )
    assert batch.parts_to_run(requested, db.SKIP_DUP, batch.DEDUP_FORCE, []) == (
        "media", "comments",
    )


def test_a_finished_note_needs_no_page_but_an_unfinished_one_does() -> None:
    collected = {"status": db.STATUS_COLLECTED}
    unfinished = {"possibly_incomplete": True}

    assert batch.already_done(collected, [], None, batch.DEDUP_SKIP)
    assert not batch.already_done(None, [], None, batch.DEDUP_SKIP)
    assert not batch.already_done({"status": db.STATUS_SCREENED}, [], None, batch.DEDUP_SKIP)
    assert not batch.already_done(collected, ["media"], None, batch.DEDUP_SKIP)
    # The one thing a file check cannot see: the previous read said it stopped
    # early, so the thread is worth reading again.
    assert not batch.already_done(collected, [], unfinished, batch.DEDUP_SKIP)
    # And an explicit refresh defeats the fast path even for a finished note —
    # that is what it is for.
    assert not batch.already_done(collected, [], None, batch.DEDUP_REFRESH)


def test_media_rows_prefer_what_was_actually_fetched() -> None:
    rows = batch.media_rows({
        "images": [{"src": "https://a/1"}, {"src": "https://a/2"}],
        "downloads": [
            {"url": "https://a/1", "state": "complete", "filename": "images/1.webp"},
            {"url": "https://a/2", "state": "failed"},
        ],
    })

    assert rows == [
        {"url": "a/1", "kind": "image", "state": "complete",
         "filename": "images/1.webp"},
        {"url": "a/2", "kind": "image", "state": "failed", "filename": None},
    ]


def test_media_rows_do_not_forget_a_picture_the_run_skipped() -> None:
    """The failure this guards, found by an end-to-end run.

    A skipped URL has no download entry to describe it, so it came back as
    merely `discovered` — which drops the file from the index, makes
    `downloaded_urls` say it is missing, and fetches it again on the next run.
    And on the one after that, for ever.
    """
    rows = batch.media_rows(
        {"images": [{"src": "https://a/1"}, {"src": "https://a/2"}]},
        known={"a/1": "images/cover.webp"},
    )

    assert rows == [
        {"url": "a/1", "kind": "image", "state": "complete",
         "filename": "images/cover.webp"},
        {"url": "a/2", "kind": "image", "state": "discovered", "filename": None},
    ]


def test_media_rows_match_a_download_to_its_picture_across_a_re_signed_url() -> None:
    """The page's copy and the download's copy are signed separately.

    Matched by URL they would look like two things — one picture discovered with
    no download (recorded `discovered`, forgetting the file), plus one download
    with no picture. Matched by name they are one picture with its file.
    """
    rows = batch.media_rows({
        "images": [{"src": "https://cdn/202610071719/aaa/pic.webp"}],
        "downloads": [
            {"url": "https://cdn/202610071730/bbb/pic.webp", "state": "complete",
             "filename": "images/cover.webp"},
        ],
    })

    assert rows == [
        {"url": "cdn/pic.webp", "kind": "image", "state": "complete",
         "filename": "images/cover.webp"},
    ]


def test_media_rows_keep_what_the_whitelist_did_not_cover() -> None:
    """A video is media too, and it never appears in the picture discovery."""
    rows = batch.media_rows({
        "images": [{"src": "https://a/1"}],
        "downloads": [
            {"url": "https://a/1", "state": "complete", "filename": "images/1.webp"},
            {"url": "https://v/1", "kind": "video", "state": "complete", "filename": "videos/1.mp4"},
        ],
    })

    assert rows[-1] == {"url": "v/1", "kind": "video", "state": "complete",
                        "filename": "videos/1.mp4"}


# --- a run -----------------------------------------------------------------


def test_a_collection_run_writes_the_note_and_indexes_its_pictures(
    monkeypatch, site, tmp_path
) -> None:
    seeded(monkeypatch, site, tmp_path)

    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 0

    note_file = note_dir_of(tmp_path) / NOTE_FILE
    assert json.loads(note_file.read_text(encoding="utf-8"))["content"] == (
        "10月中下旬开始，川西进入最佳观赏期。"
    )
    with open_db(tmp_path) as conn:
        row = db.note_row(conn, NOTE_A)
        assert row["status"] == db.STATUS_COLLECTED
        assert row["excerpt"].startswith("10月中下旬")
        assert row["last_media_at"]
        # The picture phase one had not fetched is now on disk, and the one it
        # had is not listed as needing anything.
        assert db.downloaded_urls(conn, NOTE_A) == {"sns-webpic-qc.xhscdn.com/1040g3aaa.webp",
                                                   "sns-webpic-qc.xhscdn.com/1040g3bbb.webp"}


def test_a_second_run_downloads_no_picture_it_already_has(
    monkeypatch, site, tmp_path
) -> None:
    """The requirement the whole index exists for."""
    seeded(monkeypatch, site, tmp_path)
    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 0
    downloaded_once = site.download_count
    # Phase one fetched the cover; phase two fetched the other picture.
    assert downloaded_once >= 2
    images = note_dir_of(tmp_path) / "images"
    on_disk = sorted(path.name for path in images.glob("*"))

    assert run_batch(monkeypatch, site, batch_args(tmp_path, **{"--dedup": "refresh"})) == 0

    # The second run discovers the same pictures and asks for none of them: the
    # URLs were known, and `skip_urls` is what keeps them from being fetched
    # twice. Nothing is fetched, so nothing new lands — a re-fetch would leave
    # the old `-2` copy, which is exactly what the index exists to prevent.
    assert site.download_count == downloaded_once
    assert sorted(path.name for path in images.glob("*")) == on_disk


def test_a_force_run_fetches_them_again(monkeypatch, site, tmp_path) -> None:
    seeded(monkeypatch, site, tmp_path)
    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 0
    downloaded_once = site.download_count

    assert run_batch(monkeypatch, site, batch_args(tmp_path, **{"--dedup": "force"})) == 0

    assert site.download_count > downloaded_once


def test_a_collected_note_with_all_its_files_opens_no_page(
    monkeypatch, site, tmp_path
) -> None:
    seeded(monkeypatch, site, tmp_path)
    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 0
    assert run_batch(monkeypatch, site, batch_args(tmp_path, **{"--dedup": "refresh"})) == 0
    site.calls.clear()

    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 0

    assert not [call for call in site.calls if call[:2] == ("tabs", "navigate")]
    assert not [call for call in site.calls if call[:2] == ("page", "download-images")]


def test_a_missing_file_brings_the_note_back_to_the_browser(
    monkeypatch, site, tmp_path
) -> None:
    """The index saying "collected" is not enough; the files have to be there."""
    seeded(monkeypatch, site, tmp_path)
    assert run_batch(monkeypatch, site, batch_args(tmp_path, **{"--dedup": "refresh"})) == 0
    (note_dir_of(tmp_path) / DOWNLOADS_FILE).unlink()
    site.calls.clear()

    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 0

    assert [call for call in site.calls if call[:2] == ("tabs", "navigate")]


def test_a_note_that_will_not_open_is_recorded_as_failed(monkeypatch, site, tmp_path) -> None:
    seeded(monkeypatch, site, tmp_path)
    broken = dict(site.notes)
    broken.pop(NOTE_A)
    site.notes = broken
    monkeypatch.setattr("discover.NOTE_OPEN_TIMEOUT", 0.05)

    assert run_batch(monkeypatch, site, batch_args(tmp_path)) == 1

    with open_db(tmp_path) as conn:
        assert db.note_row(conn, NOTE_A)["status"] == db.STATUS_FAILED


def test_a_plan_still_works_without_a_run(monkeypatch, site, tmp_path) -> None:
    """The old entry point, for a caller that has its own list."""
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps([{"noteId": NOTE_A, "url": note_href(site),
                                 "keywords": ["川西秋色"]}]), encoding="utf-8")

    assert run_batch(monkeypatch, site, [
        "--tab-id", "1", "--plan", str(plan), "--interval", "0", "--part", "body",
        *data_args(tmp_path, "xhs-batch"),
    ]) == 0

    assert (tmp_path / "xhs-batch" / "notes" / NOTE_A / "note.json").is_file()
    with open_db(tmp_path) as conn:
        assert db.note_row(conn, NOTE_A)["status"] == db.STATUS_COLLECTED


def test_the_notes_land_where_phase_one_put_them(monkeypatch, site, tmp_path) -> None:
    """No `--output-root`: both phases must agree on the layout, or phase two
    writes a second copy of every note next to the one phase one made."""
    seeded(monkeypatch, site, tmp_path)
    monkeypatch.setattr(runtime, "data_root", lambda cli_value=None: tmp_path / "xhs-batch")
    root = runtime.notes_dir(tmp_path / "xhs-batch")
    root.mkdir(parents=True, exist_ok=True)

    assert run_batch(monkeypatch, site, [
        "--tab-id", "1", "--run-id", str(RUN), "--interval", "0", "--part", "body",
        *data_args(tmp_path, "xhs-batch"),
    ]) == 0

    assert (root / NOTE_A / "note.json").is_file()
