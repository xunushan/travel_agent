#!/usr/bin/env python3
"""Search Xiaohongshu's result page and list the candidates it returns.

**Nothing here opens a note.** The search page is read once — navigate, wait for
the cards to render, press the filters, read the cards — and the result is a
list of notes with what a search page can honestly say about them: which note,
where its detail page is, what its title is, when it was published, and whether
this machine already has it. Everything that lives inside the note (its body,
author, tags, interaction counts, edit time, type) is not guessed at here; it is
read when the note is downloaded, and `download.py` reports it then.

The other half of this file drives the note page rather than the search page:
`open_result` brings one candidate on screen and verifies it landed, which is
what `download.py` calls before collecting. Navigation lives in one module so
there is one place that knows how a page is opened, closed and waited for.

Two facts shape the filter code, both measured 2026-10-07 and recorded in the
repo's `docs/xhs-筛选机制与实测.md`:

* **A filter is a press, never a URL parameter.** Navigating to a results URL
  carrying `sort=time_descending` left the page's own `searchContext` at
  `general`, and not one card moved.
* **A press is verified against the page's own `active` marker**, never by
  comparing the cards before and after: pressing 半年内 left the first six cards
  identical, because the newest results were already within half a year. A
  filter that silently failed would return an unfiltered page that looks exactly
  like a successful search.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import db
import runtime
from note import published_at
from runtime import (
    SEARCH_URL_TEMPLATE,
    chrome_agent,
    contains_rect,
    find_all,
    find_first,
    find_open_detail,
    load_locators,
    note_id,
    snapshot,
)

# A `;`-separated list, never a repeated flag, because a search keyword may
# contain a comma ("喀纳斯,禾木 怎么走") while no keyword contains a semicolon.
FILTER_SEPARATOR = ";"

# How many times one filter press is attempted before it counts as a failure.
# Measured 2026-10-07: the identical press applied cleanly on a settled page and
# did nothing on a page whose results were still streaming in — the re-render
# between reading an option's ref and clicking it moves the panel out from under
# the ref. Each attempt re-reads the panel, so the second one gets fresh refs.
FILTER_PRESS_ATTEMPTS = 3

# The results are rendered client-side and the measured delay is long: on one
# observation the page showed 0 `section.note-item` at 6s, still 0 at 14s, and 25
# of them later on. Reading too early reports "no results" for a search that
# worked.
DEFAULT_RENDER_TIMEOUT = 45.0
DEFAULT_POLL_INTERVAL = 1.5

# Overridable by callers/tests; read at call time so the module constant can change.
DETAIL_CLOSE_TIMEOUT = 6.0
NOTE_OPEN_TIMEOUT = 15.0

# The site's own boxes disagree with their contents by a pixel here and there;
# 2px is enough for the measured case and far less than the gap between any two
# groups, so it cannot merge two of them. See `runtime.contains_rect`.
RECT_SLACK = 2


def split_items(value: str | None) -> list[str]:
    """Split a `;`-separated CLI value into stripped, non-empty items."""
    if not value:
        return []
    return [item.strip() for item in value.split(FILTER_SEPARATOR) if item.strip()]


def parse_filter_specs(value: str | None) -> list[dict]:
    """Turn `--filters` into `[{dimension, option}]`, the dimension optional.

    One flag holding several `维度=选项` items, never a repeated flag: the agent
    composes a whole filter scheme at once, and a repeated flag makes "which ones
    did I actually pass" hard to see in a transcript.

    Only the FIRST `=` splits, so an option text containing one still works.
    """
    specs = []
    for item in split_items(value):
        dimension, separator, option = item.partition("=")
        if not separator:
            dimension, option = None, item
        else:
            dimension = dimension.strip() or None
        option = runtime.one_line(option)
        if not option:
            raise ValueError(f"--filters 的选项为空：{item!r}")
        specs.append({"dimension": dimension, "option": option})
    return specs


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
    return runtime.one_line((element or {}).get("text"))


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


# --- reading the page's own filter controls --------------------------------


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


# --- driving the search page -----------------------------------------------


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
            "——这个命令需要停在搜索结果页。"
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
    that did not take is retried before it is called a failure. A filter that
    silently did not apply would hand back a page of unrelated notes that looks
    exactly like a successful search, which is why the last attempt failing is
    an error rather than a warning.

    Retrying is safe because pressing an option that is already active is the
    same press — that is also what makes re-applying a whole scheme after a
    fresh navigation safe.
    """
    settle = config["scroll"]["search"].get("filter_settle", 3)
    applied = []
    for spec in specs:
        for _attempt in range(FILTER_PRESS_ATTEMPTS):
            dimension, text, ref, via = locate_option(tab_id, config, spec)
            chrome_agent("page", "click", "--tab-id", str(tab_id), "--ref", ref)
            time.sleep(settle)
            if (dimension, text) in active_options(results_snapshot(tab_id, config), config):
                break
        else:
            raise RuntimeError(
                f"点了「{dimension}={text}」{FILTER_PRESS_ATTEMPTS} 次，页面都没把它标成生效项"
                "——筛选没有施加成功。继续下去会拿到一批没有筛选过的结果，因此中止。"
                "用 discover.py --list-filters 看当前页面真实的选项与生效项。"
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


# --- reading the cards ------------------------------------------------------


def cards(page: dict, config: dict, limit: int) -> list[dict]:
    """The result cards on a rendered results page, in the order they appear.

    Two elements per card are needed and no more: the title link gives the
    title, and the cover link gives the href carrying the `xsec_token` that the
    detail page requires. Nothing here reads a card's author, time or like count
    — see the module docstring on why the card cannot be asked for those
    honestly.
    """
    rules = config["search"]
    titles = {
        element["href"]: (element.get("text") or "").strip()
        for element in find_all(page, rules["result_title_link"])
        if element.get("href") and (element.get("text") or "").strip()
    }
    found, seen = [], set()
    for element in find_all(page, rules["result_link"]):
        href = element["href"]
        if href in seen:
            continue
        seen.add(href)
        title = titles.get(href)
        if not title:
            enclosing = [card for card in find_all(page, rules["result_card"])
                         if contains_rect(card, element)]
            if enclosing:
                title = (enclosing[0].get("text") or "").strip() or None
        query = parse_qs(urlparse(href).query, keep_blank_values=True)
        found.append(
            {
                "rank": len(found) + 1,
                "ref": element["ref"],
                "title": title,
                "href": href,
                "noteId": note_id(href),
                "hasAccessContext": all(
                    query.get(key) for key in rules["result_link"].get("required_query_keys", [])
                ),
            }
        )
        if len(found) >= limit:
            break
    return found


# --- what is already on disk ------------------------------------------------


def library_entry(note_dir: Path | None) -> dict:
    """What this machine already knows about a note, read from its own files.

    A note that has been downloaded carries its title, times, counts and type in
    `note.json`, which is one small read and no page load. That is the whole
    reason an already-known note comes back from a search with more filled in
    than a new one: the information exists, offline, already.
    """
    if not note_dir:
        return {}
    try:
        note = json.loads((note_dir / runtime.NOTE_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        "title": note.get("title"),
        "publishedAt": note.get("publishedAt"),
        "updatedAt": note.get("updatedAt"),
        "type": note.get("type"),
        "stats": note.get("stats"),
    }


def candidate(item: dict, known: dict | None, notes_root: Path) -> dict:
    """One card as the caller gets it: what the page said, plus what disk knows."""
    note_dir = runtime.find_note_dir(
        notes_root, item["noteId"], (known or {}).get("note_dir")
    )
    entry = library_entry(note_dir)
    return {
        "rank": item["rank"],
        "noteId": item["noteId"],
        "url": item["href"],
        "title": entry.get("title") or item["title"],
        # The id carries the publication time, so this costs nothing and needs
        # no page. The note's own value wins when it has been downloaded.
        "publishedAt": entry.get("publishedAt") or published_at(item["noteId"]),
        # Known only for a note already on disk; a new one has to be opened.
        "updatedAt": entry.get("updatedAt"),
        "type": entry.get("type"),
        "stats": entry.get("stats"),
        "inLibrary": note_dir is not None,
        "noteDir": str(note_dir) if note_dir else None,
    }


# --- opening a note (used by download.py) -----------------------------------


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


def open_note(tab_id: int, url: str, want: str, config: dict,
              timeout: float | None = None) -> str:
    """Navigate to a note's detail URL and verify it landed there.

    The URL is replayed exactly as it was stored — its `xsec_token` is the access
    context, so it is never rebuilt from the id. A token that has expired lands
    on a 404, and that is reported rather than worked around: finding the note
    again means searching again, which is the caller's decision, not a silent
    fallback here.
    """
    activate_tab(tab_id)
    chrome_agent("tabs", "navigate", str(tab_id), url)
    if not wait_for_note(tab_id, want, config, timeout):
        raise RuntimeError(
            f"导航到 {url} 后没有落在目标笔记 {want}。"
            "落盘 URL 里的 xsec_token 会过期，过期后需要重新 discover 拿到新链接。"
        )
    return url


# --- the run ----------------------------------------------------------------


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
            "请看仓库 docs/xhs-筛选机制与实测.md 并更新 locators.yaml 的 search.filter_* 一段。"
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


def run_discover(args, config: dict) -> int:
    specs = parse_filter_specs(args.filters)
    root = runtime.data_root(args.data_root)
    notes_root = runtime.notes_dir(root)
    limit = max(1, args.limit)

    outcome = run_search(args.tab_id, config, args.keyword, specs)
    page = outcome["page"]
    found = cards(page, config, limit + 1)
    capped = len(found) > limit
    found = found[:limit]

    with db.open_db(runtime.db_path(root, args.db)) as conn:
        known = db.known_notes(conn, [item["noteId"] for item in found if item["noteId"]])
    candidates = [
        candidate(item, known.get(item["noteId"]), notes_root)
        for item in found
        if item["noteId"]
    ]
    in_library = sum(1 for item in candidates if item["inLibrary"])
    output = {
        "keyword": args.keyword,
        "source": {"tabId": args.tab_id, "url": page.get("url")},
        "applied": outcome["applied"],
        "limit": limit,
        "capped": capped,
        "truncatedSnapshot": bool(page.get("truncated")),
        "summary": {
            "candidates": len(candidates),
            "inLibrary": in_library,
            "new": len(candidates) - in_library,
        },
        "candidates": candidates,
    }
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    filters_text = "；".join(
        f"{spec['dimension']}={spec['option']}" if spec["dimension"] else spec["option"]
        for spec in specs
    )
    summary = output["summary"]
    print(
        f"候选 {summary['candidates']} 条 / 在库 {summary['inLibrary']} / "
        f"新发现 {summary['new']}"
        + (f"；已施加筛选：{filters_text}" if filters_text else "；未施加筛选")
        + ("；页面上还有更多" if capped else ""),
        file=sys.stderr,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="搜一个关键词，输出候选清单 JSON（不开笔记：笔记里的内容由 download 读）"
    )
    parser.add_argument("--tab-id", required=True, type=int)
    parser.add_argument("--keyword", help="搜索词，一次一个")
    parser.add_argument("--filters", help='分号分隔的筛选，如 "排序依据=最新;半年内"；维度可省，歧义报错')
    parser.add_argument("--limit", type=int, default=10, help="最多返回多少条候选（默认 10）")
    parser.add_argument(
        "--list-filters", action="store_true",
        help="诊断用：列出页面上真实存在的筛选选项与当前生效项，然后退出",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="--list-filters 的等待秒数")
    runtime.add_data_arguments(parser)
    args = parser.parse_args()
    config = load_locators()
    if args.list_filters:
        return list_filters(args.tab_id, config, args.timeout)
    if not args.keyword:
        parser.error("需要 --keyword（或用 --list-filters 只看筛选选项）")
    return run_discover(args, config)


if __name__ == "__main__":
    raise SystemExit(main())
