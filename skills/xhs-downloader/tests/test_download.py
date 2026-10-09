"""Tests for `download.py`: the two rules, and what they cost.

The point of this half is that a repeat run is cheap and an edited note is not
missed. Both are claims about *when a page gets opened*, so most of these tests
count page opens rather than inspect files — a run that downloaded the right
pictures by opening every note anyway has not done its job.

The rules:
1. have it, skip it; don't, get it — and a note with nothing missing opens no
   page at all;
2. changed, refresh it — which can only be discovered by opening the page, so
   `--check-update` is the caller's way of asking for one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import download
import media
from fake_site import IMAGE_A, IMAGE_B, NOTE_A, FakeSite, note_page
from test_playbook import patch_browser

NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_A}?xsec_token=tok"
TITLE = "九寨沟一日游轻徒步攻略"


@pytest.fixture(autouse=True)
def no_render_wait(monkeypatch):
    """The carousel's render wait is real seconds against a real page; here it
    would only cost the suite 25s every time a fake page lists no picture."""
    monkeypatch.setattr(media, "IMAGE_READY_TIMEOUT", 0.0)


def a_site(tmp_path: Path, body: str = "正文", title: str = TITLE) -> FakeSite:
    return FakeSite(
        keywords={},
        notes={NOTE_A: note_page(NOTE_A, title=title, body=body)},
        tmp_path=tmp_path,
    )


def a_video_site(tmp_path: Path) -> FakeSite:
    """A note whose only picture is its player's poster frame."""
    site = FakeSite(
        keywords={},
        notes={NOTE_A: note_page(NOTE_A, title=TITLE, body="正文", video=True)},
        tmp_path=tmp_path,
    )
    site.images = [IMAGE_A]  # the poster, and nothing else
    return site


def run(monkeypatch, capsys, site, tmp_path, *argv) -> tuple[int, list[dict]]:
    """One `download.py` run, returning its exit code and its JSON report."""
    patch_browser(monkeypatch, site)
    monkeypatch.setattr(
        "sys.argv",
        ["download.py", "--tab-id", "1", "--data-root", str(tmp_path), "--json", *argv],
    )
    code = download.main()
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else []


def note_dir_of(tmp_path: Path) -> Path:
    (found,) = (tmp_path / "notes").glob(f"{NOTE_A}_*")
    return found


def page_opens(site: FakeSite) -> int:
    return sum(1 for call in site.calls if call[:2] == ("page", "snapshot"))


# --- rule 1: have it, skip it ------------------------------------------------


def test_a_repeat_request_opens_no_page(monkeypatch, capsys, tmp_path) -> None:
    """The whole reason the state lives on disk: the second run is free."""
    site = a_site(tmp_path)
    _, first = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")
    assert first[0]["action"] == "new" and first[0]["status"] == "OK"

    site.calls.clear()
    _, second = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    assert second[0]["action"] == "skip"
    assert page_opens(site) == 0, "a note with nothing missing must not be opened"
    assert site.calls == [], "and must not touch the browser at all"


def test_the_skip_names_the_directory_it_found(monkeypatch, capsys, tmp_path) -> None:
    site = a_site(tmp_path)
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")
    _, second = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    assert second[0]["noteDir"] == str(note_dir_of(tmp_path))
    assert second[0]["parts"] == {"image": "已存在"}


def test_a_missing_part_is_fetched_without_repeating_the_rest(monkeypatch, capsys, tmp_path) -> None:
    site = a_site(tmp_path)
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "note")
    assert not (note_dir_of(tmp_path) / "downloads.json").exists()

    _, second = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    assert second[0]["action"] == "fill"
    assert second[0]["parts"] == {"image": "已下载"}
    assert note_dir_of(tmp_path) / "images" in [
        path.parent for path in note_dir_of(tmp_path).rglob("*.webp")
    ]


def test_a_part_that_is_asked_for_but_absent_is_a_failure(monkeypatch, capsys, tmp_path) -> None:
    """A picture that failed is never reported as a note without pictures."""
    site = a_site(tmp_path)
    site.images = []

    code, report = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    assert code == 1
    assert report[0]["status"] == "FAILED"
    assert report[0]["parts"]["image"] == "未完成"
    assert "未识别出笔记原图" in " ".join(report[0]["warnings"])


def test_a_video_notes_poster_is_its_cover_and_not_a_gallery(monkeypatch, capsys, tmp_path) -> None:
    """The player's cover frame is a picture of the note and every filter passes
    it honestly — but the note has no gallery, and `images/` holding one copy of
    the cover is how a reader is told this video note has a picture in it."""
    site = a_video_site(tmp_path)
    parts = "note,cover,image,video"  # `all` would need a comment thread to read

    code, report = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", parts)

    assert code == 0 and report[0]["status"] == "OK"
    note_dir = note_dir_of(tmp_path)
    assert (note_dir / "cover.webp").is_file(), "the poster is still the cover"
    assert not list((note_dir / "images").glob("*")), "and it is not also a gallery"
    assert report[0]["parts"]["image"] == "无此件", "and the report says so"
    assert "未识别出笔记原图" not in " ".join(report[0]["warnings"])


def test_a_video_note_with_no_gallery_is_not_a_gap(monkeypatch, capsys, tmp_path) -> None:
    """A poster-only note must still read as complete, or every later run would
    open the page again looking for pictures that are never coming."""
    site = a_video_site(tmp_path)
    parts = "note,cover,image,video"
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", parts)

    stored = json.loads((note_dir_of(tmp_path) / "downloads.json").read_text(encoding="utf-8"))
    assert stored["examined"] == ["image", "video"]

    site.calls.clear()
    _, second = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", parts)

    assert second[0]["action"] == "skip"
    assert site.calls == []


def test_a_picture_note_reports_no_video_rather_than_a_download(monkeypatch, capsys, tmp_path) -> None:
    """The mirror of the video note's empty gallery — and the same rule: a part
    the page answered "none" for was not downloaded."""
    site = a_site(tmp_path)

    _, report = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image,video")

    assert report[0]["parts"]["video"] == "无此件"
    assert report[0]["parts"]["image"] == "已下载"


# --- rule 2: changed, refresh it ---------------------------------------------


def test_an_edited_note_refreshes_every_requested_part(monkeypatch, capsys, tmp_path) -> None:
    site = a_site(tmp_path)
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")
    assert (note_dir_of(tmp_path) / "images").is_dir()

    # The note is edited on the site; the stored fingerprint no longer matches.
    site.notes[NOTE_A] = note_page(NOTE_A, title=TITLE, body="改过的正文，加了一句话。")
    site.calls.clear()
    _, second = run(
        monkeypatch, capsys, site, tmp_path,
        "--url", NOTE_URL, "--part", "image", "--check-update",
    )

    assert second[0]["action"] == "refresh"
    assert second[0]["changed"] is True
    assert page_opens(site) > 0


def test_check_update_opens_the_page_and_says_it_was_unchanged(monkeypatch, capsys, tmp_path) -> None:
    """Nothing is missing, so only an explicit ask gets the page opened."""
    site = a_site(tmp_path)
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    site.calls.clear()
    _, second = run(
        monkeypatch, capsys, site, tmp_path,
        "--url", NOTE_URL, "--part", "image", "--check-update",
    )

    assert second[0]["action"] == "check"
    assert page_opens(site) > 0, "--check-update is the whole point of this path"


def test_an_unchanged_note_is_not_reported_as_a_fill(monkeypatch, capsys, tmp_path) -> None:
    """`fill` means files were fetched. An opened-but-quiet page is not a fill."""
    site = a_site(tmp_path)
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    _, second = run(
        monkeypatch, capsys, site, tmp_path,
        "--url", NOTE_URL, "--part", "image", "--check-update",
    )

    assert second[0]["parts"] == {"image": "已存在"}


# --- the note's directory ----------------------------------------------------


def test_the_directory_is_named_after_the_title(monkeypatch, capsys, tmp_path) -> None:
    site = a_site(tmp_path)
    _, report = run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")

    assert Path(report[0]["noteDir"]).name == f"{NOTE_A}_{TITLE}"


def test_a_renamed_note_keeps_its_old_directory(monkeypatch, capsys, tmp_path) -> None:
    """`downloads.json` records absolute paths, so renaming the directory would
    make every picture in it read as missing and be fetched again."""
    site = a_site(tmp_path)
    run(monkeypatch, capsys, site, tmp_path, "--url", NOTE_URL, "--part", "image")
    before = note_dir_of(tmp_path)

    site.notes[NOTE_A] = note_page(NOTE_A, title="改过的标题", body="正文")
    _, second = run(
        monkeypatch, capsys, site, tmp_path,
        "--url", NOTE_URL, "--part", "image", "--check-update",
    )

    assert Path(second[0]["noteDir"]) == before
    assert before.is_dir()


def test_a_note_that_lost_its_url_is_reported_not_guessed(monkeypatch, capsys, tmp_path) -> None:
    """The detail URL carries the access token, so it is never rebuilt from the id."""
    site = a_site(tmp_path)
    code, report = run(monkeypatch, capsys, site, tmp_path, "--note-id", NOTE_A)

    assert code == 1
    assert "先 discover 再下载" in report[0]["reason"]


# --- what is on disk, as pure functions --------------------------------------


def test_missing_parts_of_a_note_that_was_never_downloaded_is_everything() -> None:
    assert download.missing_parts(None, ("note", "cover", "image")) == ["note", "cover", "image"]


def test_a_thread_that_said_it_was_cut_short_is_missing(tmp_path) -> None:
    path = tmp_path / "comments.json"
    path.write_text(json.dumps({"collected": 10}), encoding="utf-8")
    assert download.comments_complete(path) is True

    path.write_text(json.dumps({"collected": 10, "possiblyIncomplete": True}), encoding="utf-8")
    assert download.comments_complete(path) is False
    assert download.comments_complete(tmp_path / "absent.json") is False


def test_a_media_kind_nobody_looked_for_is_not_a_downloaded_part(tmp_path) -> None:
    """`examined` is what keeps "no video" from reading as "we have the video"."""
    (tmp_path / "downloads.json").write_text(
        json.dumps({"examined": ["image"], "downloads": []}), encoding="utf-8"
    )

    assert "image" in download.downloaded_parts(tmp_path)
    assert "video" not in download.downloaded_parts(tmp_path)


def test_a_file_that_vanished_is_not_a_downloaded_part(tmp_path) -> None:
    """A record saying `complete` about a file that is gone is the exact case a
    table-only check gets wrong."""
    gone = tmp_path / "images" / "a-001.webp"
    (tmp_path / "downloads.json").write_text(
        json.dumps({
            "examined": ["image"],
            "downloads": [{"kind": "image", "url": IMAGE_A, "state": "complete",
                           "filename": str(gone)}],
        }),
        encoding="utf-8",
    )

    assert "image" not in download.downloaded_parts(tmp_path)


def test_pruning_removes_only_files_the_new_version_does_not_have(tmp_path) -> None:
    images = tmp_path / "images"
    images.mkdir()
    (images / "keep.webp").write_bytes(b"x")
    (images / "stale.webp").write_bytes(b"x")

    removed = download.prune_stale(tmp_path, {"examined": ["image"], "downloads": [
        {"kind": "image", "state": "complete", "filename": str(images / "keep.webp")},
    ]})

    assert [Path(path).name for path in removed] == ["stale.webp"]
    assert sorted(path.name for path in images.iterdir()) == ["keep.webp"]


def test_a_kind_that_failed_is_never_pruned(tmp_path) -> None:
    """The old file may be the only copy of that picture, and losing it is
    strictly worse than keeping a stale one."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "old.webp").write_bytes(b"x")

    removed = download.prune_stale(tmp_path, {"examined": ["image"], "downloads": [
        {"kind": "image", "state": "failed", "filename": None},
    ]})

    assert removed == []
    assert (images / "old.webp").exists()


def test_a_kind_this_run_did_not_read_keeps_its_files(tmp_path) -> None:
    """Every version examined the kinds it read, and only those: a run that
    asked for the pictures must not delete the video it never looked at."""
    videos = tmp_path / "videos"
    videos.mkdir()
    (videos / "old.mp4").write_bytes(b"x")

    removed = download.prune_stale(tmp_path, {"examined": ["image"], "downloads": []})

    assert removed == []
    assert (videos / "old.mp4").exists()


def test_a_gallery_that_turned_out_to_be_empty_is_cleared_away(tmp_path) -> None:
    """The video-note case: the kind was read, the page said there are no
    pictures, so the poster an earlier version filed under `images/` is stale —
    and an empty `images/` left behind would still read as a gallery."""
    images = tmp_path / "images"
    images.mkdir()
    (images / "poster.webp").write_bytes(b"x")

    removed = download.prune_stale(tmp_path, {"examined": ["image", "video"], "downloads": []})

    assert [Path(path).name for path in removed] == ["poster.webp"]
    assert not images.exists()


# --- placing a new note's directory ------------------------------------------


def test_a_new_note_is_moved_to_the_directory_its_title_names(tmp_path) -> None:
    staging = tmp_path / NOTE_A
    staging.mkdir()
    (staging / "note.json").write_text("{}", encoding="utf-8")

    placed = download.place_note_dir(staging, tmp_path / f"{NOTE_A}_{TITLE}")

    assert placed.name == f"{NOTE_A}_{TITLE}"
    assert (placed / "note.json").is_file()
    assert not staging.exists()


def test_an_interrupted_runs_directory_is_merged_into_not_left_beside(tmp_path) -> None:
    """Same note by construction — the name starts with its id — so a second
    directory for it would be two halves of one note."""
    staging = tmp_path / NOTE_A
    staging.mkdir()
    (staging / "note.json").write_text("{}", encoding="utf-8")
    target = tmp_path / f"{NOTE_A}_{TITLE}"
    target.mkdir()
    (target / "cover.webp").write_bytes(b"x")

    placed = download.place_note_dir(staging, target)

    assert placed == target
    assert sorted(path.name for path in placed.iterdir()) == ["cover.webp", "note.json"]
    assert not staging.exists()


# --- reading the notes to work on --------------------------------------------


def test_entries_come_from_a_candidate_list_and_from_the_command_line(tmp_path) -> None:
    """A candidate list, a URL and a bare id are the three ways in, and the same
    note twice is one note — its page is worth opening only once."""
    other = "1" * 24
    listed = "2" * 24
    listing = tmp_path / "candidates.json"
    listing.write_text(json.dumps({"candidates": [
        {"noteId": NOTE_A, "url": NOTE_URL},
        {"noteId": NOTE_A, "url": "https://example.com/duplicate"},
        {"noteId": listed, "url": None},
    ]}), encoding="utf-8")
    args = type("Args", (), {
        "from_file": str(listing),
        "url": f"https://www.xiaohongshu.com/explore/{other}?xsec_token=t",
        "note_id": listed,
    })()

    entries = download.entries_from(args)

    assert [entry["noteId"] for entry in entries] == [NOTE_A, listed, other]
    assert entries[0]["url"] == NOTE_URL
    assert entries[1]["url"] is None
