#!/usr/bin/env python3
"""Phase one: turn an intent into a shortlist of notes worth downloading.

The shape of this file follows the shape of the job. A person researching a trip
does not read twenty notes one at a time — they search, adjust the filters, and
then read the candidates *against each other* to pick the handful that decide
something. So this script does the mechanical half (navigate, wait for the
results to render, press the filters, walk each candidate, read its state, save
its body and its cover, record it) and prints one compact table. The agent reads
that table once and answers two questions per line — keep or drop — plus one
question about the whole thing: is this enough to decide, or should the search
terms change?

Two decisions are worth knowing before reading the code:

* **Filtering is by pressing the controls, never by URL.** Measured 2026-10-07:
  navigating to a results URL carrying `sort=time_descending&noteType=2` left the
  page's own `searchContext` at `general`/`0`, and not one card moved. The site
  keeps its filters in client-side state, so only a press can set them. See
  `references/filters.md`.

* **No filter vocabulary is hard-coded.** Which dimensions and options exist is
  read off the live page (`filters` subcommand), and `--filters` matches those by
  their visible text. A redesign therefore cannot leave a stale word list behind,
  and an option nobody anticipated is still selectable.

Every candidate is fetched before any screening judgement is made, because the
judgement is comparative. What gets fetched is deliberately only what screening
needs: the server-rendered state, the body, and one cover image. Not the gallery,
not the video, and above all not the comment thread — that costs about thirty
scroll rounds per note and answers no screening question at all.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import collect
import db
import discover
import runtime
from media import DOWNLOAD_STATE_COMPLETE
from runtime import (
    SEARCH_URL_TEMPLATE,
    chrome_agent,
    contains_rect,
    find_all,
    find_first,
    load_locators,
    note_id,
    snapshot,
)

FILTER_SEPARATOR = ";"

# The site's own boxes disagree with their contents by a pixel here and there;
# 2px is enough for the measured case and far less than the gap between any two
# groups, so it cannot merge two of them. See `runtime.contains_rect`.
RECT_SLACK = 2

# How many times one filter press is attempted before it counts as a failure.
# Measured 2026-10-07: the identical press applied cleanly on a settled page and
# did nothing on a page whose results were still streaming in — the re-render
# between reading an option's ref and clicking it moves the panel out from under
# the ref. Each attempt re-reads the panel, so the second one gets fresh refs.
FILTER_PRESS_ATTEMPTS = 3

# The results are rendered client-side and the measured delay is long: on one
# observation the page showed 0 `section.note-item` at 6s, still 0 at 14s, and 25
# of them later on. `discover.py` returns whatever the snapshot holds, so a caller
# that does not wait reports "no results" for a search that worked.
DEFAULT_RENDER_TIMEOUT = 45.0
DEFAULT_POLL_INTERVAL = 1.5

# "4天前", "09-27", "2025-11-03" — the three shapes a card's time slot prints.
CARD_TIME_RE = re.compile(r"\d+\s*(?:天|小时|分钟)前|\d{2}-\d{2}|\d{4}-\d{2}-\d{2}")


def split_items(value: str | None) -> list[str]:
    """Split a `;`-separated CLI value into stripped, non-empty items.

    `;` rather than `,` because a search keyword may contain a comma
    ("喀纳斯,禾木 怎么走") while no keyword contains a semicolon.
    """
    if not value:
        return []
    return [item.strip() for item in value.split(FILTER_SEPARATOR) if item.strip()]


def parse_filter_specs(value: str | None) -> list[dict]:
    """Turn `--filters` into `[{dimension, option}]`, the dimension optional.

    One flag holding several `维度=选项` items, never a repeated flag — the rule
    the keywords follow too, and for the same reason: the agent composes a whole
    filter scheme at once, and a repeated flag makes "which ones did I actually
    pass" hard to see in a transcript.

    Only the FIRST `=` splits, so an option text containing one still works.
    """
    specs = []
    for item in split_items(value):
        dimension, separator, option = item.partition("=")
        if not separator:
            dimension, option = None, item
        else:
            dimension = dimension.strip() or None
        option = one_line(option)
        if not option:
            raise ValueError(f"--filters 的选项为空：{item!r}")
        specs.append({"dimension": dimension, "option": option})
    return specs


def one_line(text: object, limit: int | None = None) -> str:
    """Collapse whitespace — cards and bodies carry newlines — and optionally cut.

    Cutting is always at the end and marked, because an excerpt that stops
    mid-sentence without saying so reads as a truncated note rather than a
    truncated view of one. `batch.py` needs the same excerpt for the index, so
    the function itself lives in `runtime.py`.
    """
    return runtime.one_line(text, limit)


# --- reading the page's own filter controls --------------------------------


def _rect_key(element: dict) -> tuple:
    rect = element.get("rect") or {}
    return (rect.get("x"), rect.get("y"), rect.get("width"), rect.get("height"))


def _dedupe_by_rect(elements: list[dict]) -> list[dict]:
    """Drop the site's duplicate rendering of each option.

    Every filter option appears TWICE in a snapshot — same text, same rectangle,
    different ref (measured: `综合` as both e667 and e669, both `tags active`).
    Without this the panel reads as having twice as many options as it has, and a
    bare option spec would look ambiguous against itself.
    """
    output, seen = [], set()
    for element in elements:
        key = _rect_key(element)
        if key in seen:
            continue
        seen.add(key)
        output.append(element)
    return output


def _text(element: dict | None) -> str:
    return one_line((element or {}).get("text"))


def _inside(page: dict, container: dict, rule: dict) -> list[dict]:
    """Elements matching `rule` that sit within `container`'s rectangle.

    The 2px of slack is for the site's own rounding: see `contains_rect`. In this
    panel the group box and its option row can disagree by a pixel, and a strict
    test loses the whole group rather than one element of it.
    """
    return [
        element
        for element in find_all(page, rule)
        if contains_rect(container, element, RECT_SLACK)
    ]


def panel_groups(page: dict, config: dict) -> list[dict]:
    """Read the open filter panel into `[{name, options:[{text,active,ref}]}]`.

    Group order is the snapshot's own, which is top-to-bottom on screen, so the
    printed list reads the way the panel looks.

    The group's name is its label span, and it is found by what it is NOT: a
    group also contains a span per option (the text inside each `div.tags`), and
    those are the only other spans in there. Taking "the first span in the group"
    would work on today's layout and break the moment an option moved above its
    label, so the exclusion is what the code does.
    """
    rules = config["search"]
    panel = find_first(page, rules["filter_panel"])
    if not panel:
        return []
    groups = []
    for group in _inside(page, panel, rules["filter_group"]):
        containers = _inside(page, group, rules["filter_tag_container"])
        option_spans = {
            element["ref"]
            for container in containers
            for element in _inside(page, container, rules["filter_group_label"])
        }
        labels = [
            element
            for element in _inside(page, group, rules["filter_group_label"])
            if element["ref"] not in option_spans
        ]
        options = [
            {"text": _text(option), "active": "active" in (option.get("className") or "").split(),
             "ref": option["ref"]}
            for container in containers
            for option in _dedupe_by_rect(_inside(page, container, rules["filter_option"]))
        ]
        name = _text(labels[0]) if labels else ""
        options = [option for option in options if option["text"]]
        if name and options:
            groups.append({"name": name, "options": options})
    return groups


def channel_options(page: dict, config: dict) -> list[dict]:
    """The always-visible 笔记类型 row: 全部 / 图文 / 视频 / 用户.

    It is not part of the panel, needs no click to reach, and carries one option
    the panel does not (`用户`). So it is tried first for a note-type filter and
    the panel is only opened when the channel row cannot answer.
    """
    rules = config["search"]
    if not find_first(page, rules["filter_channels"]):
        return []
    active = {element["ref"] for element in find_all(page, rules["filter_channel_active"])}
    return [
        {"text": _text(element), "active": element["ref"] in active, "ref": element["ref"]}
        for element in _dedupe_by_rect(find_all(page, rules["filter_channels"]))
        if _text(element)
    ]


def resolve_option(groups: list[dict], spec: dict) -> dict:
    """Find the option a spec names, or explain why it cannot be found.

    Both failure messages carry the real options, because the whole point of
    reading them off the page is that the caller did not know them. A bare option
    matching several groups is refused rather than guessed: silently picking one
    would apply a filter that was never asked for and report success.
    """
    if spec["dimension"]:
        named = [group for group in groups if group["name"] == spec["dimension"]]
        if not named:
            available = "、".join(group["name"] for group in groups)
            raise ValueError(f"页面上没有「{spec['dimension']}」这一组筛选；现有分组：{available}")
        searched = named
    else:
        searched = groups

    matches = [
        (group["name"], option)
        for group in searched
        for option in group["options"]
        if option["text"] == spec["option"]
    ]
    if not matches:
        available = "、".join(
            f"{group['name']}({'/'.join(o['text'] for o in group['options'])})"
            for group in searched
        )
        raise ValueError(f"没有选项「{spec['option']}」；可选：{available}")
    if len(matches) > 1:
        where = "、".join(sorted({group for group, _ in matches}))
        raise ValueError(f"「{spec['option']}」在 {where} 里都有，必须写成「维度=选项」")
    group_name, option = matches[0]
    return {"dimension": group_name, **option}


# --- driving the page ------------------------------------------------------


def results_snapshot(tab_id: int, config: dict) -> dict:
    """A snapshot of a results page, wide enough to hold its own filter panel.

    The default 500-element ceiling is NOT enough here. Measured 2026-10-07 on
    `甘南环线`: `matched=711, truncated=true`, and the cut fell inside the
    发布时间 group — the panel appeared to offer only 不限/一天内, and
    `--filters 半年内` failed with "没有选项「半年内」" while that option was on
    screen. The more cards the page has loaded, the likelier the panel is the
    part that gets dropped, so every read of a results page goes through here.
    See `references/pitfalls.md` §6 on what `--limit` does and does not cut.
    """
    return snapshot(tab_id, int(config["scroll"]["search"].get("snapshot_limit", 2000)))


def wait_for(tab_id: int, config: dict, rule: dict, timeout: float, what: str) -> dict:
    """Poll until `rule` matches something, then return that snapshot."""
    deadline = time.time() + timeout
    page = results_snapshot(tab_id, config)
    while True:
        if find_first(page, rule):
            return page
        if time.time() >= deadline:
            raise RuntimeError(
                f"等了 {timeout:.0f}s 仍没有{what}（当前 {page.get('url')}）。"
                "搜索结果页是客户端渲染的：确认这个标签页已登录、停在搜索结果页，"
                "并且没有被广告屏蔽插件拦掉。"
            )
        time.sleep(DEFAULT_POLL_INTERVAL)
        page = results_snapshot(tab_id, config)


def open_panel(tab_id: int, config: dict, timeout: float = 20.0) -> dict:
    """Open the filter panel if it is not already open, and return the snapshot.

    Checking first is not an optimisation: pressing the control again would close
    a panel that was already open.
    """
    page = results_snapshot(tab_id, config)
    if find_first(page, config["search"]["filter_panel"]):
        return page
    control = find_first(page, config["search"]["filter_control"])
    if not control:
        raise RuntimeError(
            f"找不到「筛选」入口，无法施加筛选。当前页面：{page.get('url')}"
            "——这个子命令需要停在搜索结果页。"
        )
    chrome_agent("page", "click", "--tab-id", str(tab_id), "--ref", control["ref"])
    return wait_for(tab_id, config, config["search"]["filter_panel"], timeout, "筛选弹层")


def channel_row_match(page: dict, config: dict, option_text: str) -> dict | None:
    """The channel button whose text is `option_text`, if the row has one.

    `不限` is the panel's spelling of "do not filter on this" while the channel
    row says `全部`; the two are accepted for each other here. Nothing else is
    aliased — an alias table would be the vocabulary this design avoids.
    """
    wanted = {"不限": "全部"}.get(option_text, option_text)
    return next(
        (option for option in channel_options(page, config) if option["text"] == wanted), None
    )


def active_options(page: dict, config: dict) -> set[tuple[str, str]]:
    """`{(dimension, option)}` for everything the page currently shows as on."""
    active = {("笔记类型", option["text"]) for option in channel_options(page, config)
              if option["active"]}
    for group in panel_groups(page, config):
        active |= {(group["name"], option["text"]) for option in group["options"] if option["active"]}
    return active


def locate_option(tab_id: int, config: dict, spec: dict) -> tuple[str, str, str, str]:
    """Find the control `spec` names: `(dimension, text, ref, via)`.

    A spec the channel row can satisfy is answered there — no panel, one press.
    Everything else opens the panel. Called again for every press attempt,
    because a `ref` belongs to the snapshot it was read from.
    """
    page = results_snapshot(tab_id, config)
    option = channel_row_match(page, config, spec["option"])
    if option and spec["dimension"] in (None, "笔记类型"):
        return "笔记类型", option["text"], option["ref"], "channel"
    resolved = resolve_option(panel_groups(open_panel(tab_id, config), config), spec)
    return resolved["dimension"], resolved["text"], resolved["ref"], "panel"


def apply_filters(tab_id: int, config: dict, specs: list[dict]) -> list[dict]:
    """Press the controls `specs` name, verify each took, and report them.

    Each press is verified against the panel's own `active` marker, and a press
    that did not take is retried before it is called a failure. The obvious
    alternative — compare the result cards before and after — does not work:
    clicking 半年内 on 2026-10-07 left the first six cards identical, because the
    newest results were already within half a year, so an unchanged card set
    means "already satisfied" at least as often as it means "the press missed".
    A filter that silently did not apply would hand back a page of unrelated
    notes that looks exactly like a successful search, which is why the last
    attempt failing is an error rather than a warning.

    Retrying is safe because pressing an option that is already active is the
    same press — that is also what makes re-applying a whole scheme after a fresh
    navigation safe.
    """
    settle = config["scroll"]["search"].get("filter_settle", 3)
    applied = []
    for spec in specs:
        for attempt in range(FILTER_PRESS_ATTEMPTS):
            dimension, text, ref, via = locate_option(tab_id, config, spec)
            chrome_agent("page", "click", "--tab-id", str(tab_id), "--ref", ref)
            time.sleep(settle)
            if (dimension, text) in active_options(results_snapshot(tab_id, config), config):
                break
        else:
            raise RuntimeError(
                f"点了「{dimension}={text}」{FILTER_PRESS_ATTEMPTS} 次，页面都没把它标成生效项"
                "——筛选没有施加成功。继续下去会拿到一批没有筛选过的结果，因此中止。"
                "用 search.py filters 看当前页面真实的选项与生效项。"
            )
        applied.append({"dimension": dimension, "option": text, "via": via})
    return applied


def search_page_url(keyword: str) -> str:
    return SEARCH_URL_TEMPLATE.format(keyword=quote(keyword))


def run_search(tab_id: int, config: dict, keyword: str, specs: list[dict]) -> dict:
    """One search: navigate, wait for results, press the filters, wait again.

    Filters are applied AFTER the results are on screen, because the panel can
    only be opened on a page that has one, and they are applied per keyword
    because nothing observed guarantees the site carries the choice across a
    navigation.
    """
    chrome_agent("tabs", "navigate", str(tab_id), search_page_url(keyword))
    page = wait_for(
        tab_id,
        config,
        config["search"]["result_card"],
        config["scroll"]["search"].get("render_timeout", DEFAULT_RENDER_TIMEOUT),
        "搜索结果卡片",
    )
    applied = apply_filters(tab_id, config, specs) if specs else []
    if applied:
        page = results_snapshot(tab_id, config)
    return {"keyword": keyword, "page": page, "applied": applied}


def card_time(page: dict, config: dict, candidate: dict) -> str | None:
    """The fuzzy publish time the card prints ("4天前", "09-27").

    Worth showing beside the note page's exact date, because the two disagree in
    a useful way: a card reading "4天前" next to a `publishedAt` from last year
    means the note was edited recently, which is what a reader screening for
    currency wants to see.

    Returns None when the page's own rectangles cannot tell the cards apart, and
    a None here prints as "?" rather than as a time. On the 最新 sort they cannot:
    measured 2026-10-07, all thirty cards reported the SAME origin (x=267 y=217)
    while their heights differed, so every card's rectangle contained every
    other card's time slot — the whole table printed one card's time for all
    twelve rows, and the value it printed advanced from 42分钟前 to 44分钟前
    between runs, i.e. it was reading one specific card perfectly and calling it
    every row. Nothing recoverable is left on a page in that state: the card that
    "contains" this candidate's link is equally arbitrary, so the card's own text
    is no better an answer than the slot. The column goes blank; a wrong date read
    as a right one is worse than a missing one.

    Where the rects do separate the cards (measured on the 最多收藏 sort, which
    printed 08-20 / 2025-09-26 / 06-25 / 07-25 / 09-14 for its own rows), the
    slot is used, and the card's own text — whose LAST time-shaped token is the
    time (title, author, time, likes) — covers a slot that two cards share.
    """
    rules = config["search"]
    element = next(
        (item for item in page.get("elements", []) if item.get("ref") == candidate.get("ref")),
        None,
    )
    if not element:
        return None
    cards = find_all(page, rules["result_card"])
    card = next((item for item in cards if contains_rect(item, element)), None)
    if not card:
        return None
    if len({_rect_key(item)[:2] for item in cards}) < len(cards):
        return None
    for slot in _inside(page, card, rules["card_time"]):
        if sum(1 for item in cards if contains_rect(item, slot)) > 1:
            break
        match = CARD_TIME_RE.search(_text(slot))
        if match:
            return match.group(0)
    matches = CARD_TIME_RE.findall(_text(card))
    return matches[-1] if matches else None


# --- screening one candidate ------------------------------------------------


def screen_note(
    tab_id: int,
    config: dict,
    candidate: dict,
    *,
    notes_root: Path,
    prefix: str,
    timeout: float | None = None,
) -> dict:
    """Open one candidate and save exactly what screening needs.

    Direct navigation rather than a press: the href carries the `xsec_token` the
    site requires, a card's ref belongs to a snapshot that pressing a filter has
    already invalidated, and `wait_for_note` verifies the landing either way.

    One candidate failing is reported, not raised: a page that will not load is
    not a reason to discard the other nineteen.
    """
    try:
        want = candidate.get("noteId") or note_id(candidate.get("href"))
        if not want:
            raise RuntimeError(f"无法从 href 解析笔记 ID: {candidate.get('href')}")
        chrome_agent("tabs", "navigate", str(tab_id), candidate["href"])
        if not discover.wait_for_note(tab_id, want, config, timeout):
            raise RuntimeError(f"导航后没有落在目标笔记 {want}")

        note_dir = notes_root / want
        facts: dict = {}
        note = collect.collect_note(
            tab_id,
            note_dir=note_dir,
            output_path=note_dir / collect.NOTE_FILENAME,
            parts=("body", "cover"),
            prefix=prefix,
            facts=facts,
        )
        return {"candidate": candidate, "note": note, "noteDir": note_dir, "facts": facts,
                "error": None}
    except Exception as error:  # one bad candidate must not sink the run
        text = str(error).strip()
        message = text.splitlines()[-1][:300] if text else repr(error)
        return {"candidate": candidate, "note": {}, "facts": {}, "error": message}


# --- the digest table ------------------------------------------------------


def cover_path_for(note_dir: Path | str | None) -> Path | None:
    """The cover file for a note directory, whatever extension it landed with."""
    if not note_dir:
        return None
    images = Path(note_dir) / "images"
    if not images.is_dir():
        return None
    found = sorted(images.glob("cover.*"))
    return found[0] if found else None


def digest_row(index: int, row: dict, *, excerpt: int) -> list[str]:
    """One candidate as up to four lines: identity, content, cover, warnings.

    The note id is printed in full rather than shortened. It is the argument the
    next command takes, and a truncated id costs the reader a round trip to
    recover — exactly the cost this table exists to avoid.
    """
    stats = row.get("stats") or {}
    counts = "/".join(
        "-" if stats.get(key) is None else str(stats[key])
        for key in ("likes", "collects", "comments", "shares")
    )
    marker = "*" if row.get("decision") == db.DECISION_KEEP else " "
    known = {db.STATUS_APPROVED: "已筛过:keep", db.STATUS_REJECTED: "已筛过:drop",
             db.STATUS_COLLECTED: "已下载"}.get(row.get("status"))
    if row.get("decision") == db.DECISION_KEEP:
        known = "本轮已选"

    head = (
        f"{index:>2} {marker} {row['noteId']}"
        f"  {one_line(row.get('title'), 34) or '(无标题)'}"
        f"  [{one_line(row.get('author'), 10) or '?'}]"
        f" {row.get('cardTime') or '?'} {row.get('publishedAt') or '?'}"
        f" {counts} {row.get('type') or '?'}"
    )
    if row.get("updatedAt") and row.get("updatedAt") != row.get("publishedAt"):
        head += f" 编辑于{str(row['updatedAt'])[:10]}"
    if known:
        head += f"  [{known}]"

    labels = "、".join(one_line(tag, 12) for tag in (row.get("tags") or [])[:6])
    body = one_line(row.get("content"), excerpt)
    lines = [head, f"     {labels}{' | ' if labels and body else ''}{body}"]
    if row.get("coverPath"):
        lines.append(f"     cover: {row['coverPath']}")
    for warning in row.get("newWarnings") or []:
        lines.append(f"     ! {one_line(warning, 160)}")
    return lines


def row_from_index(row: dict, candidate: dict | None = None) -> dict:
    """A digest row built from the index alone, for a note not opened again.

    This is the cheap path and the reason a second search costs almost nothing:
    everything the table shows was stored when the note was first screened.
    """
    return {
        "noteId": row["note_id"],
        "title": row["title"],
        "author": row["author"],
        "type": row["type"],
        "publishedAt": row["published_at"],
        "updatedAt": row["updated_at"],
        "stats": {"likes": row["likes"], "collects": row["collects"],
                  "comments": row["comment_count"], "shares": row["shares"]},
        "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
        "content": row["excerpt"] or "",
        "status": row["status"],
        "cardTime": (candidate or {}).get("cardTime"),
        "coverPath": cover_path_for(row["note_dir"]),
    }


def row_from_screening(result: dict) -> dict:
    note, candidate, facts = result["note"], result["candidate"], result["facts"]
    return {
        "noteId": note.get("noteId") or candidate.get("noteId"),
        "title": note.get("title"),
        "author": note.get("author"),
        "type": note.get("type"),
        "publishedAt": note.get("publishedAt"),
        "updatedAt": note.get("updatedAt"),
        "stats": note.get("stats") or {},
        "tags": note.get("tags") or [],
        "content": note.get("content") or "",
        "status": db.STATUS_SCREENED,
        "cardTime": candidate.get("cardTime"),
        "coverPath": cover_path_for(result["noteDir"]),
        "newWarnings": note.get("warnings") or [],
        "facts": facts,
    }


def screening_media(row: dict) -> list[dict]:
    """The media rows screening records: the whole carousel, plus the cover.

    Every discovered URL is recorded, not only the one that was fetched. Phase
    two compares the picture set it finds against what the index knows, and an
    index holding nothing but the cover would report every other picture as newly
    added on the first media run.
    """
    facts = row.get("facts") or {}
    urls = list(facts.get("imageUrls") or [])
    cover_src, cover_file = facts.get("coverSrc"), facts.get("coverFilename")
    if cover_src and cover_src not in urls:
        urls.insert(0, cover_src)
    downloaded = cover_src if cover_file else None
    return [
        {
            "url": url,
            "kind": "cover" if url == cover_src else "image",
            "state": DOWNLOAD_STATE_COMPLETE if url == downloaded else db.MEDIA_DISCOVERED,
            "filename": cover_file if url == downloaded else None,
        }
        for url in urls
    ]


# --- the run ---------------------------------------------------------------


def plan_row(item: dict, stored: dict | None, decision: str | None) -> dict:
    """The digest row for a candidate that was not opened in this run."""
    if stored:
        row = row_from_index(stored, item)
    else:
        row = {
            "noteId": item["noteId"],
            "title": item.get("title"),
            "type": None,
            "stats": {},
            "tags": [],
            "content": "",
            "status": db.STATUS_SEEN,
            "cardTime": item.get("cardTime"),
            "coverPath": None,
        }
    row["decision"] = decision or db.DECISION_PENDING
    return row


def group_header(group: dict, *, limit: int) -> str:
    """One keyword's section header, carrying what that keyword lost.

    The counts are the keyword's own, never the run's: a round covering two
    keywords used to print one line for both, so a keyword whose pool had been
    cut looked exactly like a keyword that had been searched thoroughly.
    """
    head = f"--- 「{group['keyword']}」{group['candidates']} 候选"
    if group["capped"]:
        head += f"（页面上还有更多，已取到 --limit {limit}）"
    head += f" · 打开 {group['fresh']}"
    if group["cached"]:
        head += f" · 库里已有 {group['cached']}"
    if group["deferred"]:
        head += f" · 未展开 {group['deferred']}"
    if group["duplicates"]:
        head += f" · 与前面的关键词重复 {group['duplicates']}"
    return head


def run_phase_one(args, config: dict) -> int:
    keywords = split_items(args.keywords)
    if not keywords:
        raise SystemExit('需要 --keywords，例如 --keywords "川西秋色;稻城亚丁 秋"')
    specs = parse_filter_specs(args.filters)

    root = runtime.data_root(args.data_root)
    notes_root = runtime.notes_dir(root)

    with db.open_db(runtime.db_path(root, args.db)) as conn:
        if args.run_id:
            if not db.run_row(conn, args.run_id):
                raise SystemExit(f"run {args.run_id} 不存在；新建请去掉 --run-id")
            run_id = args.run_id
        else:
            run_id = db.start_run(
                conn,
                kind="screen",
                data_root=root,
                keywords=keywords,
                filters={spec["dimension"] or "": spec["option"] for spec in specs},
                args={"limit": args.limit, "screenLimit": args.screen_limit,
                      "excerpt": args.excerpt, "noScreen": args.no_screen},
            )

        # One keyword is one round: its own cards, its own opening budget, its own
        # section. A budget shared across keywords is what let one keyword's pool
        # be cut off entirely by another's — measured in run 2 of 2026-10-07: 26
        # cards under two keywords, and `--screen-limit 15` spent on the first
        # keyword left the second keyword's 11 candidates untouched, reported as
        # one anonymous "未展开 11 条" that named neither the keyword nor the loss.
        groups: list[dict] = []
        claimed: dict[str, str] = {}
        applied: list[dict] = []
        for keyword in keywords:
            outcome = run_search(args.tab_id, config, keyword, specs)
            page = outcome["page"]
            ceiling = max(1, args.limit)
            found = discover.discover(page, config, ceiling + 1)
            # Asking for one card past the ceiling is how the run learns the page
            # had more to give. The total is not readable anywhere, so the extra
            # card is the evidence, and the report says "还有更多" instead of a
            # number it would have to invent.
            capped = len(found) > ceiling
            items, duplicates = [], 0
            for item in found[:ceiling]:
                item["keyword"] = keyword
                item["cardTime"] = card_time(page, config, item)
                if not item["noteId"]:
                    continue
                if item["noteId"] in claimed:
                    # The same note can rank under two keywords. It stays with
                    # the keyword that found it first; the later one counts it
                    # rather than opening a second copy.
                    duplicates += 1
                    continue
                claimed[item["noteId"]] = keyword
                items.append(item)
            groups.append({"keyword": keyword, "items": items, "candidates": len(items),
                           "capped": capped, "duplicates": duplicates,
                           "applied": outcome["applied"]})
            applied = outcome["applied"] or applied

        candidates = [item for group in groups for item in group["items"]]
        decisions = {item["note_id"]: item["decision"] for item in db.run_items(conn, run_id)}
        db.add_run_items(
            conn,
            run_id,
            [{"noteId": item["noteId"], "rank": item["rank"], "href": item["href"],
              "keyword": item["keyword"]} for item in candidates],
        )
        known = db.known_notes(conn, [item["noteId"] for item in candidates])
        db.mark_seen(conn, list(known))

        for group in groups:
            # The budget is per keyword, so each keyword gets the reader's full
            # attention however many keywords the round holds.
            budget = args.screen_limit if not args.no_screen and args.screen_limit > 0 else None
            rows, failures, fresh, deferred, cached = [], [], 0, 0, 0
            for item in group["items"]:
                stored = known.get(item["noteId"])
                if args.no_screen or stored or (budget is not None and fresh >= budget):
                    # Listed but not opened. Counted apart from the ones the index
                    # already covers, because the two call for opposite reactions:
                    # this one is "raise --screen-limit", that one is "nothing to do".
                    if not args.no_screen and not stored:
                        deferred += 1
                    else:
                        cached += 1
                    rows.append(plan_row(item, stored, decisions.get(item["noteId"])))
                    continue
                result = screen_note(
                    args.tab_id, config, item, notes_root=notes_root, prefix=args.prefix,
                    timeout=args.open_timeout,
                )
                fresh += 1
                if result["error"]:
                    failures.append(f"{item['noteId']}: {result['error']}")
                    row = plan_row(item, None, None)
                    row["status"] = db.STATUS_FAILED
                    row["newWarnings"] = [result["error"]]
                    rows.append(row)
                    continue
                row = row_from_screening(result)
                row["decision"] = db.DECISION_PENDING
                db.store_note(
                    conn,
                    result["note"],
                    note_dir=result["noteDir"],
                    excerpt=one_line(result["note"].get("content"), 600) or None,
                    status=db.STATUS_SCREENED,
                    media=screening_media(row),
                )
                rows.append(row)
                if args.interval:
                    time.sleep(args.interval)
            group.update(rows=rows, failures=failures, fresh=fresh, deferred=deferred,
                         cached=cached)
        conn.commit()

        rows = [row for group in groups for row in group["rows"]]
        failures = [failure for group in groups for failure in group["failures"]]
        fresh = sum(group["fresh"] for group in groups)
        kept = sum(1 for row in rows if row.get("decision") == db.DECISION_KEEP)
        dropped = sum(1 for row in rows if row.get("decision") == db.DECISION_DROP)
        filters_text = "；".join(
            f"{spec['dimension']}={spec['option']}" if spec["dimension"] else spec["option"]
            for spec in specs
        )
        print(f"=== run {run_id} · {';'.join(keywords)}"
              f"{' · ' + filters_text if filters_text else ''}"
              f" · {len(keywords)} 个关键词 · {len(rows)} 候选 ===")
        index = 0
        for group in groups:
            print(group_header(group, limit=args.limit))
            for row in group["rows"]:
                index += 1
                for line in digest_row(index, row, excerpt=args.excerpt):
                    print(line)
        summary = (
            f"{sum(group['candidates'] for group in groups)} 张卡片 → 打开 {fresh}"
            f" · 库里已有 {len(known)}"
            f"(keep {kept}/drop {dropped}/待定 {len(rows) - kept - dropped})"
            f" · 正文与封面在 {notes_root}"
        )
        print(f"=== {summary} ===")
        cut = [group for group in groups if group["deferred"] or group["duplicates"]]
        if cut:
            # Which keyword lost what, in the open. "未展开 11 条" without a name
            # reads as a rounding detail; it was an entire keyword's pool.
            print("--- 没看全的部分 ---")
            for group in cut:
                parts = []
                if group["deferred"]:
                    parts.append(f"到上限没打开 {group['deferred']} 条")
                if group["duplicates"]:
                    parts.append(f"与前面的关键词重复 {group['duplicates']} 条")
                print(f"    「{group['keyword']}」{'、'.join(parts)}")
        if applied:
            print("已施加筛选：" + "、".join(
                f"{item['dimension']}={item['option']}({item['via']})" for item in applied
            ))
        if failures:
            print(f"--- {len(failures)} 条抓取失败（已计入候选，未入库）---")
            for failure in failures:
                print(f"    ! {failure}")
        print(f"下一步：decide.py keep|drop --run-id {run_id} --note-id <id> --reason \"...\"")
        db.finish_run(conn, run_id, totals={
            "candidates": len(rows), "opened": fresh, "failed": len(failures),
            "keywords": [
                {"keyword": group["keyword"], "candidates": group["candidates"],
                 "opened": group["fresh"], "cached": group["cached"],
                 "deferred": group["deferred"], "duplicates": group["duplicates"],
                 "capped": group["capped"]}
                for group in groups
            ],
        })
    return 0


def list_filters(tab_id: int, config: dict, timeout: float) -> int:
    """Print what the page actually offers, and fail loudly if it cannot be read.

    Failing loudly matters more here than anywhere else in the skill: a silent
    fallback to "no filters" would return a page of irrelevant notes that looks
    exactly like a successful search, and nothing downstream could tell.
    """
    channels = channel_options(results_snapshot(tab_id, config), config)
    groups = panel_groups(open_panel(tab_id, config, timeout), config)
    if not groups:
        raise RuntimeError(
            "筛选弹层打开了，但里面一个选项也没读到。页面结构可能变了："
            "请看 references/filters.md 并更新 locators.yaml 的 search.filters.* 一段。"
        )
    print("搜索页筛选选项（读自当前页面，不是固定词表）")
    if channels:
        print("  笔记类型(常驻行): " + " | ".join(
            f"{o['text']}{'*' if o['active'] else ''}" for o in channels
        ))
    for group in groups:
        print(f"  {group['name']}: " + " | ".join(
            f"{o['text']}{'*' if o['active'] else ''}" for o in group["options"]
        ))
    print('  （`*` 是当前生效项）用法：--filters "排序依据=最新;半年内"')
    print("  筛选是前端状态：拼在 URL 上的 sort=/noteType= 一律无效，只能点控件。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段一：按意图搜索并筛出值得下载的笔记")
    sub = parser.add_subparsers(dest="command", required=True)

    filters_parser = sub.add_parser("filters", help="列出页面上真实存在的筛选选项")
    filters_parser.add_argument("--tab-id", required=True, type=int)
    filters_parser.add_argument("--timeout", type=float, default=20.0)

    run_parser = sub.add_parser("run", help="一轮搜索：施加筛选、抓候选、打摘要表")
    run_parser.add_argument("--tab-id", required=True, type=int)
    run_parser.add_argument("--keywords", help='分号分隔的搜索词，如 "川西秋色;稻城亚丁 秋"（不重复传）')
    run_parser.add_argument("--filters", help='分号分隔的筛选，如 "排序依据=最新;半年内"；维度可省，歧义报错')
    run_parser.add_argument("--limit", type=int, default=10,
                            help="每个搜索词最多取多少张卡片（默认 10：一段搜索词给出十来条就够判断该不该换词）")
    run_parser.add_argument("--screen-limit", type=int, default=10,
                            help="每个搜索词最多打开多少篇做初筛（默认 10；0 = 不限）")
    run_parser.add_argument("--excerpt", type=int, default=150, help="摘要里正文截断字数")
    run_parser.add_argument("--no-screen", action="store_true", help="只出卡片表，不逐篇抓取")
    run_parser.add_argument("--run-id", type=int, help="并入已有的 run（默认新建）")
    run_parser.add_argument("--prefix", default="xiaohongshu-note")
    run_parser.add_argument("--interval", type=float, default=0.0, help="逐篇之间的间隔秒数")
    run_parser.add_argument("--open-timeout", type=float, default=None,
                            help="等待单篇笔记打开的秒数")
    runtime.add_data_arguments(run_parser)

    args = parser.parse_args()
    config = load_locators()
    if args.command == "filters":
        return list_filters(args.tab_id, config, args.timeout)
    return run_phase_one(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
