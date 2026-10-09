#!/usr/bin/env python3
"""Find and download one note's images or video, and record how it went.

`downloads.json` is the verification file: it keeps the discovered URLs, the
per-item download state and the Chrome-side filenames, which is everything
needed to check a download without re-running the collection. Nothing here
belongs in `note.json`, which is read as content.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

from runtime import chrome_agent, deduplicate, find_first, media_name, snapshot
from state import note_record, read_initial_state

# Never note content: author avatars, static UI assets, vector icons, and the
# pictures people attach to their comments.
EXCLUDED_IMAGE_MARKERS = (
    "sns-avatar-qc.xhscdn.com",
    "fe-static.xhscdn.com",
    "fe-platform.xhscdn.com",
    "picasso-static.xiaohongshu.com",
    "/avatar/",
    "/comment/",
    ".svg",
)

# A known-size image below this is a UI asset, never note content. Needed because
# the carousel scope is not purely note images: measured live on 6a6df219's
# sibling notes, a 48×48 `fe-platform` icon sits inside the slider container and
# was downloaded as slide 1. Only a KNOWN size is judged — the slides that have
# not mounted yet report null dimensions and must still be kept.
MIN_NOTE_IMAGE_SIDE = 200

# The note-image id that both of Xiaohongshu's CDN URL shapes carry:
#   <hash>/notes_pre_post/1040g…!nd_dft_wgth_webp_3   (carousel, transformed)
#   <hash>/1040g2sg325q572ls5gjg5q5muqoma1            (no path segment at all)
# Both were observed live on real 1080px note images.
NOTE_IMAGE_PATH_RE = re.compile(r"/1040g[0-9a-z]+", re.I)

# Seconds to keep waiting for a note's carousel to render after it opens.
IMAGE_READY_TIMEOUT = 25.0

DOWNLOAD_STATE_COMPLETE = "complete"

# The two kinds of media a note can carry, and the `--part` names they answer
# to. They are separate content parts because a caller often wants one without
# the other — a video note's poster images are not what someone asked for when
# they asked for the video.
KIND_IMAGE = "image"
KIND_VIDEO = "video"
MEDIA_KINDS = (KIND_IMAGE, KIND_VIDEO)

# Where each kind's files land under the note directory. Used both to organize a
# download and to tell an old entry's kind apart when one of them is re-run on
# its own.
KIND_DIRNAME = {KIND_IMAGE: "images", KIND_VIDEO: "videos"}

# The cover is its own content part with its own file at the note directory's
# root, deliberately independent of `images/`: the cover is slide 1, so a note
# whose cover and gallery are both downloaded stores that picture twice. The
# trade is intentional — neither part then has to know the other ran.
COVER_STEM = "cover"


def resolve_image_scope(
    page: dict, config: dict, fallback: str | None
) -> tuple[str | None, str | None]:
    """Pick the narrowest configured container that holds this note's media.

    Returns `(rule name, ref)`. The name is load-bearing: whatever the carousel
    holds is the note's own image by construction, which is a stronger signal
    than the URL shape. Only a broader fallback scope needs filtering.

    Most posts expose a slider, but single-image posts do not; scoping those to
    the detail container is what separates the note image from the surrounding
    avatars and recommendation thumbnails.
    """
    for name in config["detail"].get("image_scopes", ["image_media"]):
        element = find_first(page, config["detail"][name])
        if element:
            return name, element["ref"]
    return None, fallback


def page_images(tab_id: int, ref: str, load: bool = False) -> list[dict]:
    """List images inside `ref`.

    `--load` scrolls the container to force lazy images in, which the CLI
    documents as moving the page; it is opt-in for that reason, and only needed
    when the plain call returns nothing.
    """
    args = ["page", "images", "--tab-id", str(tab_id), "--ref", ref]
    if load:
        args.append("--load")
    return chrome_agent(*args).get("images", [])


def is_note_image(image: dict, scope_name: str | None = None) -> bool:
    """Decide whether a discovered image is the note's own content.

    Location mostly beats URL shape: nearly everything inside the carousel is a
    slide, whatever its URL looks like, so the narrower scope is trusted. Two
    things still get filtered there — page assets are excluded by host/path
    marker, and an image whose known size is thumbnail-sized is excluded by
    size (a real slide is ≥1080px on its long side and the unmounted ones report
    no size at all).

    The broader fallback scope has to be filtered by URL, and there size is
    useless on its own: an avatar is 360px and a comment picture is 640px, so
    both clear any useful threshold while sharing a CDN host with note images.
    """
    url = image.get("src", "")
    if any(marker in url for marker in EXCLUDED_IMAGE_MARKERS):
        return False
    sides = [side for side in (image.get("width"), image.get("height")) if side]
    if sides and max(sides) < MIN_NOTE_IMAGE_SIDE:
        return False
    if scope_name == "image_media":
        return True
    return "notes_pre_post/" in url or bool(NOTE_IMAGE_PATH_RE.search(url))


def poster_without_gallery(page: dict, config: dict) -> bool:
    """True for a video note: a player, and no carousel for it to sit beside.

    Xiaohongshu's video notes hang their cover frame on the player container as a
    CSS `background-image`. The gallery read finds it — with no
    `xhs-slider-container` the scope falls all the way to `detail.container`, and
    the poster is a note-shaped URL of a note-sized picture, so every filter it
    passes is passed honestly. Measured 2026-10-09 on note 6ab4f9b9: the one kept
    picture was recorded `background-image` while all 34 others (avatars, icons,
    comment pictures) were rejected, and the file it produced was byte-identical
    to that note's `cover.webp`.

    It is still not a gallery. It is the note's cover, which the `cover` part
    stores as `cover.<ext>` in its own right, and a video note whose `images/`
    holds one copy of its own cover reads as a note with one picture in it.

    Both halves are load-bearing. A video note that really does carry a carousel
    keeps its pictures — the slider scope wins and its slides are the note's own;
    a note with neither carousel nor player is a different, broken case that must
    keep its warning.
    """
    detail = config["detail"]
    player = detail.get("video_media")
    slider = detail.get("image_media")
    return bool(
        player
        and slider
        and find_first(page, player) is not None
        and find_first(page, slider) is None
    )


def deduplicate_pictures(images: list[dict]) -> list[dict]:
    """One entry per picture, not per URL.

    Comparing whole `src`s was enough while one picture meant one URL. The
    carousel is a looping swiper, and its duplicate of another slide can come
    back under a *different* CDN transform — measured 2026-10-08 on note
    696117b2, one slide arrived as both `…!nd_dft_wlteh_webp_3` and
    `…!nc_n_webp_mw_1`. Both then went to disk, the same picture once as `-001`
    and once as the last index. `media_name` drops the transform, so the id
    decides, and the clone collapses into the slide it duplicates.
    """
    seen: set[str] = set()
    output: list[dict] = []
    for image in images:
        name = media_name(image.get("src"))
        if not name or name in seen:
            continue
        seen.add(name)
        output.append(image)
    return output


def note_image_order(tab_id: int) -> list[str]:
    """The order the note itself lists its pictures in, as `media_name`s.

    DOM order is not that order, and the difference is not a fixed offset. The
    carousel's first child is a loop duplicate of the slide *before* the one on
    screen, so the discovery comes back rotated, and the rotation moves with the
    slide being shown: measured 2026-10-08 on note 696117b2 with the page's own
    pager reading `1/18`, the first element in the container was the duplicate
    `swiper-slide-duplicate swiper-slide-prev` — the LAST slide — and the order
    came out `[18, 1, 2, …, 17]`. Every file number after the first was
    therefore one above the picture's own number, which breaks a note whose
    text points at `（图15）`.

    The state's `imageList` is the order the note declares and it does not move.
    Returns `[]` when the page carries no state, leaving the DOM order as the
    only thing the caller has.
    """
    try:
        record = note_record(read_initial_state(tab_id))
    except RuntimeError:
        return []
    images = (record or {}).get("imageList") or []
    return [media_name(image.get("urlDefault") or image.get("url")) for image in images]


def order_like_note(images: list[dict], order: list[str]) -> list[dict]:
    """Sort discovered pictures into the order the note lists them in.

    A picture the note's own list does not mention keeps its relative discovery
    order and goes last rather than being dropped: it still came out of the
    carousel scope, and losing it would cost a picture where merely misplacing
    it costs position. Sorting is stable, so ties keep discovery order too.
    """
    if not order:
        return images
    rank = {name: index for index, name in enumerate(order)}
    return sorted(
        images, key=lambda image: rank.get(media_name(image.get("src")), len(rank))
    )


def discover_note_images(
    tab_id: int, config: dict, fallback: str | None = None, timeout: float | None = None
) -> tuple[str | None, list[dict], list[dict]]:
    """List this note's own images, waiting for the carousel to finish mounting.

    The detail container exists well before its images do, and it re-renders
    again once the comment thread mounts, so a scope ref is resolved from a
    fresh snapshot each attempt rather than reused. Waiting is bounded: a scope
    that yields pictures of which none is note content ends the wait, because
    more time will not turn an avatar into a slide.

    A video note still yields one picture — its cover frame, hung on the player
    as a CSS background. It is returned like any other, because the `cover` part
    wants it; that it is not a gallery is `poster_without_gallery`'s call, made
    by the caller that knows which part it is collecting.
    """
    timeout = IMAGE_READY_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    scope_name: str | None = None
    scope: str | None = None
    discovered: list[dict] = []
    while True:
        scope_name, scope = resolve_image_scope(snapshot(tab_id), config, fallback)
        if scope:
            # Keyed by picture id: `page images` spells each entry `src`, and a
            # `src`-only comparison keeps one picture twice. See
            # `deduplicate_pictures`.
            discovered = deduplicate_pictures(page_images(tab_id, scope))
            kept = [image for image in discovered if is_note_image(image, scope_name)]
            # Images exist but none is note content: waiting will not change it.
            if kept or discovered or time.time() >= deadline:
                order = note_image_order(tab_id)
                return (
                    scope,
                    order_like_note(kept, order),
                    order_like_note(discovered, order),
                )
        elif time.time() >= deadline:
            return scope, [], []
        time.sleep(2)


def organize_downloads(downloads: list[dict], directory: Path, warnings: list[str]) -> None:
    """Move only files confirmed as downloaded in this collection run.

    An existing file of the same name is REPLACED, not sidestepped. Chrome names
    each download `<prefix>-<index>.<ext>`, so re-collecting a note produces the
    same names, and the older `-2`/`-3` suffixes turned every retry into another
    copy of the same picture. Overwriting is the truthful behaviour for an
    archive that describes the note as it is now — the note is not a museum of
    its own past versions, and what the previous collection fetched is still
    described, item by item, by the `downloads.json` this run rewrites.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for item in downloads:
        if item.get("state") != DOWNLOAD_STATE_COMPLETE or not item.get("filename"):
            continue
        source = Path(item["filename"])
        if not source.is_file():
            warnings.append(f"下载完成但找不到文件，未归档：{source}")
            continue
        destination = directory / source.name
        shutil.move(str(source), destination)
        item["originalFilename"] = str(source)
        item["filename"] = str(destination)


def collect_cover(
    tab_id: int,
    config: dict,
    *,
    note_dir: Path,
    prefix: str,
    fallback: str | None = None,
) -> dict:
    """Download the note's first picture, as `cover.<ext>` in the note directory.

    The cover is slide 1 of the carousel, and it is often the information rather
    than the decoration — measured candidates carry timetables, route maps and
    viewpoint lists on their covers. It is a content part of its own because
    reading it is a separate decision from keeping the note.

    **It sits beside `images/`, not inside it.** Storing it as the archive's
    `images/<id>-001.<ext>` saved one file when a note's gallery was downloaded
    too — measured 2026-10-07, `cover.webp` and `684695b6…-001.webp` in one note
    directory had the same MD5. That saving made the two parts depend on each
    other: whether `cover` was already downloaded depended on whether `image`
    had run. Now each part answers for itself, and the one duplicated picture is
    the price.

    It is deliberately NOT taken from the state's `imageList`: `--url` is a
    whitelist over what `page images` discovered in a scope, so a URL the DOM
    never produced would match nothing. The picture itself is the carousel's
    first slide as discovered — the list is used only to SORT what was
    discovered, never to add to it (see `note_image_order`), which is also what
    keeps this from being the loop duplicate standing in front of slide 1.

    The returned `src` is the URL that was discovered, which is what the caller
    can record. `images` is the whole discovery, not just the slide that was
    fetched, so a caller comparing picture sets can tell a note whose gallery
    changed from one that did not.
    """
    warnings: list[str] = []
    scope, images, _ = discover_note_images(tab_id, config, fallback)
    if not scope or not images:
        warnings.append("未找到笔记首图，未能落封面")
        return {"src": None, "images": [], "downloads": [], "warnings": warnings}

    cover = images[0]
    downloads = chrome_agent(
        "page",
        "download-images",
        "--tab-id",
        str(tab_id),
        "--ref",
        scope,
        "--prefix",
        prefix,
        "--url",
        cover["src"],
    ).get("downloads", [])
    if not any(item.get("state") == DOWNLOAD_STATE_COMPLETE for item in downloads):
        warnings.append("封面下载未完成")
        return {"src": cover["src"], "images": images, "downloads": downloads, "warnings": warnings}

    note_dir.mkdir(parents=True, exist_ok=True)
    for item in downloads:
        if item.get("state") != DOWNLOAD_STATE_COMPLETE or not item.get("filename"):
            continue
        source = Path(item["filename"])
        if not source.is_file():
            warnings.append(f"封面下载完成但找不到文件：{source}")
            continue
        destination = note_dir / f"{COVER_STEM}{source.suffix}"
        shutil.move(str(source), destination)
        item["originalFilename"] = str(source)
        item["filename"] = str(destination)
    return {"src": cover["src"], "images": images, "downloads": downloads, "warnings": warnings}


def carry_forward(verification: dict | None, drop: set[str]) -> dict:
    """The previous run's record of the media kinds this run is not touching.

    A run that only wants the pictures must not erase what the last one recorded
    about the video, and vice versa — `downloads.json` is the only record of
    what is on disk. Entries of a kind being re-read now are dropped instead,
    because the fresh read is a better answer than the old one.

    `examined` is carried with them: a kind the last run verified and this one
    did not touch is still verified, and dropping it would make the next run
    open the page again to re-learn what is already written down.
    """
    previous = verification or {}
    downloads = [
        item
        for item in previous.get("downloads") or []
        if (item.get("kind") or kind_of_entry(item)) not in drop
    ]
    return {
        "images": previous.get("images") or [],
        "audioVideo": previous.get("audioVideo") or [],
        "downloads": downloads,
        "examined": [k for k in previous.get("examined") or [] if k not in drop],
    }


def kind_of_entry(item: dict) -> str:
    """Which content part an old download entry belongs to.

    Entries written before `kind` existed are told apart by where their file
    went: `videos/` is a video, everything else is a picture.
    """
    if item.get("kind"):
        return item["kind"]
    filename = str(item.get("filename") or "")
    return KIND_VIDEO if f"/{KIND_DIRNAME[KIND_VIDEO]}/" in filename else KIND_IMAGE


def media_missing(verification: dict | None, kinds: Iterable[str]) -> list[str]:
    """Which requested media kinds are not fully on disk, and why.

    "Downloaded" for a media kind means the same thing here as everywhere else:
    an entry in `downloads.json` that says `complete` **and** whose file is
    still there. A note whose pictures were deleted has `downloads.json` still
    claiming them, which is exactly the case a table-only check gets wrong.

    `examined` is what keeps "we found no video" apart from "we never looked for
    one". A note whose pictures were downloaded by a run that asked for
    `--part image` has no video entries — which is not evidence of having the
    video, nor of the note not having one. That run recorded that it examined
    `image` only, so `video` still counts as missing and the page is opened
    again.
    """
    previous = verification or {}
    examined = set(previous.get("examined") or [])
    problems: list[str] = []
    entries = previous.get("downloads") or []
    for kind in kinds:
        if kind not in examined:
            problems.append(f"{kind}: 从未在页面上核对过")
            continue
        broken = [
            item
            for item in entries
            if kind_of_entry(item) == kind
            and (
                item.get("state") != DOWNLOAD_STATE_COMPLETE
                or not item.get("filename")
                or not Path(item["filename"]).is_file()
            )
        ]
        if broken:
            problems.append(f"{kind}: {len(broken)} 条未完成或文件已不在")
    return problems


def collect_media(
    tab_id: int,
    page: dict,
    config: dict,
    *,
    note_dir: Path,
    prefix: str,
    kinds: Iterable[str] = MEDIA_KINDS,
    fallbacks: dict[str, str | None] | None = None,
    known_files: dict[str, str] | None = None,
    force: bool = False,
) -> dict:
    """Discover the requested media kinds, download what is missing, report it.

    Runs before the comment thread is scrolled on purpose: refs for the visible
    regions are resolved from the snapshot taken right after the note opened,
    and the elements that sit at a positive on-screen position — the carousel
    among them — are the ones a long thread pushes out of a capped snapshot.

    `kinds` is which of `image` / `video` this run is responsible for. The kinds
    NOT named keep whatever the previous `downloads.json` recorded for them
    (`carry_forward`), so asking for one never un-records the other.

    `known_files` maps media NAME to the file already on disk for it —
    `downloaded_names` reads it out of this note's own `downloads.json`. Those
    pictures are left out of the whitelist so a refresh downloads only what
    changed, **and they are written back into the new `downloads.json` as
    `complete` entries**. That second half is what makes the skip survive: this
    file is the only record of what is on disk, so a refresh that dropped its
    skipped pictures from it would leave the next run believing the note had no
    pictures at all — and fetching every one of them again, beside the first
    copies as `-2`, `-3`, for ever. `force` turns the skip off, which is what a
    note that changed underneath us needs.

    The returned `examined` is which kinds the page gave an ANSWER for, and it
    is narrower than `kinds` on purpose. Asking is not answering: an image read
    that found no container, or a container that listed nothing at all, has not
    established that the note is pictureless — a lazy carousel that never
    mounted looks exactly like that — so the kind stays unverified and the next
    run looks again. The video case is the opposite and is why this is per kind:
    a picture note has no player, and that absence IS the answer — as is the
    absent gallery of a video note, whose one picture is the player's poster.
    """
    kinds = tuple(kinds)
    fallbacks = fallbacks or {}
    known_files = {} if force else (known_files or {})
    warnings: list[str] = []
    examined: list[str] = []
    images: list[dict] = []
    images_ref: str | None = None
    # A video note's only picture is the player's own poster frame, which the
    # `cover` part already owns. It is dropped from the gallery rather than from
    # the discovery, so the cover — which reads the same list — is untouched.
    poster_only = KIND_IMAGE in kinds and poster_without_gallery(page, config)
    if KIND_IMAGE in kinds:
        images_ref, images, discovered = discover_note_images(
            tab_id, config, fallbacks.get("image_media")
        )
        if poster_only:
            images = []
        # An empty gallery on a video note IS the page's answer, like the absent
        # player on a picture note — so the kind is verified and the next run
        # does not come back to re-read it.
        if images_ref and (images or discovered or poster_only):
            examined.append(KIND_IMAGE)

    audio_video: list[dict] = []
    media_ref: str | None = None
    if KIND_VIDEO in kinds:
        examined.append(KIND_VIDEO)
        player = find_first(page, config["detail"]["video_media"])
        media_ref = (player or {}).get("ref") or fallbacks.get("video_media")
        if media_ref:
            # The player often exposes only a blob: URL while the direct MP4
            # lives in the page-level structured data, so both are read, merged.
            scoped_media = chrome_agent(
                "page", "media", "--tab-id", str(tab_id), "--ref", media_ref
            ).get("media", [])
            page_media = chrome_agent("page", "media", "--tab-id", str(tab_id)).get(
                "media", []
            )
            audio_video = deduplicate(scoped_media + page_media)

    # Compared by picture name, not by URL: the CDN re-signs every URL on every
    # read, so a URL-only comparison sees a fresh picture every time and lands a
    # second copy of it beside the first as `-2`.
    pending = [image for image in images if media_name(image["src"]) not in known_files]
    # Everything already on disk gets an entry too, so the file describes the
    # whole gallery rather than only this run's work. Built in `images` order so
    # a refresh produces the same list in the same order as the run before it.
    downloads: list[dict] = [
        {
            "kind": KIND_IMAGE,
            "url": image["src"],
            "state": DOWNLOAD_STATE_COMPLETE,
            "filename": known_files[media_name(image["src"])],
        }
        for image in images
        if media_name(image["src"]) in known_files
    ]
    if KIND_IMAGE in kinds and images_ref and not images and not poster_only:
        warnings.append("未识别出笔记原图，已拒绝下载小图/表情/评论配图/页面资源")
    if KIND_IMAGE in kinds and not images_ref:
        warnings.append("未找到图片容器（detail.image_scopes 都没匹配上）")
    if KIND_IMAGE in kinds and pending and images_ref:
        # `--url` is the whitelist. Without it the extension falls back to every
        # image it discovered in the scope, which is how avatars, emoji and
        # recommendation thumbnails end up in the output.
        command = [
            "page", "download-images", "--tab-id", str(tab_id), "--ref", images_ref,
            "--prefix", prefix,
        ]
        for image in pending:
            command.extend(["--url", image["src"]])
        image_downloads = chrome_agent(*command).get("downloads", [])
        for item in image_downloads:
            item["kind"] = KIND_IMAGE
        organize_downloads(image_downloads, note_dir / KIND_DIRNAME[KIND_IMAGE], warnings)
        downloads.extend(image_downloads)
        if not any(
            item.get("state") == DOWNLOAD_STATE_COMPLETE for item in image_downloads
        ):
            warnings.append(
                "图片下载未完成：请重新确认图片容器是详情轮播，而非整张笔记或评论容器"
            )
    if KIND_VIDEO in kinds and media_ref:
        result = chrome_agent(
            "page",
            "download-media",
            "--tab-id",
            str(tab_id),
            "--prefix",
            prefix,
            "--timeout",
            "600",
        )
        media_downloads = result.get("downloads", [])
        for item in media_downloads:
            item["kind"] = KIND_VIDEO
        organize_downloads(media_downloads, note_dir / KIND_DIRNAME[KIND_VIDEO], warnings)
        downloads.extend(media_downloads)
        if result.get("unsupported"):
            warnings.append("存在当前无法直接下载的 blob/HLS/DASH/unsupported 媒体")

    # `carried` holds only the kinds this run did not touch, so nothing is
    # duplicated by appending it.
    carried = carry_forward(read_downloads(note_dir), set(kinds))
    downloads = downloads + carried["downloads"]
    return {
        "images": images if KIND_IMAGE in kinds else carried["images"],
        "audioVideo": audio_video if KIND_VIDEO in kinds else carried["audioVideo"],
        "downloads": downloads,
        # This run's answers plus the ones an earlier run already got.
        "examined": [k for k in MEDIA_KINDS if k in examined or k in carried["examined"]],
        # Discovered but already on disk, so deliberately not fetched again.
        "skipped": len(images) - len(pending),
        "warnings": warnings,
    }


def kind_absent(verification: dict | None, kind: str) -> bool:
    """Whether the page answered for this kind with "the note has none".

    `examined` says the question was asked; the absence of entries says the
    answer was nothing. Together they are the difference between a video note
    with no pictures — where `image` is a part that exists and is empty — and a
    picture note whose pictures merely failed to come down, which has entries
    and would be a lie to call "none".
    """
    entries = [
        item for item in (verification or {}).get("downloads") or []
        if kind_of_entry(item) == kind
    ]
    return kind in ((verification or {}).get("examined") or []) and not entries


def failed_downloads(downloads: list[dict]) -> list[dict]:
    """The items that did not land, which is what an agent has to report."""
    return [item for item in downloads if item.get("state") != DOWNLOAD_STATE_COMPLETE]


def downloaded_names(verification: dict | None) -> dict[str, str]:
    """`{media name: file}` for what a verification file says actually landed.

    This replaces the query that used to ask the `media` table. The table was a
    copy of this file, so a note whose file said one thing and whose rows said
    another had no way to be right; now there is one answer, and it is the one
    that sits beside the pictures.

    Keyed by `media_name`, not by URL: the CDN re-signs every URL on every read,
    so a URL-keyed answer would report every picture as new each run and land a
    second copy beside the first as `-2`.
    """
    names: dict[str, str] = {}
    for item in (verification or {}).get("downloads") or []:
        if item.get("state") != DOWNLOAD_STATE_COMPLETE or not item.get("filename"):
            continue
        name = media_name(item.get("url"))
        if name:
            names[name] = item["filename"]
    # A file the page never showed — a video, or a picture the discovery missed —
    # is still a file this note has, so `images` alone is not the whole answer.
    return names


def note_media_names(verification: dict | None) -> set[str]:
    """Every picture the verification file attributes to this note.

    Discovered counts, not downloaded: a picture that was deliberately skipped
    because it was already on disk must not read as removed from the gallery on
    the next run. Measured 2026-10-07 on a note that had been collected twice —
    the second read saw a fresh CDN signature for the same five pictures, and
    only the name is stable across the two reads.
    """
    names = set(downloaded_names(verification))
    for image in (verification or {}).get("images") or []:
        name = media_name(image.get("src"))
        if name:
            names.add(name)
    return names


def incomplete_downloads(verification: dict | None) -> list[dict]:
    """The requested files that are not on disk, whatever the reason.

    Distinct from `failed_downloads` in what it is FOR rather than what it
    matches: this is what `batch.collection_is_complete` consults before it
    stamps a note as collected. `schema/downloads-output.schema.json` fixes the
    four states, and only `complete` means the file is there.
    """
    return [
        item
        for item in (verification or {}).get("downloads") or []
        if item.get("state") != DOWNLOAD_STATE_COMPLETE
    ]


def write_downloads(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_downloads(note_dir: Path) -> dict | None:
    """The verification file beside a note, if that note has one."""
    path = note_dir / "downloads.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
