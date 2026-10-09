#!/usr/bin/env python3
"""Download the requested content parts of the notes named on the command line.

The notes come from a `discover.py` candidate list (`--from`), from ids
(`--note-id`) or from URLs (`--url`). Nothing is judged here: which notes are
worth keeping, which keywords to use and when to stop are the caller's.

**Whether something has been downloaded is answered by the files.** There is no
"downloaded" flag anywhere, so there is no way for a flag and the disk to
disagree. The rules are two:

1. **Have it, skip it; don't, get it.** Each requested part is checked on disk;
   a part that is there and complete is not fetched again. Pictures and videos
   are checked entry by entry in `downloads.json`, so a run that lost one
   picture fetches only that one.
2. **Changed, refresh it.** Opening a note reads its own `lastUpdateTime` and
   its text; if either says the note is not what was stored, the note is
   refreshed as a whole — every part already on disk is re-fetched, plus the
   ones this run was asked for — and files that no longer belong to the current
   version are removed. A note is either a video or a gallery, never both, so
   "the whole note" is a handful of files.

   **Comments are not part of "the whole note".** They are the thread's content,
   not the author's, and they change on their own, so an edit is no reason to
   re-read them: they follow rule 1, and `--force` (or asking when there is no
   `comments.json` yet) is how a caller gets a fresh copy.

   `--force` is the narrower tool: it ignores the disk for the parts that were
   asked for and only those, so `--part comment --force` re-reads the thread
   without dragging the gallery along.

Only opening a page can discover a change, so a note whose requested parts are
all present is skipped without opening anything — `--check-update` is how a
caller asks for the page to be opened anyway.

The note's own text is read on **every** page this opens, whatever was asked
for. Two reasons, and the second is the one that matters: the title is what
names the note's directory, and title+body+tags is the only thing the "did it
change?" comparison has to go on. A read that skipped the body could not answer
either.

An error is reported as an error. A note whose pictures failed to download is
never described as a note without pictures.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import db
import discover
import runtime
from collect import (
    ALL_PARTS,
    COMMENT,
    COVER,
    DOWNLOADS_FILENAME,
    IMAGE,
    NOTE,
    NOTE_FILENAME,
    VIDEO,
    collect_note,
    normalize_parts,
)
from comments import COMMENTS_FILENAME
from media import (
    KIND_IMAGE,
    KIND_VIDEO,
    downloaded_names,
    kind_absent,
    media_missing,
    read_downloads,
)

# What the note did, which is what a caller reads out of the report.
ACTION_NEW = "new"          # never stored: every requested part is fetched
ACTION_FILL = "fill"        # stored and unchanged: only the missing parts
ACTION_REFRESH = "refresh"  # the note changed: every requested part is re-fetched
ACTION_CHECK = "check"      # opened to verify, and it was unchanged
ACTION_SKIP = "skip"        # nothing to do; no page was opened

# Which content part is answered by which media kind in `media`'s terms.
MEDIA_PARTS = {IMAGE: KIND_IMAGE, VIDEO: KIND_VIDEO}

# Transport failures, not page failures: the same note is worth retrying.
BROWSER_DOWN_MARKERS = (
    "Daemon socket not found",
    "No extension connected",
    "Could not establish connection",
    "Receiving end does not exist",
    "Extension disconnected",
    "back/forward cache",
    "message channel is closed",
)

DAEMON_LOG = Path.home() / "Library/Application Support/ChromeAgent/logs/daemon.log"


# --- browser liveness -------------------------------------------------------


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
    leave the socket dead while Chrome itself is perfectly healthy.
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
    raise RuntimeError("chrome-agent 反复重启后仍连不上扩展，请手动确认 Chrome 扩展已加载")


# --- what is on disk --------------------------------------------------------


def cover_paths(note_dir: Path | None) -> list[Path]:
    """The cover file(s) in a note directory, whatever extension it landed with."""
    if note_dir is None or not note_dir.is_dir():
        return []
    return sorted(note_dir.glob("cover.*"))


def comments_complete(path: Path) -> bool:
    """Whether a thread file is there and did not record itself as cut short.

    The thread's own fields are the only authority: a `collected` count means
    nothing without knowing what the page declared it had.
    """
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return not payload.get("possiblyIncomplete")


def missing_parts(note_dir: Path | None, parts) -> list[str]:
    """Which of `parts` are not on disk, and why.

    This is the whole of rule 1, and the reason a repeat run is cheap: an empty
    answer means no page is opened at all. `note_dir` is None for a note that has
    never been downloaded, in which case every part is missing by definition.

    The reasons are the same sentences `media.media_missing` produces, so a
    caller can tell "never looked for the video" from "the video failed".
    """
    if note_dir is None:
        return list(parts)
    verification = read_downloads(note_dir)
    missing: list[str] = []
    for part in parts:
        if part == NOTE and not (note_dir / NOTE_FILENAME).is_file():
            missing.append(part)
        elif part == COVER and not cover_paths(note_dir):
            missing.append(part)
        elif part in MEDIA_PARTS and media_missing(verification, (MEDIA_PARTS[part],)):
            missing.append(part)
        elif part == COMMENT and not comments_complete(note_dir / COMMENTS_FILENAME):
            missing.append(part)
    return missing


def downloaded_parts(note_dir: Path) -> set[str]:
    """Which parts this note already has, read off the disk."""
    verification = read_downloads(note_dir)
    held = set()
    if (note_dir / NOTE_FILENAME).is_file():
        held.add(NOTE)
    if cover_paths(note_dir):
        held.add(COVER)
    if not media_missing(verification, (KIND_IMAGE,)):
        held.add(IMAGE)
    if not media_missing(verification, (KIND_VIDEO,)):
        held.add(VIDEO)
    if comments_complete(note_dir / COMMENTS_FILENAME):
        held.add(COMMENT)
    return held


def part_state(
    part: str, still: set[str], fetched: set[str], held: set[str], stored: dict | None
) -> str:
    """How one requested part reads in the report.

    `无此件` is checked before `已下载` because it is the more specific truth: a
    video note's gallery, and a picture note's video, are parts that were asked
    about and answered "none". Calling those "downloaded" would be a claim about
    work nobody did, and the reader has no other way to tell the two apart.
    """
    if part in still:
        return "未完成"
    if part in MEDIA_PARTS and kind_absent(stored, MEDIA_PARTS[part]):
        return "无此件"
    if part in fetched:
        return "已下载"
    if part in held:
        return "已存在"
    return "未完成"


def note_warnings(note_dir: Path) -> list[str]:
    """Every warning the note's own files recorded, in file order, deduplicated."""
    found: list[str] = []
    for name in (NOTE_FILENAME, DOWNLOADS_FILENAME, COMMENTS_FILENAME):
        try:
            payload = json.loads((note_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for item in payload.get("warnings") or []:
            if item not in found:
                found.append(item)
    return found


def prune_stale(note_dir: Path, verification: dict) -> list[str]:
    """Delete files that do not belong to the version now on disk.

    Run only after a refresh, and only for a kind whose every entry landed: a
    failed download means the old file may be the only copy of that picture, and
    trading a stale file for no file is strictly worse.

    The kind has to have been READ this version to be pruned, and `examined` is
    what says so — not the presence of entries. Those part ways on a note whose
    answer is "none": a video note has no gallery, so its image kind is examined
    with zero entries, and files left there by an earlier version are exactly
    what has to go. An unexamined kind is one this run never looked at, and its
    files are still the current answer.
    """
    removed: list[str] = []
    examined = (verification or {}).get("examined") or []
    for kind, dirname in ((KIND_IMAGE, "images"), (KIND_VIDEO, "videos")):
        if kind not in examined:
            continue
        mine = [item for item in (verification or {}).get("downloads") or []
                if item.get("kind") == kind]
        if any(item.get("state") != "complete" for item in mine):
            continue
        keep = {Path(item["filename"]).name for item in mine if item.get("filename")}
        directory = note_dir / dirname
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and path.name not in keep:
                path.unlink()
                removed.append(str(path))
        if not any(directory.iterdir()):
            # A directory with nothing in it still reads as "this note has an
            # image part" — which is the whole thing being fixed.
            directory.rmdir()
    return removed


# --- the note's directory ---------------------------------------------------

def place_note_dir(staging: Path, target: Path) -> Path:
    """Put a newly read note where the directory named after its title belongs.

    Called only for a note with no directory yet — an existing one is never
    renamed. That is not tidiness: `downloads.json` records where each file went
    as an absolute path, so renaming a note's directory makes every one of its
    pictures read as missing, and the next run downloads the whole gallery again
    beside the files that are already there.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if staging == target or not staging.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        return target
    if target.exists():
        # An interrupted earlier run can leave the correctly-named directory
        # behind without a note.json in it. Same note by construction (the name
        # starts with its id), so the files go in rather than beside.
        for item in staging.iterdir():
            shutil.move(str(item), str(target / item.name))
        staging.rmdir()
        return target
    shutil.move(str(staging), str(target))
    return target


# --- the run ----------------------------------------------------------------


def split_list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def entries_from(args) -> list[dict]:
    """The notes this run works on, in the order they were given.

    Three ways in, and none of them is a query: a candidate list, a list of ids,
    or a list of URLs. The tool keeps no "what should be downloaded" list of its
    own — that was the ledger's job, and the ledger is the caller's.
    """
    entries: list[dict] = []
    seen: set[str] = set()

    def add(note_id: str | None, url: str | None) -> None:
        if not note_id or note_id in seen:
            return
        seen.add(note_id)
        entries.append({"noteId": note_id, "url": url})

    if args.from_file:
        payload = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
        for item in payload.get("candidates") or []:
            add(item.get("noteId"), item.get("url"))
    for url in split_list(args.url):
        add(runtime.note_id(url), url)
    for note_id in split_list(args.note_id):
        add(note_id, None)
    return entries


def resolve_url(entry: dict, stored: dict | None) -> str | None:
    """The detail URL to replay: the one given, else the one the last run stored.

    Never rebuilt from the id: the `xsec_token` in a stored URL is the access
    context, and a URL without one does not open the note.
    """
    return entry.get("url") or (stored or {}).get("url")


def handle_note(args, config: dict, conn, entry: dict, requested, notes_root: Path) -> dict:
    """Collect one note, following the two rules, and report what happened.

    The order is forced by one fact: a note's directory is named after its
    title, and the title is inside the note. So the page is opened first, the
    note itself is read, the directory is settled, and only then are the media
    parts fetched — `downloads.json` records absolute paths, so the directory
    cannot move afterwards.
    """
    note_id = entry["noteId"]
    stored = db.note_row(conn, note_id)
    existing = runtime.find_note_dir(notes_root, note_id, (stored or {}).get("note_dir"))
    record: dict = {
        "noteId": note_id,
        "title": (stored or {}).get("title"),
        "parts": {},
        "warnings": [],
    }

    missing = missing_parts(existing, requested)
    if existing is not None and not missing and not args.force and not args.check_update:
        # Nothing was fetched and nothing is missing, so every part is either
        # already on disk or one the page answered "none" for — the same two
        # readings the reading path reports, without opening the page to repeat
        # what `downloads.json` already says.
        held = downloaded_parts(existing)
        stored_media = read_downloads(existing)
        record.update(
            status="OK",
            action=ACTION_SKIP,
            noteDir=str(existing),
            parts={
                part: part_state(part, set(), set(), held, stored_media)
                for part in requested
            },
            reason="请求的件盘上都在，未开页面",
        )
        return record

    url = resolve_url(entry, stored)
    if not url:
        record.update(
            status="FAILED",
            reason="没有可重放的详情页 URL（索引里也没有）——先 discover 再下载",
        )
        return record

    discover.close_detail_if_open(args.tab_id, config)
    discover.open_note(args.tab_id, url, note_id, config, args.open_timeout)

    # 1. The note itself, always. Its title names the directory; its text and
    #    edit time are the only evidence of whether the note changed.
    staging = existing or (notes_root / note_id)
    fresh = collect_note(
        args.tab_id,
        note_dir=staging,
        output_path=staging / NOTE_FILENAME,
        parts=(NOTE,),
        comment_limit=args.comment_limit,
        prefix=args.prefix,
    )
    note_dir = existing or place_note_dir(
        staging, notes_root / runtime.note_dir_name(note_id, fresh.get("title"))
    )

    changed = bool(stored) and db.content_changed(stored, fresh)
    refresh = bool(args.force or changed)
    if changed:
        # A note that moved is refreshed as a whole: what is already on disk is
        # part of what changed, so it is brought to the new version too, and the
        # request only adds to that set. Leaving yesterday's pictures beside a
        # `note.json` that describes today's is the one state the archive must
        # not be in.
        fetched = (set(requested) | downloaded_parts(note_dir)) - {COMMENT}
        # Comments are not the note's content — they are the thread's, and they
        # change on their own — so an edit is no reason to re-read them. They
        # follow rule 1: asked for and absent, or asked for and forced.
        if COMMENT in requested and (COMMENT in missing or args.force):
            fetched.add(COMMENT)
    elif args.force:
        # `--force` is narrower: it ignores the disk for what was asked for and
        # touches nothing else, so `--part comment --force` re-reads the thread
        # without dragging the gallery along.
        fetched = set(requested)
    else:
        fetched = set(missing) & set(requested)
    fetched.discard(NOTE)  # already read and written above

    # 2. Everything else. On a refresh nothing on disk is offered as skippable,
    #    so every part fetched here really is fetched again and the leftovers of
    #    the old version are pruned; on a fill, only what is missing is fetched.
    if fetched:
        collect_note(
            args.tab_id,
            note_dir=note_dir,
            output_path=note_dir / NOTE_FILENAME,
            parts=tuple(fetched),
            comment_limit=args.comment_limit,
            prefix=args.prefix,
            known_files={} if refresh else downloaded_names(read_downloads(note_dir)),
            force=refresh,
        )
    if refresh:
        prune_stale(note_dir, read_downloads(note_dir))

    db.store_note(conn, fresh, note_dir=note_dir)

    # A changed note is refreshed as a whole, so parts that were already on disk
    # were fetched again too. They are named in the report rather than rewritten
    # behind the caller's back — and a failure in one of them is a real failure,
    # because the point of refreshing them is that the archive holds one version.
    reported = list(requested) + [part for part in ALL_PARTS if part in fetched and part not in requested]

    still = set(missing_parts(note_dir, reported))
    fetched.add(NOTE)  # written by the read above, whatever was asked for
    if not stored:
        action = ACTION_NEW
    elif changed:
        action = ACTION_REFRESH
    elif fetched - {NOTE}:
        action = ACTION_FILL
    else:
        action = ACTION_CHECK

    held = downloaded_parts(note_dir)
    stored_media = read_downloads(note_dir)
    record.update(
        status="FAILED" if still else "OK",
        action=action,
        title=fresh.get("title"),
        noteDir=str(note_dir),
        updatedAt=fresh.get("updatedAt"),
        changed=changed,
        parts={
            part: part_state(part, still, fetched, held, stored_media)
            for part in reported
        },
        warnings=note_warnings(note_dir),
    )
    if still:
        record["reason"] = "有件没下成：" + "；".join(
            f"{part}={'; '.join(media_missing(stored_media, (MEDIA_PARTS[part],))) or '未落盘'}"
            if part in MEDIA_PARTS
            else f"{part}=未落盘"
            for part in reported if part in still
        )
    return record


def process(args, config, conn, entry, requested, notes_root) -> dict:
    """`handle_note`, with one restart-and-retry for a dead browser."""
    for attempt in range(2):
        try:
            return handle_note(args, config, conn, entry, requested, notes_root)
        except Exception as error:
            message = str(error).strip().splitlines()[-1][:300]
            if attempt or not any(marker in message for marker in BROWSER_DOWN_MARKERS):
                return {"noteId": entry["noteId"], "status": "FAILED", "reason": message}
            try:
                recover_browser(args.tab_id)
            except Exception as recovery:
                return {"noteId": entry["noteId"], "status": "FAILED", "reason": str(recovery)}
    raise AssertionError("unreachable")


def report(record: dict, index: int, total: int) -> None:
    """One line per note, on stderr so `--json` on stdout stays parseable."""
    parts = " ".join(f"{name}={state}" for name, state in (record.get("parts") or {}).items())
    print(
        f"[{index}/{total}] {record['status']:6} {record.get('action', ''):7} "
        f"{record['noteId']} {runtime.one_line(record.get('title'), 24)} "
        f"{parts} {record.get('reason', '')}".rstrip(),
        file=sys.stderr,
        flush=True,
    )
    for warning in record.get("warnings") or []:
        print(f"           ! {warning}", file=sys.stderr, flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="下载指定笔记的指定内容件；盘上已有的不再下，变了就整篇刷新"
    )
    parser.add_argument("--tab-id", required=True, type=int)
    parser.add_argument("--note-id", help="一个参数、逗号分隔的笔记 id")
    parser.add_argument("--url", help="一个参数、逗号分隔的详情页链接")
    parser.add_argument("--from", dest="from_file", help="discover.py 输出的候选清单 JSON")
    parser.add_argument(
        "--part", default="all",
        help=f"要哪些内容件：{','.join(ALL_PARTS)} 的组合，或 all（默认）",
    )
    parser.add_argument(
        "--check-update", action="store_true",
        help="即使件都在也打开页面核验版本；变了就整篇刷新",
    )
    parser.add_argument("--force", action="store_true", help="无视盘上已有的，全部重下")
    parser.add_argument("--limit", type=int, help="最多处理多少篇")
    parser.add_argument("--interval", type=float, default=15.0,
                        help="真正开了页面的笔记之间的间隔秒数（跳过的不等）")
    parser.add_argument("--comment-limit", type=int, help="每篇读多少条顶层评论")
    parser.add_argument("--open-timeout", type=float, default=None, help="等待单篇笔记打开的秒数")
    parser.add_argument("--prefix", default="xiaohongshu-note")
    parser.add_argument("--json", action="store_true", help="报告以 JSON 输出到 stdout")
    runtime.add_data_arguments(parser)
    args = parser.parse_args()

    requested = normalize_parts(args.part)
    root = runtime.data_root(args.data_root)
    notes_root = runtime.notes_dir(root)
    config = runtime.load_locators()
    entries = entries_from(args)
    if args.limit:
        entries = entries[: args.limit]
    if not entries:
        print("没有要下载的笔记：用 --note-id / --url / --from 指定")
        return 0

    results: list[dict] = []
    with db.open_db(runtime.db_path(root, args.db)) as conn:
        for index, entry in enumerate(entries, start=1):
            record = process(args, config, conn, entry, requested, notes_root)
            record["index"] = index
            results.append(record)
            conn.commit()
            report(record, index, len(entries))
            opened = record.get("action") not in (ACTION_SKIP, None)
            if index < len(entries) and args.interval and opened:
                time.sleep(args.interval)

    if args.json:
        json.dump(results, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        tally: dict[str, int] = {}
        for record in results:
            tally[record["status"]] = tally.get(record["status"], 0) + 1
        print(f"合计 {tally}", file=sys.stderr)
        for record in results:
            if record["status"] != "OK":
                print(
                    f"  {record['noteId']} {record.get('reason', '')}",
                    file=sys.stderr,
                )
    return 1 if any(record["status"] == "FAILED" for record in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
