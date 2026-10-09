"""CLI, snapshot and ref-matching helpers shared by the Xiaohongshu scripts.

Nothing here knows a selector: page semantics live in `locators.yaml`, and every
rule is resolved against the current snapshot at call time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

SKILL_DIR = Path(__file__).resolve().parents[1]

# Where a collection lands when nothing says otherwise. One root holds the notes
# and the index together so a run can be moved or deleted as a unit.
DEFAULT_DATA_ROOT = Path.home() / "Documents" / "travel_agent"
DATA_ROOT_ENV = "XHS_DATA_ROOT"
NOTES_DIRNAME = "notes"
DB_FILENAME = "xhs.db"

# The one definition of a search URL, shared by `discover.py` and `download.py`
# so the run that harvests a page and the run that revisits a note from it are
# looking at the same site. `source` and
# `type` are what the site's own results page sends; they are NOT filters and
# changing them does not change the sort or the note type — measured 2026-10-07,
# adding `sort=`/`noteType=` here left the server-rendered `searchContext` at
# `general`/`0`. See the repo's `docs/xhs-筛选机制与实测.md`.
SEARCH_URL_TEMPLATE = (
    "https://www.xiaohongshu.com/search_result"
    "?keyword={keyword}&source=web_search_result_notes&type=51"
)

NOTE_ID_RE = re.compile(r"/(?:explore|search_result|discovery/item)/([0-9a-f]{24})")

# Xiaohongshu is a single-page app: during an in-page route change the content
# script's message port drops for a moment and the identical call succeeds right
# afterwards. These are transport failures, not page failures, so they are
# retried here instead of failing an unattended run at the first hiccup.
TRANSIENT_MARKERS = (
    "Receiving end does not exist",
    "Could not establish connection",
    "Extension disconnected",
    "Daemon socket not found",
    # Navigating a note away leaves the previous document in the back/forward
    # cache, which closes the content script's port mid-call.
    "back/forward cache",
    "message channel is closed",
)
CHROME_AGENT_ATTEMPTS = 5
CHROME_AGENT_BACKOFF = 1.5


def is_transient(message: str) -> bool:
    return any(marker in message for marker in TRANSIENT_MARKERS)


def chrome_agent(
    *args: str, attempts: int | None = None, backoff: float | None = None
) -> dict:
    """Run the chrome-agent CLI, retrying only transport failures.

    A page-level error (stale ref, hidden element) is raised on the first try so
    the caller still sees the real reason.
    """
    attempts = CHROME_AGENT_ATTEMPTS if attempts is None else attempts
    backoff = CHROME_AGENT_BACKOFF if backoff is None else backoff
    last = "chrome-agent 调用失败"
    for attempt in range(attempts):
        result = subprocess.run(
            ["chrome-agent", *args, "--json"], capture_output=True, text=True, check=False
        )
        if result.returncode:
            last = (result.stderr or result.stdout).strip()
        else:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                last = f"无法解析 chrome-agent 输出: {result.stdout[:200]}"
            else:
                if not payload.get("error"):
                    return payload
                last = str(payload["error"])
        if attempt + 1 >= attempts or not is_transient(last):
            break
        time.sleep(backoff)
    raise RuntimeError(last)


def note_id(url: str | None) -> str | None:
    """Extract the note ID from any of Xiaohongshu's note URL shapes."""
    match = NOTE_ID_RE.search(url or "")
    return match.group(1) if match else None


def load_locators() -> dict:
    with (SKILL_DIR / "locators.yaml").open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def data_root(cli_value: str | Path | None = None) -> Path:
    """Resolve the directory a run's notes and database live under.

    Precedence is `--data-root`, then `$XHS_DATA_ROOT`, then
    `~/Documents/travel_agent` — the CLI wins over the environment so a one-off
    run can be redirected without unsetting what the session is configured with.
    Returned unresolved-but-expanded: `~` in a CLI value has to work, and the
    path is printed in reports, where the user's own spelling reads better than
    a canonical one. Nothing is created here.
    """
    value = cli_value or os.environ.get(DATA_ROOT_ENV) or DEFAULT_DATA_ROOT
    return Path(value).expanduser()


def notes_dir(root: str | Path, cli_value: str | Path | None = None) -> Path:
    """Where the note directories go: `<root>/notes/` unless overridden."""
    if cli_value:
        return Path(cli_value).expanduser()
    return Path(root) / NOTES_DIRNAME


# Characters no filesystem wants in a name. `/` is the separator, NUL ends a
# path outright, and the rest are Windows' own set — harmless on macOS but the
# archive is meant to be copyable.
UNSAFE_FILENAME_RE = re.compile(r'[/\\\0:*?"<>|\r\n\t]')
TITLE_IN_DIRNAME = 40


def note_dir_name(note_id: str, title: str | None, limit: int = TITLE_IN_DIRNAME) -> str:
    """The directory name for a note: `<noteId>_<title>`, made safe.

    The id leads and is what makes the name unique, so two notes sharing a title
    never collide and a nameless note still gets a directory. The title is for
    the person reading the folder listing, so it is cut rather than dropped.
    """
    text = one_line(UNSAFE_FILENAME_RE.sub(" ", title or ""), limit).strip(" ._")
    return f"{note_id}_{text}" if text else note_id


NOTE_FILENAME = "note.json"


def find_note_dir(
    notes_root: str | Path, note_id: str, known_dir: str | Path | None = None
) -> Path | None:
    """The directory a note already lives in, or None if it has none.

    The index is the fast answer and the disk is the true one, so a directory
    named by `note_dir` counts only while it still holds the note. The glob is
    the fallback for an index that was deleted (it is a cache — see `db.py`) or
    written by an older build that named directories by id alone.
    """
    notes_root = Path(notes_root)
    candidates = []
    if known_dir:
        candidates.append(Path(known_dir))
    candidates.append(notes_root / note_id)
    candidates.extend(sorted(notes_root.glob(f"{note_id}_*")))
    return next((path for path in candidates if (path / NOTE_FILENAME).is_file()), None)


def db_path(root: str | Path, cli_value: str | Path | None = None) -> Path:
    """Where the index lives: `<root>/xhs.db` unless `--db` names another file."""
    if cli_value:
        return Path(cli_value).expanduser()
    return Path(root) / DB_FILENAME


def add_data_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the two storage flags every entry point accepts.

    One helper rather than two copies: the flags have to mean the same thing in
    `discover.py` and `download.py`, or a discovery's index is not the one the
    download reads.
    """
    parser.add_argument(
        "--data-root",
        type=Path,
        help=f"Notes and database root (default: ${DATA_ROOT_ENV} or {DEFAULT_DATA_ROOT})",
    )
    parser.add_argument(
        "--db",
        type=Path,
        help=f"Index file (default: <data-root>/{DB_FILENAME})",
    )


WHITESPACE_RE = re.compile(r"\s+")


def one_line(text: object, limit: int | None = None) -> str:
    """Collapse whitespace — cards and bodies carry newlines — and optionally cut.

    Cutting is always at the end and marked, because an excerpt that stops
    mid-sentence without saying so reads as a truncated note rather than a
    truncated view of one.
    """
    collapsed = WHITESPACE_RE.sub(" ", str(text or "")).strip()
    if limit is not None and len(collapsed) > limit:
        return collapsed[: limit - 1] + "…"
    return collapsed


def snapshot(tab_id: int, limit: int | None = None) -> dict:
    """Snapshot the page, optionally raising the element ceiling.

    The ceiling matters for the comment thread: one comment row is about twenty
    elements, so the 500-element default exposes roughly 22 rows however far the
    page is scrolled. The comment path therefore asks for a larger `limit`;
    everything else keeps the default.

    The cap keeps the top-N elements in on-screen order, so raising it only ever
    adds elements (measured on note 6a6df219 with the thread scrolled to its
    end: `--limit 500` returned y ∈ [-12073, -7956], `--limit 3000` returned
    the whole page plus `matched=3626, truncated=true`). Whatever is cut is at
    the BOTTOM of that order, not above the viewport.
    """
    args = ["page", "snapshot", "--tab-id", str(tab_id), "--scope", "full"]
    if limit is not None:
        args += ["--limit", str(limit)]
    return chrome_agent(*args)


def comment_snapshot_limit(config: dict) -> int:
    # The fallback tracks `locators.yaml`: this number is the ceiling on how many
    # comments a run can see, so a config that loses the key must not fall back
    # to the value that could not reach 10.
    return int(config["scroll"]["comments"].get("snapshot_limit", 8000))


def comment_snapshot(tab_id: int, config: dict) -> dict:
    """A snapshot wide enough to hold a long comment thread."""
    return snapshot(tab_id, comment_snapshot_limit(config))


def extract_text(tab_id: int, ref: str | None, max_chars: int) -> dict:
    """Read an element's full text by ref.

    A snapshot element carries at most 200 characters, so anything longer than a
    label has to be read this way; `truncated` says whether `max_chars` cut it.
    """
    if not ref:
        return {"text": "", "length": 0, "truncated": False}
    result = chrome_agent(
        "page",
        "text",
        "--tab-id",
        str(tab_id),
        "--ref",
        ref,
        "--max-chars",
        str(max_chars),
    )
    return {
        "text": result.get("text", ""),
        "length": int(result.get("length", 0)),
        "truncated": bool(result.get("truncated")),
    }


def deduplicate(items: list[dict], key: str = "url") -> list[dict]:
    output = []
    seen = set()
    for item in items:
        value = item.get(key)
        if not value or value in seen:
            continue
        seen.add(value)
        output.append(item)
    return output


def media_name(url: str | None) -> str:
    """A media file's stable identity: its host plus its file name.

    The CDN re-signs every URL on every read, and the signature is a path segment
    in front of the file name. Measured 2026-10-07, ONE picture of one note:

        …/202610071719/259fbf23…/1040g2sg31igk0d22085g4a6gt97n16m17s9uko0!nd_dft_wlteh_webp_3
        …/202610071730/61550091…/1040g2sg31igk0d22085g4a6gt97n16m17s9uko0!nd_dft_wlteh_webp_3

    Two URLs, one picture; the `!nd_dft_…` tail is the CDN's encoding request
    rather than part of the id. Only the id is stable, so only the id may decide
    "already downloaded" — comparing whole URLs makes every refresh fetch every
    picture again and land it beside the first copy as `-2`, `-3`, …

    The host stays IN. Two files of different kinds can share a name
    (`sns-webpic…/1040g3aaa.webp` against a video of the same name), and the two
    errors are not equally bad: calling one file two costs a duplicate download,
    which is visible and recoverable, while calling two files one silently drops
    a picture. Where the choice is between those, this errs toward the download.

    **Idempotent by contract**: `media_name(media_name(u)) == media_name(u)`. The
    index stores names and re-reads them through this function, so a second pass
    that stripped the host would turn every stored `host/file` into `file` and
    make two different files look like one.
    """
    text = (url or "").strip()
    if "//" not in text:
        # Already a name, in either the stored `host/file` form or bare `file`.
        return text
    parts = urlparse(text)
    tail = (parts.path or "").rstrip("/").rsplit("/", 1)[-1].split("!", 1)[0]
    return f"{parts.netloc}/{tail}" if parts.netloc and tail else ""


def tab_source(tab_id: int) -> dict:
    """The tab a collection ran against, for the verification file."""
    tabs = chrome_agent("tabs", "list").get("tabs", [])
    tab = next((item for item in tabs if item.get("id") == tab_id), None)
    if not tab:
        raise RuntimeError(f"Tab not found: {tab_id}")
    return {"tabId": tab_id, "url": tab.get("url", ""), "title": tab.get("title")}


def _class_names(element: dict) -> set[str]:
    return set(str(element.get("className") or "").split())


def matches(element: dict, rule: dict) -> bool:
    if rule.get("tag") and element.get("tag") != rule["tag"]:
        return False
    classes = _class_names(element)
    if any(name not in classes for name in rule.get("class_contains", [])):
        return False
    # Xiaohongshu marks a reply with both `comment-item` and `comment-item-sub`,
    # so the only way to name the top-level comment is by what it lacks.
    if any(name in classes for name in rule.get("class_excludes", [])):
        return False
    visible = bool(element.get("states", {}).get("visible"))
    if rule.get("visible") is not None and visible != rule["visible"]:
        return False
    rect = element.get("rect") or {}
    if rect.get("width", 0) < rule.get("min_width", 0):
        return False
    if rect.get("height", 0) < rule.get("min_height", 0):
        return False
    href = element.get("href")
    if rule.get("href_path_prefix") or rule.get("required_query_keys"):
        if not href:
            return False
        parsed = urlparse(href)
        if rule.get("href_path_prefix") and not parsed.path.startswith(
            rule["href_path_prefix"]
        ):
            return False
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(not query.get(key) for key in rule.get("required_query_keys", [])):
            return False
    return True


def find_all(snapshot: dict, rule: dict) -> list[dict]:
    return [element for element in snapshot.get("elements", []) if matches(element, rule)]


def find_first(snapshot: dict, rule: dict) -> dict | None:
    return next(iter(find_all(snapshot, rule)), None)


def find_open_detail(page: dict, config: dict) -> dict | None:
    """Return the element proving a note detail is on screen, if any.

    Xiaohongshu renders a note either as an overlay over the results page or as
    a standalone page depending on window width, and the two layouts do not
    share a container class. `detail.open_markers` lists the alternatives.
    """
    names = config["detail"].get("open_markers") or ["container"]
    for name in names:
        rule = config["detail"].get(name)
        if rule and (found := find_first(page, rule)):
            return found
    return None


def contains_rect(container: dict, child: dict, tolerance: float = 0.0) -> bool:
    """Whether `child` sits inside `container`'s rectangle.

    `tolerance` is not a fudge factor for sloppy rules, it is there because the
    site rounds its own boxes. Measured on the search filter panel (2026-10-07):
    the `发布时间` group's own box is 71px tall while the option row inside it
    ends ONE pixel lower, so strict containment dropped the entire group — and
    with it the only recency filter the page has. Callers that group by geometry
    pass a pixel or two of slack; callers that separate two rows of the same
    list (the comment tree) keep the strict default, where slack could adopt a
    sibling. Hiding this inside a global default would have quietly loosened
    those too.
    """
    outer, inner = container.get("rect") or {}, child.get("rect") or {}
    return (
        outer.get("x", 0) - tolerance <= inner.get("x", 0)
        and outer.get("y", 0) - tolerance <= inner.get("y", 0)
        and outer.get("x", 0) + outer.get("width", 0) + tolerance
        >= inner.get("x", 0) + inner.get("width", 0)
        and outer.get("y", 0) + outer.get("height", 0) + tolerance
        >= inner.get("y", 0) + inner.get("height", 0)
    )
