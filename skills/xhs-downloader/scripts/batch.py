#!/usr/bin/env python3
"""Collect a list of notes unattended, one at a time.

The runner only sequences the existing Playbook scripts: `discover.py` opens a
note and verifies the landing, `collect.py` extracts and downloads it. Page
semantics stay in `locators.yaml`; nothing here knows about Xiaohongshu's DOM.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from collect import collect_note
from comments import recapture_comments
from discover import (
    activate_tab,
    close_detail_if_open,
    discover,
    open_result,
    snapshot,
    wait_for_note,
)
from media import failed_downloads, read_downloads
from runtime import chrome_agent, find_first, load_locators

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={keyword}&source=web_search_result_notes&type=51"

# Transport failures, not page failures: retrying the note is the right answer.
BROWSER_DOWN_MARKERS = (
    "Daemon socket not found",
    "No extension connected",
    "Could not establish connection",
    "Receiving end does not exist",
    "Extension disconnected",
    "back/forward cache",
    "message channel is closed",
)


def search_url(keyword: str) -> str:
    return SEARCH_URL.format(keyword=quote(keyword))


def read_note(output_path: Path) -> dict:
    return json.loads(output_path.read_text(encoding="utf-8"))


def body_chars(note: dict) -> int:
    """Length of the note body, for both this layout and the older one.

    An older note.json kept the body as `{"text": ...}`; measuring that with
    `len()` counts its keys, which is how a 3-character body was reported for
    every legacy note in a `--comments-only` run.
    """
    content = note.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, dict):
        return len(content.get("text") or "")
    return 0


def comments_of(output_path: Path) -> dict | None:
    """The note's own comment file, if that note has had its thread read."""
    path = output_path.parent / "comments.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


DAEMON_LOG = Path.home() / "Library/Application Support/ChromeAgent/logs/daemon.log"


def browser_reachable(tab_id: int) -> bool:
    """Probe the whole path, not just the socket.

    The daemon answers `tabs list` as soon as it is listening, but forwarding
    stays broken until the extension re-registers — 30s or more later. A page
    call on the tab we actually drive is the only honest check.
    """
    probe = subprocess.run(
        [
            "chrome-agent", "page", "snapshot", "--tab-id", str(tab_id),
            "--scope", "viewport", "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0 and probe.stdout.lstrip().startswith("{")


def _wait_for_registration(marker: int, timeout: float) -> bool:
    """Wait for a fresh 'Extension registered' line past `marker` in the log."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with DAEMON_LOG.open(errors="replace") as handle:
                handle.seek(marker)
                fresh = handle.read()
        except OSError:
            fresh = ""
        if "Extension registered" in fresh:
            return True
        time.sleep(2)
    return False


def recover_browser(tab_id: int) -> None:
    """Bring the CLI, daemon and extension back into agreement.

    `chrome-agent stop` is issued by other tooling on this machine, and a native
    host that outlives its daemon keeps the extension from re-registering. Both
    leave the socket dead while Chrome is perfectly healthy.
    """
    if browser_reachable(tab_id):
        return
    for timeout in (60.0, 90.0):
        marker = DAEMON_LOG.stat().st_size if DAEMON_LOG.is_file() else 0
        subprocess.run(
            ["chrome-agent", "start"], capture_output=True, text=True, check=False
        )
        if _wait_for_registration(marker, timeout) and browser_reachable(tab_id):
            return
        # A native host left over from the previous daemon holds the port and
        # stops the extension from reconnecting; clearing it is what fixes that.
        subprocess.run(
            ["pkill", "-f", "native_host/native_host.py"], capture_output=True, check=False
        )
    raise RuntimeError("chrome-agent 反复重启后仍无法连上扩展，请手动检查 Chrome 扩展是否已加载")


def open_note(tab_id: int, entry: dict, config: dict, mode: str) -> str:
    """Bring the wanted note on screen, verifying it actually landed.

    The stored href is a search-result URL whose access token may still be
    live; when it is not, the note is looked for again in the live search
    results for each of its keywords.
    """
    want = entry["noteId"]
    # Chrome throttles background tabs, and the note only appends more comments
    # from its event loop; a backgrounded tab reads the first page and stops.
    activate_tab(tab_id)
    if entry.get("url"):
        chrome_agent("tabs", "navigate", str(tab_id), entry["url"])
        if wait_for_note(tab_id, want, config):
            return "stored-url"

    for keyword in entry.get("keywords") or []:
        chrome_agent("tabs", "navigate", str(tab_id), search_url(keyword))
        for _ in range(4):  # the result grid fills in progressively
            page = snapshot(tab_id)
            selected = next(
                (item for item in discover(page, config, 100) if item["noteId"] == want),
                None,
            )
            if selected:
                open_result(tab_id, selected, mode, config)
                return f"search:{keyword}"
            time.sleep(2)

    raise RuntimeError(f"{want} 既打不开存下来的 URL，也没能在它所属关键词的搜索结果里找到")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab-id", required=True, type=int)
    parser.add_argument(
        "--plan", required=True, type=Path, help="JSON list of {noteId, url, keywords}"
    )
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument(
        "--log",
        type=Path,
        help="JSONL run log; defaults to <output-root>/_batch_log.jsonl",
    )
    parser.add_argument("--open-mode", choices=("click", "navigate"), default="click")
    parser.add_argument(
        "--interval", type=float, default=15.0, help="Seconds to wait between notes"
    )
    parser.add_argument("--limit", type=int, help="Collect at most this many notes")
    parser.add_argument("--only", help="Comma-separated noteIds to restrict the run to")
    parser.add_argument(
        "--skip-existing", action="store_true", help="Skip notes that already have note.json"
    )
    parser.add_argument(
        "--no-comments", action="store_true", help="Skip the comment thread entirely"
    )
    parser.add_argument(
        "--comment-limit",
        type=int,
        help="Top-level comments per note; a comment and its replies count as one. "
        "Defaults to locators.yaml scroll.comments.limit (10)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Collect text and comments only; download no images or videos",
    )
    parser.add_argument(
        "--comments-only",
        action="store_true",
        help="Refresh only the comment section of existing note.json files, "
        "leaving already-downloaded media untouched",
    )
    args = parser.parse_args()

    config = load_locators()
    entries = json.loads(args.plan.read_text())
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        entries = [entry for entry in entries if entry["noteId"] in wanted]
    if args.limit:
        entries = entries[: args.limit]

    log_path = args.log or (args.output_root / "_batch_log.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results = []

    for index, entry in enumerate(entries, start=1):
        note_id = entry["noteId"]
        note_dir = args.output_root / note_id
        output_path = note_dir / "note.json"
        started = time.time()
        record = {"index": index, "noteId": note_id, "title": entry.get("title")}

        if args.skip_existing and output_path.is_file():
            record.update(status="SKIPPED", reason="note.json 已存在")
        else:
            for attempt in range(2):
                try:
                    record["closedDetail"] = close_detail_if_open(args.tab_id, config)
                    record["opened"] = open_note(args.tab_id, entry, config, args.open_mode)
                    if args.comments_only:
                        comments = recapture_comments(
                            args.tab_id,
                            output_path=output_path,
                            comment_limit=args.comment_limit,
                        )
                        record.update(
                            status="OK",
                            kind="评论重采",
                            bodyChars=body_chars(read_note(output_path)),
                            comments=comments["collected"],
                            # Already the whole forest, at every depth.
                            replies=comments["repliesCollected"],
                            declaredTotal=comments.get("declaredTotal"),
                            warnings=comments["warnings"],
                        )
                        record.pop("error", None)
                        break
                    page = snapshot(args.tab_id)
                    is_video = find_first(page, config["detail"]["video_media"]) is not None
                    note = collect_note(
                        args.tab_id,
                        note_dir=note_dir,
                        output_path=output_path,
                        comment_limit=args.comment_limit,
                        download_images=not args.no_images,
                        download_media=is_video and not args.no_images,
                        prefix=note_id,
                        with_comments=not args.no_comments,
                    )
                    verification = read_downloads(note_dir) or {}
                    thread = comments_of(output_path)
                    record.update(
                        status="OK",
                        kind="视频" if is_video else "图文",
                        bodyChars=body_chars(note),
                        comments=thread["collected"] if thread else 0,
                        images=len(verification.get("images", [])),
                        downloads=len(verification.get("downloads", [])),
                        failedDownloads=len(
                            failed_downloads(verification.get("downloads", []))
                        ),
                        warnings=note["warnings"],
                    )
                    record.pop("error", None)
                    break
                except Exception as error:  # one bad note must not stop the run
                    message = str(error).strip().splitlines()[-1][:300]
                    if attempt or not any(
                        marker in message for marker in BROWSER_DOWN_MARKERS
                    ):
                        record.update(status="FAILED", error=message)
                        break
                    record.update(error=f"{message}（正在重启 chrome-agent 后重试）")
                    try:
                        recover_browser(args.tab_id)
                    except Exception as recovery:
                        record.update(status="FAILED", error=str(recovery))
                        break

        record["elapsedSec"] = round(time.time() - started, 1)
        results.append(record)
        replies = (
            f" 回复{record['replies']}/声明{record.get('declaredTotal')}"
            if "replies" in record
            else ""
        )
        print(
            f"[{index}/{len(entries)}] {record['status']:7} {note_id} "
            f"{record.get('kind','')} 正文{record.get('bodyChars','-')} "
            f"评论{record.get('comments','-')}{replies} "
            f"图{record.get('images','-')} 下载{record.get('downloads','-')} "
            f"{record['elapsedSec']}s {record.get('error','')}",
            flush=True,
        )
        with log_path.open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        if index < len(entries) and args.interval:
            time.sleep(args.interval)

    tally = {}
    for record in results:
        tally[record["status"]] = tally.get(record["status"], 0) + 1
    print(f"\n合计: {tally}")
    for record in results:
        if record["status"] != "OK":
            print(
                f"  {record['status']:7} {record['noteId']} "
                f"{record.get('error', record.get('reason',''))}"
            )
    return 0 if not tally.get("FAILED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
