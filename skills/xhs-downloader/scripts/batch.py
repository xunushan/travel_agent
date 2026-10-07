#!/usr/bin/env python3
"""Phase two: collect the notes phase one picked, unattended, one at a time.

The runner only sequences the existing scripts: `discover.py` opens a note and
verifies the landing, `collect.py` extracts and downloads the parts it was
asked for, `db.py` says whether there is anything to do. Page semantics stay in
`locators.yaml`; nothing here knows about Xiaohongshu's DOM.

Its input is a **run**, not a hand-written plan: `--run-id` reads the run's
`keep` items back out of the index, so the notes collected are the notes the
agent approved, with no list copied between two commands. `--plan` still works
for a caller that has its own list.

Every note's content is fetched once and remembered. A second run over the same
run re-reads the note's body (the update time that says whether anything moved
lives on the note page, not on the search card), compares it with the index, and
then downloads only what the comparison left to do — with picture downloads made
idempotent by URL, so a note whose media is already on disk re-downloads none of
it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import db
import runtime
from collect import (
    BODY,
    COMMENTS,
    DOWNLOADS_FILENAME,
    MEDIA,
    NOTE_FILENAME,
    collect_note,
    normalize_parts,
)
from comments import COMMENTS_FILENAME, recapture_comments
from discover import (
    activate_tab,
    close_detail_if_open,
    discover,
    open_result,
    snapshot,
    wait_for_note,
)
from media import failed_downloads, read_downloads
from runtime import SEARCH_URL_TEMPLATE as SEARCH_URL
from runtime import chrome_agent, find_first, load_locators, one_line

# How much of the index to believe. `skip` is the default because it is the only
# one that makes a repeat run cheaper than the first one.
DEDUP_SKIP = "skip"      # stop as soon as the comparison finds nothing to do
DEDUP_REFRESH = "refresh"  # never stop at "nothing changed"; run the parts asked for
DEDUP_FORCE = "force"    # ignore the index entirely, re-download every picture

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


# --- the index as the run's memory -----------------------------------------


def keep_entries(conn, run_id: int) -> list[dict]:
    """The run's approved notes, in the order the reader ranked them.

    `keywords` is a one-item list because that is what `--plan` entries carry and
    `open_note` re-searches on it when a stored href's token has expired; the
    index records the keyword each note was found under for exactly this.
    """
    if not db.run_row(conn, run_id):
        raise SystemExit(f"run {run_id} 不存在；阶段一先跑 search.py run")
    entries = []
    for item in db.run_items(conn, run_id):
        if item["decision"] != db.DECISION_KEEP:
            continue
        entries.append({
            "noteId": item["note_id"],
            "url": item["href"],
            "keywords": [item["keyword"]] if item["keyword"] else [],
        })
    return entries


def artifacts_missing(note_dir: Path, parts: tuple[str, ...]) -> list[str]:
    """Which of the requested parts have no file on disk yet.

    The fast path: a note the index says was collected whose files are all still
    there needs no browser at all.
    """
    wanted = {
        BODY: note_dir / NOTE_FILENAME,
        MEDIA: note_dir / DOWNLOADS_FILENAME,
        COMMENTS: note_dir / COMMENTS_FILENAME,
    }
    return [name for name, path in wanted.items() if name in parts and not path.is_file()]


def media_rows(verification: dict, known: dict[str, str] | None = None) -> list[dict]:
    """Index rows for what a media run discovered, and what it actually fetched.

    Built from `downloads.json` rather than from the page, so the rows describe
    what actually happened rather than what was requested.

    A discovered image and the download that fetched it are matched by picture
    NAME, not by URL: the CDN signs the page's copy and the download's copy
    separately, so two spellings of one picture would otherwise look like one
    picture discovered-but-not-fetched plus one fetched-but-not-discovered.

    `known` is `{name: filename}` for what the index already has on disk, and it
    is what makes a deliberate skip safe to record. A discovered picture with no
    download behind it is normally just that — discovered — but when the run
    skipped it *because it was already fetched*, writing `discovered` would drop
    the file from the index, and every later run would fetch it again.
    """
    known = known or {}
    landed = {
        runtime.media_name(item.get("url")): item
        for item in verification.get("downloads") or []
        if item.get("url")
    }
    rows = []
    for image in verification.get("images") or []:
        url = image.get("src")
        if not url:
            continue
        name = runtime.media_name(url)
        done = landed.pop(name, {})
        filename = done.get("filename") or known.get(name)
        rows.append({
            # The name, not the URL the page happened to show: every row in the
            # `media` table is keyed this way, and a row carrying the day's
            # signature would compare as a different picture tomorrow.
            "url": name,
            "kind": "image",
            "state": done.get("state") or (
                db.MEDIA_COMPLETE if filename else db.MEDIA_DISCOVERED
            ),
            "filename": filename,
        })
    # Whatever the whitelist did not cover — a video, or an image the discovery
    # missed but the extension still fetched — is still media this note has.
    for name, item in landed.items():
        rows.append({
            "url": name,
            "kind": item.get("kind") or "video",
            "state": item.get("state"),
            "filename": item.get("filename") or known.get(name),
        })
    return rows


def parts_to_run(
    requested: tuple[str, ...], action: str, dedup: str, missing: list[str]
) -> tuple[str, ...]:
    """The parts still worth running, given what the comparison found.

    The body is never in the answer: it has already been read by the time the
    comparison exists, and reading it is what produced the comparison.

    "Nothing changed" is not the same as "nothing to do" — a note phase one
    screened has an unchanged body and no media yet — so a skip keeps whatever
    the requested parts have not produced a file for.
    """
    rest = tuple(part for part in requested if part != BODY)
    if dedup in (DEDUP_REFRESH, DEDUP_FORCE) or action != db.SKIP_DUP:
        return rest
    return tuple(part for part in rest if part in missing)


def already_done(stored: dict | None, missing: list[str], thread: dict | None, dedup: str) -> bool:
    """Whether this note can be skipped without opening a page at all.

    Three things have to hold, and each of them is a different kind of "done":
    the index says a previous run finished, every file the requested parts
    produce is still on disk, and — for a thread — the previous read did not
    record itself as having stopped early. That last one is the case a file
    check cannot see, and `comments.json` is where it says so.
    """
    if dedup != DEDUP_SKIP or not stored or stored["status"] != db.STATUS_COLLECTED:
        return False
    if missing:
        return False
    return not (thread and thread["possibly_incomplete"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab-id", required=True, type=int)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--run-id",
        type=int,
        help="阶段一的 run：采集这一轮里被标成 keep 的笔记（推荐）",
    )
    source.add_argument(
        "--plan", type=Path, help="JSON list of {noteId, url, keywords}（自备清单时用）"
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="笔记写到哪里；默认 <data-root>/notes，与阶段一同一个目录树",
    )
    parser.add_argument(
        "--part",
        default="all",
        help="要采哪些部分：all|body|media|comments 的组合，逗号分隔。"
        "默认 all（正文+媒体+评论）。阶段一已抓过正文，故补媒体与评论通常够了",
    )
    parser.add_argument(
        "--dedup",
        choices=(DEDUP_SKIP, DEDUP_REFRESH, DEDUP_FORCE),
        default=DEDUP_SKIP,
        help="skip=比对后没有变化就停手（默认）；refresh=不管有没有变化都跑完；"
        "force=完全不信索引，重新下载所有图片",
    )
    runtime.add_data_arguments(parser)
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
    root = runtime.data_root(args.data_root)
    output_root = args.output_root or runtime.notes_dir(root)
    requested = normalize_parts(
        "comments" if args.comments_only else args.part
    )
    if args.no_comments:
        requested = tuple(part for part in requested if part != COMMENTS)

    with db.open_db(runtime.db_path(root, args.db)) as conn:
        entries = (
            keep_entries(conn, args.run_id)
            if args.run_id
            else json.loads(args.plan.read_text())
        )
        if args.only:
            wanted = {item.strip() for item in args.only.split(",") if item.strip()}
            entries = [entry for entry in entries if entry["noteId"] in wanted]
        if args.limit:
            entries = entries[: args.limit]
        return collect_all(args, config, conn, entries, output_root, requested)


def collect_all(args, config, conn, entries, output_root, requested) -> int:
    log_path = args.log or (output_root / "_batch_log.jsonl")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    results = []

    for index, entry in enumerate(entries, start=1):
        note_id = entry["noteId"]
        note_dir = output_root / note_id
        output_path = note_dir / NOTE_FILENAME
        started = time.time()
        record = {"index": index, "noteId": note_id, "title": entry.get("title")}
        stored = db.note_row(conn, note_id)
        missing = artifacts_missing(note_dir, requested)
        thread = db.comments_row(conn, note_id) if COMMENTS in requested else None

        if args.skip_existing and output_path.is_file():
            record.update(status="SKIPPED", reason="note.json 已存在")
        elif already_done(stored, missing, thread, args.dedup):
            record.update(status="SKIPPED", reason="库里已采集完成，文件都在")
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
                        db.store_comments_meta(conn, note_id, comments)
                        record.pop("error", None)
                        break
                    page = snapshot(args.tab_id)
                    is_video = find_first(page, config["detail"]["video_media"]) is not None
                    # `force` is the only mode that ignores what is already on
                    # disk; every other one refuses to fetch a URL twice. The
                    # same map is what `media_rows` writes back below, so what a
                    # run skips and what it records as held cannot drift apart.
                    known_files = (
                        {} if args.dedup == DEDUP_FORCE
                        else db.downloaded_files(conn, note_id)
                    )
                    skip_urls = set(known_files)
                    common = dict(
                        note_dir=note_dir,
                        output_path=output_path,
                        comment_limit=args.comment_limit,
                        prefix=note_id,
                        download_images=not args.no_images,
                        download_media=is_video and not args.no_images,
                    )
                    # The body first, and on its own: it is cheap, and it is the
                    # only thing that can say whether anything moved. The search
                    # card carries no update time, so the comparison has no
                    # earlier opportunity than this.
                    note = collect_note(args.tab_id, parts=(BODY,), **common)
                    action = db.compare(stored, note, want_comments=COMMENTS in requested)
                    record["action"] = action
                    todo = parts_to_run(requested, action, args.dedup, missing)
                    if todo:
                        collect_note(args.tab_id, parts=todo, skip_urls=skip_urls, **common)
                        for part in todo:
                            db.touch(conn, note_id, part)
                    verification = read_downloads(note_dir) or {}
                    thread = comments_of(output_path)
                    seen = media_rows(verification, known_files)
                    if db.media_urls_changed(conn, note_id, [row["url"] for row in seen]):
                        record["mediaChanged"] = True
                    excerpt, excerpt_kind = db.excerpt_of(note.get("content"))
                    db.store_note(
                        conn,
                        note,
                        note_dir=note_dir,
                        excerpt=excerpt,
                        excerpt_kind=excerpt_kind,
                        status=db.STATUS_COLLECTED,
                        media=seen,
                    )
                    if thread:
                        db.store_comments_meta(conn, note_id, thread)
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
                        # Recorded as failed rather than left as it was, so the
                        # next run's fast path does not skip a note that has no
                        # files to show for itself.
                        db.set_status(conn, note_id, db.STATUS_FAILED)
                        break
                    record.update(error=f"{message}（正在重启 chrome-agent 后重试）")
                    try:
                        recover_browser(args.tab_id)
                    except Exception as recovery:
                        record.update(status="FAILED", error=str(recovery))
                        db.set_status(conn, note_id, db.STATUS_FAILED)
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
            f"{record.get('action','')} {record.get('kind','')} "
            f"正文{record.get('bodyChars','-')} "
            f"评论{record.get('comments','-')}{replies} "
            f"图{record.get('images','-')} 下载{record.get('downloads','-')} "
            f"{'图片有变 ' if record.get('mediaChanged') else ''}"
            f"{record['elapsedSec']}s {record.get('reason','')} {record.get('error','')}",
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
