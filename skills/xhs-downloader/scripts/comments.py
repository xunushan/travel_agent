#!/usr/bin/env python3
"""Read one note's comment thread into `comments.json`.

The thread is read as a tree: a top-level comment carries its replies, and those
carry theirs. Nesting comes from two different places and they must not be
confused — the DOM knows which comment a reply belongs to, and only the text
knows which reply answered which.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from discover import activate_tab
from runtime import (
    chrome_agent,
    comment_snapshot,
    contains_rect,
    extract_text,
    find_all,
    find_first,
    load_locators,
)

# A reply to another reply keeps its target inline as "回复 <名字> : <正文>".
COMMENT_REPLY_TARGET = re.compile(r"^回复\s+(?P<target>.+?)\s*[:：]\s*(?P<body>.*)$", re.S)

# UI chrome inside a comment's innerText, never part of the comment body.
COMMENT_UI_TEXT = re.compile(
    r"^(?:共\s*\d+\s*条评论|赞|回复|作者|置顶评论|展开\s*\d+\s*条回复|\d+)$"
)
# Every comment ends with a time-and-place line, which terminates its body.
# Xiaohongshu writes a recent comment's time as a relative word instead of a
# number — "刚刚", "昨天", "前天" — so matching only `N天前` / `MM-DD` /
# `YYYY-MM-DD` lets that line, and the place on the line after it, leak into
# the body (seen live: a body ending "...哪个更好\n昨天 22:37上海").
COMMENT_TIME_OR_PLACE = re.compile(
    r"^(?:\d+\s*(?:秒|分钟|分|小时|天)前.*"
    # The day word alone is not enough: a body line may open with "今天去了…",
    # and cutting there would leave an empty comment. It counts only when the
    # clock time that Xiaohongshu always prints with it follows.
    r"|(?:今天|昨天|前天)\s*\d{1,2}:\d{2}.*"
    # Only on a line of its own; "刚刚拍的" is a body, and every relative-day
    # line observed live carries its clock time ("昨天 22:37上海").
    r"|刚刚"
    # Real calendar shapes only. A loose `\d{2}-\d{2}` read the lens "70-200的头"
    # as the date `70-20` with `0的头` for a place (observed live on note
    # 6a6df219, where that reply came back with an empty body).
    r"|\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]).*"
    r"|(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]).*"
    r"|\d{1,2}:\d{2}.*"
    r")$"
)
COMMENT_TOTAL = re.compile(r"共\s*(\d+)\s*条评论")
# Xiaohongshu prints this once the thread has no more rows to append. It is the
# only positive proof that the read reached the bottom.
COMMENT_THREAD_END = "- THE END -"

COMMENTS_FILENAME = "comments.json"


def comment_body_end(lines: list[str]) -> int:
    """Where a comment's body stops: the index of the time-and-place line.

    Two things must hold, and the second is the one a body's own text cannot be
    trusted to give. The line must look like a date or a relative time, and it
    must sit directly above the UI tail every comment carries (`赞` / its like
    count / `回复`) — or be the last line. Without that check a body opening
    with something date-shaped is eaten: "70-200的头" (a lens, seen live on note
    6a6df219) parsed as the date `70-20` followed by the place `0的头`, leaving
    the comment stored with an empty body.

    Returns len(lines) when nothing qualifies, which makes the whole rest of the
    row the body.
    """
    for index, line in enumerate(lines[1:], start=1):
        if not COMMENT_TIME_OR_PLACE.match(line):
            continue
        if index == len(lines) - 1 or COMMENT_UI_TEXT.match(lines[index + 1]):
            return index
    return len(lines)


def parse_comment(text: str, is_reply: bool = False) -> dict:
    """Split one comment's innerText into author, body and flags.

    Every comment ends with a time-and-place line followed by likes and a reply
    button, so that line terminates the body and the chrome above it is
    dropped. A reply has exactly the same line shape as a top-level comment
    (verified live: both read author / 作者 / body / time / likes / 回复), which
    is why the parent link has to come from the DOM and never from this text.

    A reply aimed at another reply carries its target inline as
    `回复 <名字> : <正文>`; that is split out so `replyTo` names the person being
    answered instead of polluting the body.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return {"author": "", "text": "", "pinned": False, "byAuthor": False,
                "replyTo": None}
    cut = comment_body_end(lines)
    header = lines[1:cut]
    body = "\n".join(line for line in header if not COMMENT_UI_TEXT.match(line))
    reply_to = None
    if is_reply:
        target = COMMENT_REPLY_TARGET.match(body)
        if target:
            reply_to = target.group("target")
            body = target.group("body").strip()
    return {
        "author": lines[0],
        "text": body,
        "pinned": "置顶评论" in header,
        "byAuthor": "作者" in header,
        "replyTo": reply_to,
    }


def read_comment(tab_id: int, ref: str, is_reply: bool = False) -> dict | None:
    """Read one comment, tolerating a ref that went stale mid-round."""
    try:
        return parse_comment(extract_text(tab_id, ref, 4000)["text"], is_reply)
    except RuntimeError:
        return None


def resolve_scroll_anchor(page: dict, config: dict) -> dict | None:
    """Pick a ref whose nearest scrollable ancestor is the comment thread.

    `page scroll --ref` walks up from the given element, so anchoring on the
    comments container moves the note scroller without naming it.

    The chain ends at the note body because the comments container is not always
    in the snapshot yet — on a note that has just opened, the slots can be
    filled by the results grid and by the `...` menu each comment row carries,
    leaving the container itself out of reach. The body sits in the same right
    half, above the thread, so it scrolls the same element once the thread
    exists.
    """
    rules = config["detail"]
    for name in rules.get("comments_scroll_anchors", ["comments_scroll_target"]):
        element = find_first(page, rules[name])
        if element:
            return element
    return None


def scroll_comments(tab_id: int, page: dict, config: dict) -> dict | None:
    """Advance the comment thread one step and report what the CLI saw.

    A note that has just opened can report "not moved" before its thread has
    laid out, so one settled retry is given before that is believed. Deciding
    whether to stop is left to the caller, which applies `stop_when` from
    `locators.yaml`.
    """
    scroll_config = config["scroll"]["comments"]
    step: dict | None = None
    for attempt in range(2):
        # Re-resolve each attempt: a retry follows a fresh snapshot, and a
        # re-rendered row has a new ref.
        anchor = resolve_scroll_anchor(
            page if attempt == 0 else comment_snapshot(tab_id, config), config
        )
        if not anchor:
            return None
        step = chrome_agent(
            "page",
            "scroll",
            "--tab-id",
            str(tab_id),
            "--ref",
            anchor["ref"],
            "--dy",
            str(scroll_config["dy"]),
        )
        if step.get("moved"):
            break
        if attempt == 0:
            time.sleep(1.5)
    return step


def expand_replies(tab_id: int, config: dict) -> list[str]:
    """Click "展开 N 条回复" until no such control is left in view.

    Collapsed replies are absent from the DOM entirely, so the control has to
    be pressed before they can be read, and each press reveals one batch. Only
    what is on screen can be clicked, so this runs again after every scroll
    rather than once up front.
    """
    rules = config["detail"]
    max_rounds = config["scroll"]["comments"].get("max_expand_rounds", 10)
    failures = 0
    for _ in range(max_rounds):
        button = find_first(comment_snapshot(tab_id, config), rules["comment_show_more"])
        if not button:
            return []
        try:
            chrome_agent("page", "click", "--tab-id", str(tab_id), "--ref", button["ref"])
        except RuntimeError:
            # The ref went stale, or the button vanished because the replies
            # opened; either way the next round resolves it afresh.
            failures += 1
            if failures >= 3:
                return ["展开回复连续失败，可能有折叠回复未读取"]
            continue
        failures = 0
        time.sleep(1.0)
    return [f"展开回复达到 {max_rounds} 轮上限，部分折叠回复可能未读取"]


def thread_size(tab_id: int, config: dict, page: dict | None = None) -> int | None:
    """How many comment rows the thread has rendered so far.

    Measured from the container's own text rather than from the snapshot: one
    comment row is about twenty elements, so a capped snapshot exposes only its
    first rows. `page text` returns the container's full innerText with no such
    cap, which is what makes "did the thread grow?" answerable at all.

    None means the container could not be resolved, which is NOT the same as
    "no new comments"; the caller must not read it as a stop signal.
    """
    container = find_first(
        page or comment_snapshot(tab_id, config), config["detail"]["comments"]
    )
    if not container:
        return None
    text = extract_text(tab_id, container["ref"], 200000)["text"]
    return sum(
        1 for line in text.splitlines() if COMMENT_TIME_OR_PLACE.match(line.strip())
    )


def number_replies(nodes: list[dict], depth: int = 1) -> int:
    """Number siblings, record their depth, and return the subtree size.

    Depth counts levels of reply below the comment being read, so a comment's
    direct replies are depth 1 and their own replies are depth 2. The
    top-level comment itself carries no `depth` — it is the thing the numbered
    rows hang from.

    Each node's `replyCount` is its whole subtree, not just its direct children,
    because that is what Xiaohongshu's own reply badge counts.
    """
    subtree = 0
    for position, node in enumerate(nodes, start=1):
        node["index"] = position
        node["depth"] = depth
        node["replyCount"] = number_replies(node["replies"], depth + 1)
        subtree += 1 + node["replyCount"]
    return subtree


def nest_replies(rows: list[dict]) -> tuple[list[dict], int]:
    """Rebuild the reply tree Xiaohongshu only implies.

    The DOM is flat: every reply of one comment is a sibling row inside a single
    `reply-container` (verified live — a parent's three replies all sat at the
    same x and width, one after another). A reply aimed at another reply is
    therefore distinguishable only by the `回复 X ：` prefix the page prints into
    its text, which `parse_comment` has already split out as `replyTo`.

    Rows arrive in chronological order, and that is what makes the target
    unambiguous: a row answers the most recent row already seen from the author
    it names. Naming someone who wrote nothing in this thread — a deleted reply,
    a nickname that never renders here, or the top-level commenter who only
    wrote the comment — attaches the row at the top instead, because there is no
    row for it to answer. Depth is evidence-based, never invented.

    The top-level commenter is deliberately not special-cased. Treating `回复
    <顶层评论作者>：` as "answers the comment itself" collapsed a real three-deep
    exchange on note 6a6df219 (康定-红海子-塔公这条线吧 → 我想去姑弄村… →
    姑弄村就在塔公) into a flat sibling of its own first reply.

    Returns the roots and the total number of rows.
    """
    roots: list[dict] = []
    latest: dict[str, dict] = {}
    for row in rows:
        node = {**row, "replies": []}
        target = row.get("replyTo")
        parent = latest.get(target) if isinstance(target, str) else None
        if parent is not None:
            parent["replies"].append(node)
        else:
            roots.append(node)
        latest[row["author"]] = node
    return roots, number_replies(roots)


def extract_comments(
    tab_id: int, config: dict, limit: int
) -> tuple[list[dict], list[str]]:
    """Read the comment thread, nesting each reply under what it answers.

    Returns at most `limit` top-level comments, each carrying its replies — and
    those replies their own, however deep the thread goes. A comment and its
    whole subtree count as one against the budget.

    The thread loads progressively, so this scrolls in steps and accumulates as
    it goes. Refs are re-issued whenever a row is re-rendered, so comments
    already read are recognised by author and body instead.
    """
    rules = config["detail"]
    scroll_config = config["scroll"]["comments"]
    max_idle_rounds = scroll_config.get("max_idle_rounds", 4)
    items: list[dict] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    rounds = 0
    last_size = -1
    idle_rounds = 0

    while len(items) < limit and rounds < scroll_config["max_steps"]:
        rounds += 1
        warnings.extend(expand_replies(tab_id, config))
        page = comment_snapshot(tab_id, config)
        # A note with no comments never renders a container, so without this the
        # loop would scroll the full `max_steps` for nothing. Verified on notes
        # that do have comments: the empty state is absent there.
        if find_first(page, rules["comments_empty"]):
            break
        container = find_first(page, rules["comments"])
        parents = find_all(page, rules["comment_parent"])
        if container:
            parents = [item for item in parents if contains_rect(container, item)]
        parents.sort(key=lambda item: (item.get("rect") or {}).get("y", 0))

        for parent in parents:
            if len(items) >= limit:
                break
            top = next(
                (
                    item
                    for item in find_all(page, rules["comment_item"])
                    if contains_rect(parent, item)
                ),
                None,
            )
            if not top:
                continue
            parsed = read_comment(tab_id, top["ref"])
            if not parsed:
                continue
            key = (parsed["author"], parsed["text"])
            if key in seen:
                continue
            seen.add(key)
            rows = [
                reply
                for candidate in sorted(
                    find_all(page, rules["comment_reply"]),
                    key=lambda item: (item.get("rect") or {}).get("y", 0),
                )
                if contains_rect(parent, candidate)
                # The top-level comment has a `comment-inner-container` of its
                # own, and it is the only one inside a parent that is not a
                # reply.
                and not contains_rect(top, candidate)
                if (reply := read_comment(tab_id, candidate["ref"], is_reply=True))
            ]
            replies, reply_total = nest_replies(rows)
            items.append(
                {
                    "index": len(items) + 1,
                    "author": parsed["author"],
                    "text": parsed["text"],
                    "pinned": parsed["pinned"],
                    "byAuthor": parsed["byAuthor"],
                    "replyCount": reply_total,
                    "replies": replies,
                }
            )

        if len(items) >= limit:
            break
        step = scroll_comments(tab_id, page, config)
        if step is None:
            warnings.append("找不到评论滚动锚点，评论可能未读全")
            break
        # A scroller that reports "not moved" has only reached the end of what is
        # currently rendered — it does not mean the thread is over. Xiaohongshu
        # appends the next batch a moment later and the scroller's own maximum
        # grows with it (measured on one note: max 3281 -> 5509 -> 6964 -> 7938
        # while `moved` was already False), so the end is declared by the thread
        # text going quiet, not by the scroll position.
        size = thread_size(tab_id, config)
        if size is not None and size > last_size:
            last_size, idle_rounds = size, 0
        else:
            idle_rounds += 1
        if idle_rounds >= max_idle_rounds:
            break

    if len(items) < limit and rounds >= scroll_config["max_steps"]:
        warnings.append(
            f"滚动达到 {scroll_config['max_steps']} 步上限，评论可能未读全"
        )
    return items, warnings


def collect_comments(
    tab_id: int, config: dict, limit: int, fallback: str | None = None
) -> tuple[dict, list[str]]:
    """Collect the comment section, and record how complete it is known to be.

    `declaredTotal` is the "共 N 条评论" counter the page prints. Two things are
    needed to compare against it honestly, and getting either wrong invents
    warnings for threads that were in fact read whole:

    * the counter includes replies, so the comparable number is every row read,
      not the top-level count;
    * a request cut short by `limit` is a truncation the caller asked for, not
      an incomplete read.

    `threadEnded` is the page's own end-of-thread marker, which settles the
    question outright where it appears.
    """
    activate_tab(tab_id)
    items, warnings = extract_comments(tab_id, config, limit)
    final = comment_snapshot(tab_id, config)
    container = find_first(final, config["detail"]["comments"])
    # The note says outright that it has no comments; nothing below is missing.
    no_comments = find_first(final, config["detail"]["comments_empty"]) is not None
    if not items and not no_comments:
        warnings.append("未读取到评论（可能未登录、评论区未加载或该笔记关闭了评论）")
    raw = extract_text(tab_id, container["ref"] if container else fallback, 100000)["text"]
    declared = COMMENT_TOTAL.search(raw)
    declared_total = int(declared.group(1)) if declared else None
    collected = len(items)
    # `replyCount` is each comment's whole subtree, so this counts every row at
    # every depth without walking the tree again.
    read_total = collected + sum(item["replyCount"] for item in items)
    thread_ended = COMMENT_THREAD_END in raw
    incomplete = (
        collected < limit
        and not no_comments
        and not thread_ended
        and (declared_total is None or read_total < declared_total)
    )
    if incomplete:
        warnings.append(
            f"评论只读到 {read_total} 条（页面声明 {declared_total} 条），"
            "可能是未登录或评论区未完全加载"
        )
    return {
        "declaredTotal": declared_total,
        "requested": limit,
        "collected": collected,
        "repliesCollected": read_total - collected,
        "threadEnded": thread_ended,
        "noComments": no_comments,
        "possiblyIncomplete": incomplete,
        "items": items,
    }, warnings


def comments_path(output_path: Path) -> Path:
    """Comments live beside the note, in their own file.

    Whether to read comments at all is a parameter, and a thread is often worth
    re-reading long after the media was downloaded and verified. Separating the
    two means a comment pass only ever rewrites comment data, so it cannot
    disturb — or risk re-downloading — the note itself, and comments can be
    appended for a note collected before comment support existed.
    """
    return output_path.parent / COMMENTS_FILENAME


def write_comments(
    output_path: Path,
    *,
    note_id_value: str,
    comments: dict,
    warnings: list[str],
) -> dict:
    """Write `comments.json`: the thread, and how much of it is known to be here.

    The comment container's own text is deliberately not stored — it duplicates
    every body at ten times the size, and the structured rows already carry
    them. What is stored is what an agent needs to judge the read.
    """
    payload = {
        "schemaVersion": 2,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "noteId": note_id_value,
        **comments,
        "warnings": warnings,
    }
    path = comments_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload
