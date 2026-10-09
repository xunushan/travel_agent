"""A fake Xiaohongshu that speaks only the `chrome-agent` CLI.

Everything `discover.py` does to a page it does through `chrome-agent`, so the
fake is a CLI stand-in rather than a mocked module: it answers `tabs navigate`,
`page snapshot`, `page click`, `page text`, `page images` and
`page download-images` from a small model of the site, and it models the two
behaviours the real one was measured to have that shape the design —

* the filter panel is opened by pressing 筛选 and options register by class
  (`active`), never by URL, so `page click` here really does move the state the
  next `page snapshot` reports;
* the search results page is client-rendered, so a snapshot taken before the
  cards mount has none.

Shared by `test_discover.py` and `test_media.py`; it lives in its own module
because pytest only collects `test_*.py` but every test module may import it.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import db
import pytest
import runtime

import discover
from test_playbook import element, patch_browser

NOTE_A = "6aba9036000000001c00d596"
NOTE_B = "6abfda1f000000001803b413"
NOTE_C = "6ac5fbbf0000000018007118"

IMAGE_A = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/1040g3aaa.webp"
IMAGE_B = "https://sns-webpic-qc.xhscdn.com/notes_pre_post/1040g3bbb.webp"

# What the real page offers, measured 2026-10-07 (repo docs/xhs-筛选机制与实测.md).
PANEL = {
    "排序依据": ["综合", "最新", "最多点赞", "最多收藏", "最多评论"],
    "笔记类型": ["不限", "视频", "图文"],
    "发布时间": ["不限", "一天内", "一周内", "半年内"],
    "搜索范围": ["不限", "已看过", "未看过", "已关注"],
}
CHANNELS = ["全部", "图文", "视频", "用户"]
DEFAULTS = {"排序依据": "综合", "笔记类型": "不限", "发布时间": "不限", "搜索范围": "不限"}

SEARCH_PATH = "/search_result"


def rect(x: float, y: float, width: float, height: float) -> dict:
    return {"x": x, "y": y, "width": width, "height": height}


def state_blob(note: str, *, title: str, body: str, updated_ms: int, published_ms: int,
               kind: str = "normal") -> str:
    return (
        "window.__INITIAL_STATE__="
        + json.dumps(
            {"note": {"noteDetailMap": {note: {"note": {
                "noteId": note, "title": title, "desc": body, "type": kind,
                "time": published_ms, "lastUpdateTime": updated_ms,
                "interactInfo": {"likedCount": "91", "collectedCount": "32",
                                 "commentCount": "18", "shareCount": "0", "niceCount": ""},
                "tagList": [{"id": "t", "name": "川西", "type": "topic"}],
                "imageList": [{"url": "", "urlDefault": IMAGE_A, "width": "1440", "height": "2400"},
                              {"url": "", "urlDefault": IMAGE_B, "width": "1440", "height": "2400"}],
                "user": {"userId": "u1", "nickname": "冷三岁"}}}}}},
            ensure_ascii=False,
        )
        + "};window.__SSR__=true"
    )


def note_page(note: str, *, title: str, body: str, updated_ms: int = 1785590297000,
              published_ms: int = 1785590297000,
              video: bool = False) -> tuple[dict, dict]:
    """A note detail page and the per-ref texts the CLI would return for it.

    `video` swaps the carousel for a player, which is the one structural
    difference between the two kinds of note: measured 2026-10-09, a video note
    has `video-player-media` and no `xhs-slider-container`, which is what sends
    the gallery read to the fallback container and finds the poster there.
    """
    media_element = (
        element("player", className="video-player-media", rect=rect(0, 0, 600, 500))
        if video
        else element("slider", className="xhs-slider-container", rect=rect(0, 0, 600, 500))
    )
    page = {
        "url": f"https://www.xiaohongshu.com/explore/{note}?xsec_token=tok",
        "elements": [
            element("container", className="note-container", rect=rect(0, 0, 600, 740)),
            media_element,
            element("body", className="note-text", text=body, rect=rect(0, 500, 600, 200)),
            element("h1", tag="h1", className="title", text=title, rect=rect(0, 480, 600, 20)),
            # The overlay's close control. It exists so a phase-two run, which
            # closes whatever note is open before opening the next one, can get
            # back to the results.
            element("close", tag="button", className="close-icon", rect=rect(560, 8, 32, 32)),
            element("footer", className="bottom-container", rect=rect(0, 700, 600, 40)),
            element("date", className="date", text="09-27 四川", rect=rect(0, 700, 200, 19)),
            element("script", tag="script", className="None",
                    text="window.__INITIAL_STATE__={", rect=rect(0, 0, 0, 0)),
        ],
    }
    texts = {
        "body": body,
        "script": state_blob(note, title=title, body=body, updated_ms=updated_ms,
                            published_ms=published_ms, kind="video" if video else "normal"),
    }
    return page, texts


def card_elements(index: int, note: str, title: str, time_text: str) -> list[dict]:
    href = (
        f"https://www.xiaohongshu.com{SEARCH_PATH}/{note}"
        f"?xsec_token=tok&xsec_source=pc_search"
    )
    y = 400 + index * 320
    return [
        element(f"card{index}", tag="section", className="note-item", text=f"{title} 冷三岁 {time_text} 91",
                rect=rect(960, y, 230, 300)),
        element(f"cover{index}", className="cover mask ld", href=href, rect=rect(960, y, 230, 300)),
        element(f"title{index}", className="title", href=href, text=title, rect=rect(960, y + 260, 230, 20)),
        element(f"time{index}", className="time", text=time_text, rect=rect(1080, y + 270, 60, 19)),
    ]


def channel_elements(state: str) -> list[dict]:
    # The row spells "no filter" as 全部 while the panel spells it 不限, and the
    # two really are one state — a fake that did not know that would report no
    # active channel at all.
    current = "全部" if state in ("全部", "不限") else state
    return [
        element(f"chan:{name}", className=f"channel{' active' if name == current else ''}",
                text=name, rect=rect(260 + index * 64, 88, 60, 40))
        for index, name in enumerate(CHANNELS)
    ]


def panel_elements(active: dict[str, str]) -> list[dict]:
    elements = [element("panel", className="filter-panel", rect=rect(900, 100, 620, 560))]
    for row, (name, options) in enumerate(PANEL.items()):
        top = 120 + row * 90
        # Every option must sit inside its group and tag container: the reader
        # scopes by rectangle, so a container narrower than its own row of
        # options silently loses the right-hand ones.
        elements.append(element(f"group:{name}", className="filters", text=name,
                                rect=rect(920, top, 560, 70)))
        elements.append(element(f"label:{name}", tag="span", text=name,
                                rect=rect(920, top, 200, 19)))
        elements.append(element(f"tcon:{name}", className="tag-container", rect=rect(920, top + 25, 560, 40)))
        for index, option in enumerate(options):
            is_active = active.get(name) == option
            elements.append(
                element(f"opt:{name}:{option}", className=f"tags{' active' if is_active else ''}",
                        text=option, rect=rect(920 + index * 100, top + 25, 96, 40))
            )
            # The site renders every option twice at the same rectangle.
            elements.append(
                element(f"opt2:{name}:{option}", className=f"tags{' active' if is_active else ''}",
                        text=option, rect=rect(920 + index * 100, top + 25, 96, 40))
            )
    return elements


class FakeSite:
    """A chrome-agent stand-in with just enough of the site's behaviour."""

    def __init__(self, *, keywords: dict[str, list[tuple[str, str, str]]],
                 notes: dict[str, tuple[dict, dict]], tmp_path: Path) -> None:
        self.keyword_cards = keywords          # keyword -> [(note, title, time text)]
        self.notes = notes                     # note -> (page, texts)
        self.tmp = tmp_path
        self.url = ""
        self.search_url = ""
        self.keyword = ""
        self.tabs_seen: set[int] = set()
        self.panel_open = False
        self.active = dict(DEFAULTS)
        self.images: list[str] = [IMAGE_A, IMAGE_B]
        self.calls: list[tuple] = []
        self.download_count = 0

    # --- the CLI surface ---

    def __call__(self, *args: str) -> dict:
        self.calls.append(args)
        self.note_tab(args)
        verb = args[:2]
        if verb == ("tabs", "navigate"):
            # (`tabs navigate <tab-id> <url>` — the URL is the fourth word, not
            # the third; passing the tab id here is what made every search look
            # empty and spin out the render timeout.)
            self.navigate(args[3])
            return {"navigated": True}
        if verb == ("tabs", "list"):
            # `collect.py` records the tab a collection ran against, so a fake
            # that answers nothing here fails the write of downloads.json.
            return {"tabs": [
                {"id": tab, "url": self.url, "title": "川西秋色 - 小红书"}
                for tab in sorted(self.tabs_seen or {1})
            ]}
        if verb == ("page", "snapshot"):
            return self.snapshot()
        if verb == ("page", "click"):
            self.click(args[args.index("--ref") + 1])
            return {"success": True, "clicked": True}
        if verb == ("page", "text"):
            return self.text(args[args.index("--ref") + 1])
        if verb == ("page", "images"):
            # How a picture is exposed depends on the note: a carousel slide is an
            # `<img>`, and a video note's cover frame is a CSS background on the
            # player. The fake reports it the way the real page does, because that
            # field is what tells a poster from a gallery.
            source = "background-image" if self.poster_note() else "img"
            return {"images": [
                {"src": url, "width": 1440, "height": 2400, "source": source}
                for url in self.images
            ]}
        if verb == ("page", "download-images"):
            # One entry per whitelisted URL: `--url` is repeatable, and a fake
            # that answered only the first would hide every picture but one.
            urls = [args[index + 1] for index, word in enumerate(args) if word == "--url"]
            return {"downloads": [
                {"url": url, "state": "complete", "filename": str(self.download(url))}
                for url in urls
            ]}
        return {"success": True}

    def methods(self) -> list[str]:
        return [args[0] + "." + args[1] for args in self.calls]

    def poster_note(self) -> bool:
        """Whether the note on screen is a video note — a player, no carousel.

        Same evidence the script reads off the snapshot, so the fake cannot
        disagree with it about which kind of note is open.
        """
        entry = self.notes.get(runtime.note_id(self.url) or "")
        classes = {
            name
            for element_ in (entry[0]["elements"] if entry else [])
            for name in (element_.get("className") or "").split()
        }
        return "video-player-media" in classes and "xhs-slider-container" not in classes

    def note_tab(self, args: tuple) -> None:
        """Remember which tab the caller is driving, for `tabs list`."""
        if "--tab-id" in args:
            self.tabs_seen.add(int(args[args.index("--tab-id") + 1]))
        elif args[0] == "tabs" and len(args) > 2 and args[1] != "list":
            self.tabs_seen.add(int(args[2]))

    # --- the site ---

    def navigate(self, url: str) -> None:
        self.url = url
        if SEARCH_PATH + "?" in url:
            self.search_url = url
            self.keyword = parse_qs(urlparse(url).query).get("keyword", [""])[0]
            # A fresh search starts from the site's defaults.
            self.panel_open, self.active = False, dict(DEFAULTS)

    def click(self, ref: str) -> None:
        if ref == "close":
            # Closing the note overlay puts the results page back on screen —
            # which is what a batch run does between two notes.
            self.url = self.search_url
        elif ref == "filter-btn":
            self.panel_open = True
        elif ref.startswith("chan:"):
            self.active["笔记类型"] = ref.split(":", 1)[1]
        elif ref.startswith("opt:") or ref.startswith("opt2:"):
            _, dimension, option = ref.split(":", 2)
            self.active[dimension] = option

    def snapshot(self) -> dict:
        note = runtime.note_id(self.url)
        if note and note in self.notes:
            return self.notes[note][0]
        elements = channel_elements(self.active["笔记类型"])
        elements.append(element("filter-btn", className="filter", text="筛选",
                                rect=rect(1324, 88, 84, 40)))
        if self.panel_open:
            elements.extend(panel_elements(self.active))
        for index, (note_id, title, time_text) in enumerate(
            self.keyword_cards.get(self.keyword, [])
        ):
            elements.extend(card_elements(index, note_id, title, time_text))
        return {"url": self.url, "elements": elements}

    def text(self, ref: str) -> dict:
        note = runtime.note_id(self.url)
        texts = self.notes.get(note, ({}, {}))[1] if note else {}
        value = texts.get(ref, "")
        return {"text": value, "length": len(value), "truncated": False}

    def download(self, url: str) -> Path:
        self.download_count += 1
        path = self.tmp / f"download-{self.download_count}.webp"
        path.write_bytes(b"image")
        return path


@pytest.fixture
def site(tmp_path) -> FakeSite:
    return FakeSite(
        keywords={
            "川西秋色": [(NOTE_A, "川西赏秋时间表", "09-27"),
                        (NOTE_B, "川西晚秋｜雪山彩林", "4天前")],
            "稻城亚丁 秋": [(NOTE_B, "川西晚秋｜雪山彩林", "4天前"),
                          (NOTE_C, "稻城亚丁怎么走", "10-01")],
        },
        notes={
            NOTE_A: note_page(NOTE_A, title="川西赏秋时间表", body="10月中下旬开始，川西进入最佳观赏期。"),
            NOTE_B: note_page(NOTE_B, title="川西晚秋｜雪山彩林", body="彩林最好的时候是十月中。",
                              updated_ms=1785590297000, published_ms=1784000000000),
            NOTE_C: note_page(NOTE_C, title="稻城亚丁怎么走", body="从成都出发走雅康高速。"),
        },
        tmp_path=tmp_path,
    )


def run_cli(monkeypatch, site, argv: list[str]) -> int:
    patch_browser(monkeypatch, site)
    monkeypatch.setattr("sys.argv", ["discover.py", *argv])
    return discover.main()


def data_args(tmp_path, name: str = "xhs-e2e") -> list[str]:
    return ["--data-root", str(tmp_path / name)]


def database_of(tmp_path, name: str = "xhs-e2e") -> Path:
    return runtime.db_path(runtime.data_root(str(tmp_path / name)))

