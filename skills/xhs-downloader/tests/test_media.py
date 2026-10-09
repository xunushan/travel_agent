"""The carousel is a looping swiper, and two of its habits wrote wrong files.

Measured live on note 696117b2 on 2026-10-08, with the page's own pager reading
`1/18`: the container's first child was `swiper-slide-duplicate
swiper-slide-prev` — a copy of the LAST slide — and one slide came back under two
CDN transforms. Both facts are modelled here, and both have to be neutralised
before a file's number means what the note's text says it means.
"""

from __future__ import annotations

import json

import download
import media
import pytest
from fake_site import IMAGE_A, IMAGE_B, NOTE_A, FakeSite, note_page
from test_playbook import patch_browser

# A third picture, so "the note does not list it" can be told from "the note
# lists it second".
IMAGE_C = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/1040g3ccc.webp"

NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_A}?xsec_token=tok"


def run_download(monkeypatch, site: FakeSite, tmp_path, argv: list[str]) -> int:
    """Drive the one CLI that downloads, against the fake site."""
    patch_browser(monkeypatch, site)
    monkeypatch.setattr(
        "sys.argv",
        ["download.py", "--tab-id", "1", "--data-root", str(tmp_path),
         "--url", NOTE_URL, *argv],
    )
    return download.main()


def note_dir_of(tmp_path) -> "Path":
    """The note's directory, whose name the title decides."""
    (found,) = (tmp_path / "notes").glob(f"{NOTE_A}_*")
    return found


def whitelisted(site: FakeSite) -> list[str]:
    """The URLs handed to `page download-images`, in the order they were passed.

    Chrome numbers each download by that order, so this IS the file numbering.
    """
    return [
        args[index + 1]
        for args in site.calls
        if args[:2] == ("page", "download-images")
        for index, word in enumerate(args)
        if word == "--url"
    ]


def test_one_picture_under_two_transforms_is_one_picture() -> None:
    """The loop duplicate was served as the same id under a second transform.

    A `src` comparison sees two pictures and downloads both; only the id is
    stable, so only the id may decide.
    """
    first = {"src": IMAGE_A + "!nd_dft_wlteh_webp_3"}
    second = {"src": IMAGE_A + "!nc_n_webp_mw_1"}

    assert media.deduplicate_pictures([first, second]) == [first]


def test_pictures_follow_the_notes_order_and_unlisted_ones_trail() -> None:
    """A picture the note's list does not mention is moved, never dropped."""
    dom = [{"src": IMAGE_B}, {"src": IMAGE_A}, {"src": IMAGE_C}]
    order = [media.media_name(IMAGE_A), media.media_name(IMAGE_B)]

    assert media.order_like_note(dom, order) == [
        {"src": IMAGE_A}, {"src": IMAGE_B}, {"src": IMAGE_C},
    ]


def test_no_note_order_leaves_the_discovery_alone() -> None:
    dom = [{"src": IMAGE_B}, {"src": IMAGE_A}]

    assert media.order_like_note(dom, []) == dom


def test_a_rotated_discovery_is_numbered_the_way_the_note_lists_it(
    monkeypatch, tmp_path
) -> None:
    """The bug end to end: `-001` has to be the note's own first picture.

    The fake state lists A then B while the fake page yields B first, which is
    what the real carousel does when its loop duplicate of the last slide leads.
    """
    site = FakeSite(
        keywords={},
        notes={NOTE_A: note_page(NOTE_A, title="九寨沟一日游轻徒步攻略", body="正文")},
        tmp_path=tmp_path,
    )
    site.images = [IMAGE_B, IMAGE_A]

    assert run_download(monkeypatch, site, tmp_path, [
        "--part", "image", "--prefix", NOTE_A,
    ]) == 0

    assert whitelisted(site) == [IMAGE_A, IMAGE_B]
    recorded = json.loads((note_dir_of(tmp_path) / "downloads.json").read_text())
    assert [item["url"] for item in recorded["downloads"]] == [IMAGE_A, IMAGE_B]
    assert all(item["kind"] == "image" for item in recorded["downloads"])


def test_the_cover_is_the_notes_first_picture_not_the_loop_duplicate(
    monkeypatch, tmp_path
) -> None:
    """`collect_cover` takes `images[0]`, so the order has to be right first."""
    site = FakeSite(
        keywords={},
        notes={NOTE_A: note_page(NOTE_A, title="九寨沟一日游轻徒步攻略", body="正文")},
        tmp_path=tmp_path,
    )
    site.images = [IMAGE_B, IMAGE_A]

    assert run_download(monkeypatch, site, tmp_path, [
        "--part", "cover", "--prefix", NOTE_A,
    ]) == 0

    assert whitelisted(site) == [IMAGE_A]
    assert [path.name for path in sorted(note_dir_of(tmp_path).glob("cover.*"))]
