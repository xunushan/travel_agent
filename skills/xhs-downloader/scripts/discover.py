#!/usr/bin/env python3
"""Discover and optionally open Xiaohongshu search results from a live snapshot."""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import parse_qs, urlparse

from runtime import (
    chrome_agent,
    contains_rect,
    find_all,
    find_first,
    find_open_detail,
    load_locators,
    note_id,
)

# Overridable by callers/tests; read at call time so the module constant can change.
DETAIL_CLOSE_TIMEOUT = 6.0
NOTE_OPEN_TIMEOUT = 15.0


def snapshot(tab_id: int) -> dict:
    return chrome_agent("page", "snapshot", "--tab-id", str(tab_id), "--scope", "full")


def activate_tab(tab_id: int) -> bool:
    """Bring the tab to the foreground, which the note's lazy loading requires.

    Chrome throttles background tabs' event loop, and Xiaohongshu appends the
    next batch of comments from that loop. Measured on a 610-comment note: while
    the tab sat behind another one, scrolling to the very bottom loaded nothing
    for a full minute (container rows stuck at 40, maxScrollY 7092); activating
    the tab and scrolling again grew it 40 -> 60 -> 80 -> 100 rows and
    maxScrollY 7092 -> 10521 -> 13523. Without this the thread silently stops at
    its first page however large `snapshot_limit` is.

    Returns whether the tab ended up active; a caller that cannot activate the
    tab is not stopped, it just will not see the thread grow.
    """
    try:
        result = chrome_agent("tabs", "activate", str(tab_id))
    except RuntimeError:
        return False
    return bool(result.get("active"))


def discover(page: dict, config: dict, limit: int) -> list[dict]:
    rules = config["search"]
    titles = {
        element["href"]: (element.get("text") or "").strip()
        for element in find_all(page, rules["result_title_link"])
        if element.get("href") and (element.get("text") or "").strip()
    }
    cards = find_all(page, rules["result_card"])
    results, seen = [], set()
    for element in find_all(page, rules["result_link"]):
        href = element["href"]
        if href in seen:
            continue
        seen.add(href)
        title = titles.get(href)
        if not title:
            enclosing = [card for card in cards if contains_rect(card, element)]
            if enclosing:
                title = (enclosing[0].get("text") or "").strip() or None
        query = parse_qs(urlparse(href).query, keep_blank_values=True)
        results.append(
            {
                "rank": len(results) + 1,
                "ref": element["ref"],
                "title": title,
                "href": href,
                "noteId": note_id(href),
                "hasAccessContext": all(
                    query.get(key) for key in rules["result_link"].get("required_query_keys", [])
                ),
            }
        )
        if len(results) >= limit:
            break
    return results


def detail_is_open(tab_id: int, config: dict) -> bool:
    return find_open_detail(snapshot(tab_id), config) is not None


def wait_for_detail_closed(tab_id: int, config: dict, timeout: float | None = None) -> bool:
    """Dismissal has a transition; only report success once the container is gone."""
    timeout = DETAIL_CLOSE_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    while True:
        if not detail_is_open(tab_id, config):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.6)


def close_detail_if_open(tab_id: int, config: dict) -> str | None:
    """Close an open note detail, returning the method that worked.

    The close control is hidden outright in Xiaohongshu's narrow layout, where
    `page click` fails with "Element not visible". A keypress on the detail
    itself is what actually dismisses the overlay there.

    Returns None when no detail was open. Raises when a detail is open and
    neither route closed it, because every later action would then land on the
    overlay instead of the page underneath.
    """
    page = snapshot(tab_id)
    marker = find_open_detail(page, config)
    if not marker:
        return None

    close = find_first(page, config["detail"]["close_control"])
    if close:
        try:
            chrome_agent("page", "click", "--tab-id", str(tab_id), "--ref", close["ref"])
        except RuntimeError:
            pass  # hidden close control; fall through to the keypress route
        else:
            if wait_for_detail_closed(tab_id, config):
                return "close_control"

    for keys in config["detail"].get("dismiss_keys", []):
        target = find_open_detail(snapshot(tab_id), config)
        if not target:
            return "already-closed"  # dismissed as a side effect of the click
        chrome_agent(
            "page", "keypress", "--tab-id", str(tab_id), "--ref", target["ref"],
            "--keys", keys,
        )
        if wait_for_detail_closed(tab_id, config):
            return f"keypress:{keys}"

    raise RuntimeError(
        "详情遮罩仍开着：关闭控件不可见，dismiss_keys 也没能关掉它。"
        "继续操作会点在遮罩上，因此中止。"
    )


def wait_for_note(tab_id: int, want: str, config: dict, timeout: float | None = None) -> bool:
    """Poll until the tab has actually rendered the note we asked for.

    A click on a covered card reports success while changing nothing, so the
    URL and the detail container are checked rather than the click result.
    """
    timeout = NOTE_OPEN_TIMEOUT if timeout is None else timeout
    deadline = time.time() + timeout
    while True:
        page = snapshot(tab_id)
        if note_id(page.get("url")) == want and find_open_detail(page, config):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.8)


def open_result(tab_id: int, selected: dict, mode: str, config: dict) -> dict:
    """Open one result and verify it landed, falling back to direct navigation."""
    want = selected.get("noteId") or note_id(selected.get("href"))
    if not want:
        raise RuntimeError(f"无法从 href 解析笔记 ID: {selected.get('href')}")

    activate_tab(tab_id)
    attempts = []
    for attempt_mode in [mode] + (["navigate"] if mode != "navigate" else []):
        if attempt_mode == "click":
            result = chrome_agent(
                "page", "click", "--tab-id", str(tab_id), "--ref", selected["ref"]
            )
        else:
            result = chrome_agent("tabs", "navigate", str(tab_id), selected["href"])
        attempts.append({"mode": attempt_mode, "result": result})
        if wait_for_note(tab_id, want, config):
            return {"rank": selected["rank"], "noteId": want, "mode": attempt_mode,
                    "attempts": attempts}

    raise RuntimeError(
        f"打开 rank {selected['rank']} 后没有落在目标笔记 {want}；"
        f"click 和 navigate 都试过，实际停留在他处。尝试记录: {attempts}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab-id", required=True, type=int)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--open-rank", type=int, help="Close an open detail overlay, then open this result"
    )
    parser.add_argument(
        "--open-mode",
        choices=("click", "navigate"),
        default="click",
        help="How to open --open-rank",
    )
    args = parser.parse_args()
    config = load_locators()

    closed_detail = close_detail_if_open(args.tab_id, config) if args.open_rank else None
    page = snapshot(args.tab_id)
    results = discover(page, config, max(1, args.limit))
    output = {
        "source": {"tabId": args.tab_id, "url": page.get("url")},
        "results": results,
        "truncatedSnapshot": bool(page.get("truncated")),
        "closedDetail": closed_detail,
    }
    if args.open_rank:
        selected = next((item for item in results if item["rank"] == args.open_rank), None)
        if not selected:
            if find_open_detail(page, config):
                reason = "当前仍停在笔记详情页，先关掉详情或导航回搜索结果页"
            elif note_id(page.get("url")):
                reason = "当前是笔记页而不是搜索结果页"
            else:
                reason = "当前页面没有可用的结果卡片"
            raise RuntimeError(
                f"rank {args.open_rank} 没有候选（本次只找到 {len(results)} 条）——{reason}："
                f"{page.get('url')}"
            )
        output["opened"] = open_result(args.tab_id, selected, args.open_mode, config)
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
