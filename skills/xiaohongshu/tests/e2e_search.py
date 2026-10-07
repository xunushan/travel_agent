#!/usr/bin/env python3
"""Search-flow smoke test, run by hand against a signed-in Chrome.

Needs `chrome-agent` on PATH and its extension loaded. It speaks only the CLI,
never an imported module, because that is the whole contract with the tool
skill: this site skill owns what to collect and where it lands, not how the
browser is driven.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time


def cli(*args: str) -> dict:
    result = subprocess.run(
        ["chrome-agent", *args, "--json"],
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    data = json.loads(result.stdout)
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def find_search_ref(snapshot: dict) -> str:
    candidates = []
    for element in snapshot.get("elements", []):
        evidence = " ".join(
            str(element.get(key) or "") for key in ("role", "name", "placeholder", "text", "type")
        ).lower()
        if element.get("interactive") and ("搜索" in evidence or "search" in evidence):
            candidates.append(element)
    if not candidates:
        raise RuntimeError("未在 DOM 快照中找到搜索输入框")
    inputs = [item for item in candidates if item.get("tag") == "input"]
    return (inputs or candidates)[0]["ref"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("keyword", nargs="?", default="稻城亚丁攻略")
    args = parser.parse_args()

    cli("ensure", "--launch-if-missing", "--wait-for-extension", "--timeout", "30")
    tabs = cli("tabs", "list", "--domain", "xiaohongshu.com").get("tabs", [])
    if tabs:
        tab_id = tabs[0]["id"]
    else:
        tab_id = cli("tabs", "open", "https://www.xiaohongshu.com/")["tabId"]
        time.sleep(3)

    cli("tabs", "claim", str(tab_id))
    snapshot = cli("page", "snapshot", "--tab-id", str(tab_id), "--scope", "full")
    search_ref = find_search_ref(snapshot)
    cli("page", "validate", "--tab-id", str(tab_id), "--ref", search_ref)
    cli("page", "fill", "--tab-id", str(tab_id), "--ref", search_ref, "--value", args.keyword)
    cli("page", "keypress", "--tab-id", str(tab_id), "--ref", search_ref, "--keys", "Enter")
    cli(
        "page",
        "wait",
        "--tab-id",
        str(tab_id),
        "--selector",
        'a[href*="/search_result/"]',
        "--timeout",
        "15000",
    )
    extracted = cli("page", "extract", "--tab-id", str(tab_id))
    note_links = [
        link for link in extracted.get("links", []) if "/search_result/" in link.get("href", "")
    ]
    print(
        json.dumps(
            {
                "keyword": args.keyword,
                "tabId": tab_id,
                "url": extracted.get("url"),
                "resultCount": len(note_links),
                "sampleResults": note_links[:5],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if note_links else 1


if __name__ == "__main__":
    raise SystemExit(main())
