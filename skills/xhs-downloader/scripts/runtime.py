"""CLI, snapshot and ref-matching helpers shared by the Xiaohongshu scripts.

Nothing here knows a selector: page semantics live in `locators.yaml`, and every
rule is resolved against the current snapshot at call time.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml

PLAYBOOK_DIR = Path(__file__).resolve().parents[1]

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
    with (PLAYBOOK_DIR / "locators.yaml").open(encoding="utf-8") as source:
        return yaml.safe_load(source)


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
    return int(config["scroll"]["comments"].get("snapshot_limit", 3000))


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


def contains_rect(container: dict, child: dict) -> bool:
    outer, inner = container.get("rect") or {}, child.get("rect") or {}
    return (
        outer.get("x", 0) <= inner.get("x", 0)
        and outer.get("y", 0) <= inner.get("y", 0)
        and outer.get("x", 0) + outer.get("width", 0)
        >= inner.get("x", 0) + inner.get("width", 0)
        and outer.get("y", 0) + outer.get("height", 0)
        >= inner.get("y", 0) + inner.get("height", 0)
    )
