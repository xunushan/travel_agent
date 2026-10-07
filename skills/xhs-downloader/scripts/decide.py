#!/usr/bin/env python3
"""Record the agent's keep/drop judgement about one run's candidates.

This is the cheap step and must stay cheap: it opens no page, touches no browser
and reads no note, it only writes what has already been decided. Opening a note
costs a navigation plus a body read, so a screening pass can look at twenty
candidates and record a verdict about each without a second lookup — which is
why the CLI is a thin layer over `db.decide` rather than a collector.

`show` reads the ledger back for the next step: its `--json` array is what
`batch.py --plan` consumes, and the text form is what an agent reads when it
wants to see what is still undecided without opening the database by hand.

The file is `decide` rather than `select` because `select` shadows a stdlib
module: every script here reaches `subprocess` through `runtime`, and
`subprocess` imports `selectors`, which needs the real `select.select` — a file
of that name on `sys.path` breaks the import before any code here runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

import db
import runtime

# `db` owns the decision vocabulary; only PENDING gets a different label here,
# because the CLI never writes it and the summary reads "待定" better than the
# raw column value. Order is the order the summary prints in.
DECISION_LABELS = {
    db.DECISION_KEEP: "keep",
    db.DECISION_DROP: "drop",
    db.DECISION_PENDING: "待定",
}
SUMMARY_ORDER = (db.DECISION_KEEP, db.DECISION_DROP, db.DECISION_PENDING)


def parse_ids(value: str) -> list[str]:
    """Split the single `--note-id` argument into ids.

    One argument rather than a repeatable flag: the caller already holds a list,
    and `--note-id a --note-id b` would make it rebuild that list in its own
    words. Spaces around the commas are tolerated because the ids come out of a
    rendered page or an agent's own text, and duplicates are dropped so a
    repeated id cannot inflate the count `db.decide` reports.
    """
    ids: list[str] = []
    for item in value.split(","):
        note_id = item.strip()
        if note_id and note_id not in ids:
            ids.append(note_id)
    return ids


def summary_line(items: list[dict]) -> str:
    """The one-line tally, always over the whole run rather than the printed slice."""
    counts = Counter(item["decision"] for item in items)
    parts = [f"{DECISION_LABELS[name]} {counts.get(name, 0)}" for name in SUMMARY_ORDER]
    return f"合计 {len(items)} 候选：" + " / ".join(parts)


def json_item(item: dict, note: dict | None) -> dict:
    """One candidate as `batch.py --plan` reads it, plus what `show` renders.

    `noteId`, `href` and `decision` are the contract. `url` repeats `href`
    because that search-result URL is the only one still carrying its
    `xsec_token` and therefore the one phase two can navigate to; `keywords`
    is what `batch.py` falls back to when even that has expired.
    """
    return {
        "noteId": item["note_id"],
        "href": item["href"],
        "url": item["href"],
        "decision": item["decision"],
        "rank": item["rank"],
        "reason": item["reason"],
        "title": (note or {}).get("title"),
        "keywords": [item["keyword"]] if item.get("keyword") else [],
    }


def text_line(item: dict, note: dict | None) -> str:
    """One candidate, compact enough to read twenty of at once."""
    rank = item.get("rank")
    rank_text = str(rank) if rank is not None else "-"
    label = DECISION_LABELS.get(item["decision"], item["decision"])
    # A note the index has never seen still has to say something; its own id is
    # the only label available, and hiding the row would lose a real candidate.
    title = (note or {}).get("title") or item["note_id"]
    reason = item.get("reason") or ""
    return f"{rank_text:>3}  {item['note_id']}  {label}  {title}  {reason}".rstrip()


def judge(conn, run_id: int, args, decision: str) -> int:
    ids = parse_ids(args.note_id)
    if not ids:
        print("--note-id 为空：请给出至少一个逗号分隔的笔记 id")
        return 1
    if db.run_row(conn, run_id) is None:
        print(f"找不到 run {run_id}：请确认 id 来自阶段一的搜索，而不是新建一个 run")
        return 1
    # The candidates are the run's own rows, so the ids that are not among them
    # can be named without `db.decide`, which only reports how many it matched.
    candidates = {item["note_id"] for item in db.run_items(conn, run_id)}
    missing = [note_id for note_id in ids if note_id not in candidates]
    recorded = db.decide(conn, run_id, ids, decision, reason=args.reason)
    print(f"run {run_id}: 已记录 {recorded} 条 {decision}")
    if missing:
        print(f"未记录（不属于本次 run 的候选）：{' '.join(missing)}")
    # Zero recorded means every id named was wrong, which is a bad run id far
    # more often than an empty run — the caller has to see it as a failure.
    return 0 if recorded else 1


def show(conn, run_id: int, args) -> int:
    if db.run_row(conn, run_id) is None:
        print(f"找不到 run {run_id}：请确认 id 来自阶段一的搜索")
        return 1
    everything = db.run_items(conn, run_id)
    if not everything:
        print(f"run {run_id} 还没有任何候选：先跑阶段一的搜索")
        return 0
    items = db.run_items(conn, run_id, decision=args.decision)
    if args.limit is not None:
        items = items[: args.limit]
    # One lookup for every displayed candidate, not one query per row: the ids
    # on a result page arrive together and are all missing or all present.
    notes = db.known_notes(conn, [item["note_id"] for item in items])
    summary = summary_line(everything)
    if args.json:
        payload = [json_item(item, notes.get(item["note_id"])) for item in items]
        print(json.dumps(payload, ensure_ascii=False))
        # stdout has to stay a bare JSON array for `batch.py --plan`, so the
        # human summary goes to stderr rather than corrupting the pipe.
        print(summary, file=sys.stderr)
    else:
        for item in items:
            print(text_line(item, notes.get(item["note_id"])))
        print(summary)
    return 0


def add_judgement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True, type=int, help="阶段一搜索建立的 run id")
    parser.add_argument(
        "--note-id",
        required=True,
        help="一个参数，逗号分隔多个笔记 id（逗号周围可以有空格）",
    )
    parser.add_argument("--reason", help="判断理由，原样写进台账，便于以后回看")
    runtime.add_data_arguments(parser)


def add_show_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-id", required=True, type=int, help="阶段一搜索建立的 run id")
    parser.add_argument(
        "--decision",
        choices=db.DECISIONS,
        help="只看某一类判断；默认全部",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="最多打印多少条候选；默认全部（输出给 agent 读时用来保持简短）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出 batch.py --plan 可直接读取的 JSON 数组",
    )
    runtime.add_data_arguments(parser)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    keep = subparsers.add_parser("keep", help="把候选记为 keep（有用）")
    add_judgement_arguments(keep)
    drop = subparsers.add_parser("drop", help="把候选记为 drop（无用）")
    add_judgement_arguments(drop)
    viewer = subparsers.add_parser("show", help="列出某个 run 的候选与判断")
    add_show_arguments(viewer)

    args = parser.parse_args()
    root = runtime.data_root(args.data_root)
    with db.open_db(runtime.db_path(root, args.db)) as conn:
        if args.command == "keep":
            return judge(conn, args.run_id, args, db.DECISION_KEEP)
        if args.command == "drop":
            return judge(conn, args.run_id, args, db.DECISION_DROP)
        return show(conn, args.run_id, args)


if __name__ == "__main__":
    raise SystemExit(main())
