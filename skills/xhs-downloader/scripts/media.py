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
from pathlib import Path

from runtime import chrome_agent, deduplicate, find_first, media_name, snapshot

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


def discover_note_images(
    tab_id: int, config: dict, fallback: str | None = None, timeout: float | None = None
) -> tuple[str | None, list[dict], list[dict]]:
    """List this note's own images, waiting for the carousel to finish mounting.

    The detail container exists well before its images do, and it re-renders
    again once the comment thread mounts, so a scope ref is resolved from a
    fresh snapshot each attempt rather than reused. Waiting is bounded: a video
    note has a media scope but no note images at all, and is recognised as soon
    as the scope yields images that none of are note content.
    """
    timeout = IMAGE_READY_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    scope_name: str | None = None
    scope: str | None = None
    discovered: list[dict] = []
    while True:
        scope_name, scope = resolve_image_scope(snapshot(tab_id), config, fallback)
        if scope:
            # Images are keyed by `src`, not `url`; deduplicating on the wrong
            # field discards every one of them.
            discovered = deduplicate(page_images(tab_id, scope), key="src")
            kept = [image for image in discovered if is_note_image(image, scope_name)]
            # Images exist but none is note content: waiting will not change it.
            if kept or discovered or time.time() >= deadline:
                return scope, kept, discovered
        elif time.time() >= deadline:
            return scope, [], []
        time.sleep(2)


def organize_downloads(downloads: list[dict], directory: Path, warnings: list[str]) -> None:
    """Move only files confirmed as downloaded in this collection run.

    An existing file of the same name is REPLACED, not sidestepped. Chrome names
    each download `<prefix>-<index>.<ext>`, so re-collecting a note produces the
    same names, and the older `-2`/`-3` suffixes turned every retry into another
    copy of the same picture. Overwriting is the truthful behaviour for an
    archive that describes the note as it is now; the previous copies are what
    the `media` table in the database is for.
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
    """Download just the note's first picture, as `images/cover.<ext>`.

    Phase 1 hands the agent a file path per candidate instead of the image, and
    that first picture is often the information rather than the decoration — the
    measured candidates carry timetables, route maps and viewpoint lists on
    their covers. So it is worth having on disk; whether to look at it stays the
    agent's call, because reading an image costs real tokens.

    It is deliberately NOT taken from the state's `imageList`: `--url` is a
    whitelist over what `page images` discovered in a scope, so a URL the DOM
    never produced would match nothing. The carousel is discovered the same way
    the archive discovers it, and its first slide is the cover.

    The returned `src` is the URL that was discovered, which is what the caller
    records in the index. A caller comparing picture sets across runs has to key
    on the same URL the DOM produces, or a redirect inside the browser's own
    download would make an untouched note look like it had swapped pictures.
    `images` is the whole discovery, not just the slide that was fetched: phase
    one records every URL as belonging to this note, so that a later run with
    pictures to download does not read the rest of them as newly added.
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

    # One file per note, always the same name: this is a screening aid, so the
    # second run replaces the first rather than adding `-2`.
    directory = note_dir / "images"
    directory.mkdir(parents=True, exist_ok=True)
    for item in downloads:
        if item.get("state") != DOWNLOAD_STATE_COMPLETE or not item.get("filename"):
            continue
        source = Path(item["filename"])
        if not source.is_file():
            warnings.append(f"封面下载完成但找不到文件：{source}")
            continue
        destination = directory / f"cover{source.suffix}"
        shutil.move(str(source), destination)
        item["originalFilename"] = str(source)
        item["filename"] = str(destination)
    return {"src": cover["src"], "images": images, "downloads": downloads, "warnings": warnings}


def collect_media(
    tab_id: int,
    page: dict,
    config: dict,
    *,
    note_dir: Path,
    prefix: str,
    download_images: bool,
    download_media: bool,
    fallbacks: dict[str, str | None] | None = None,
    skip_urls: set[str] | None = None,
) -> dict:
    """Discover this note's media, download what was asked for, and report it.

    Runs before the comment thread is scrolled on purpose: refs for the visible
    regions are resolved from the snapshot taken right after the note opened,
    and the elements that sit at a positive on-screen position — the carousel
    among them — are the ones a long thread pushes out of a capped snapshot.

    `skip_urls` are images already on disk according to the database; they are
    left out of the whitelist so a refresh downloads only what changed.
    """
    fallbacks = fallbacks or {}
    skip_urls = skip_urls or set()
    warnings: list[str] = []
    images: list[dict] = []
    images_ref: str | None = None
    if download_images:
        images_ref, images, _ = discover_note_images(
            tab_id, config, fallbacks.get("image_media")
        )

    audio_video: list[dict] = []
    player = find_first(page, config["detail"]["video_media"])
    media_ref = (player or {}).get("ref") or fallbacks.get("video_media")
    if media_ref:
        # The player often exposes only a blob: URL while the direct MP4 lives in
        # the page-level structured data, so both scopes are read and merged.
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
    pending = [
        image for image in images
        if media_name(image["src"]) not in skip_urls
    ]
    downloads: list[dict] = []
    if download_images and images_ref and not images:
        warnings.append("未识别出笔记原图，已拒绝下载小图/表情/评论配图/页面资源")
    if download_images and not images_ref:
        warnings.append("未找到图片容器（detail.image_scopes 都没匹配上）")
    if download_images and pending and images_ref:
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
        organize_downloads(image_downloads, note_dir / "images", warnings)
        downloads.extend(image_downloads)
        if not any(
            item.get("state") == DOWNLOAD_STATE_COMPLETE for item in image_downloads
        ):
            warnings.append(
                "图片下载未完成：请重新确认图片容器是详情轮播，而非整张笔记或评论容器"
            )
    if download_media and media_ref:
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
        organize_downloads(media_downloads, note_dir / "videos", warnings)
        downloads.extend(media_downloads)
        if result.get("unsupported"):
            warnings.append("存在当前无法直接下载的 blob/HLS/DASH/unsupported 媒体")

    return {
        "images": images,
        "audioVideo": audio_video,
        "downloads": downloads,
        # Discovered but already on disk, so deliberately not fetched again.
        "skipped": len(images) - len(pending),
        "warnings": warnings,
    }


def failed_downloads(downloads: list[dict]) -> list[dict]:
    """The items that did not land, which is what an agent has to report."""
    return [item for item in downloads if item.get("state") != DOWNLOAD_STATE_COMPLETE]


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
