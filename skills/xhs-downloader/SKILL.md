---
name: xhs-downloader
description: 小红书（xiaohongshu.com）的搜索与下载工具：搜一个关键词、施加筛选，拿到候选清单；再按清单把笔记的正文/封面/图片/视频/评论树下载到本地，并记录下载状态。用在站内搜索发现、笔记详情抓取、媒体下载与批量采集任务上。
---

# 小红书搜索与下载

两个入口，两件事：

- **`discover.py`** — 搜一个关键词、施加筛选，输出候选清单。**不打开任何笔记。**
- **`download.py`** — 把指定笔记的指定内容件下载到本地，并维护下载状态。
  **盘上已有的不再下，变了就整篇刷新。**

**这个工具不做判断。** 搜什么词、施加什么筛选、哪些笔记值得留、什么时候停，都是调用方的事；
它只负责把页面上的事实读准、把文件落对、把状态维护对。没有"问题台账"，也不认识"keep/drop"。

## 依赖与前置

- `chrome-agent` 命令必须可用（CLI + daemon + 已加载扩展 + 一个已登录小红书的标签页）。
  本站不 import 它的代码，只调它的 CLI 与 `--json` 输出。
- 脚本用绝对路径运行，不依赖当前工作目录：

```bash
SITE="$HOME/.claude/skills/xhs-downloader"   # 源码仓库里换成 .../travel_agent/skills/xhs-downloader
chrome-agent tabs list --json                # 挑一个已登录小红书的标签页，记下 tab-id
chrome-agent tabs activate <tab-id> --json   # 采评论必须在前台
```

## 按需加载

| 需要什么 | 读哪份 |
|---|---|
| 一步步跑完一次（第一次照做） | [references/flow.md](references/flow.md) |
| 报错、行为反常、结果不对 | [references/pitfalls.md](references/pitfalls.md) |
| 改脚本、改选择器、改产出结构 | [references/files.md](references/files.md) |

筛选的机制与实测记录不在 skill 里（仓库 `docs/xhs-筛选机制与实测.md`）；skill 只讲怎么操作。

## 命令

```bash
# ① 看页面真实有哪些筛选选项（不写死词表，同一 tab 看一次即可）
python "$SITE/scripts/discover.py" --tab-id <tab> --list-filters

# ② 搜一个关键词 → 候选清单 JSON（stdout；进度在 stderr）
python "$SITE/scripts/discover.py" --tab-id <tab> \
  --keyword "扎尕那晨雾" --filters "排序依据=最新" --limit 5 > candidates.json

# ③ 下载清单里的前若干篇（--from 就是上一步的产物）
python "$SITE/scripts/download.py" --tab-id <tab> --from candidates.json --part all --limit 5

# 也可以直接点名，或只下某一类内容件
python "$SITE/scripts/download.py" --tab-id <tab> --note-id <id>,<id> --part note
python "$SITE/scripts/download.py" --tab-id <tab> --url "<详情页链接>" --part comment
```

- `--part` 共五种：`note` / `cover` / `image` / `video` / `comment`，`all` 是默认。
  每个件都是一份可以独立检查的文件；问"我有没有这个"只问文件。
- `--filters` 是**一个参数、分号分隔**，每项 `维度=选项`，维度可省（`"半年内"`）；
  不传就是不加筛选。有哪些维度只能 `--list-filters` 当场读，没有词表。
- 数据默认落在 `~/Documents/travel_agent`（`--data-root` 或 `$XHS_DATA_ROOT` 可指定）：
  索引 `<root>/xhs.db`，笔记 `<root>/notes/<noteId>_<标题>/`。**两个入口必须用同一个根。**

## 下载的两条规则

状态就在文件里，没有任何"已下载"标志位——所以标志位和磁盘不可能互相矛盾。

1. **有就不下，没有就下。** 请求的每个件先在盘上查：在、且完整，就不再取。图片和视频
   逐条查 `downloads.json`，所以断在一张图上的一轮，下一轮只补那一张。
2. **变了就整篇刷新。** 打开笔记时会读它的 `lastUpdateTime` 和正文；只要有一个说明它
   不是存下来的那一版，**盘上已有的件连同本轮请求的件**都会重下，不再属于当前版本的文件
   会被删掉。一篇笔记要么是视频要么是图集，不会都有，所以"整篇"就是那几个文件。
   **评论不算笔记的内容**——它是评论区的、自己会变，所以刷新不动它；要新的一轮评论就
   请求它（盘上没有时）或 `--part comment --force`。

**只有打开页面才知道变没变。** 所以"请求的件盘上都在"的笔记整篇跳过、连页面都不开；
`--check-update` 是调用方要求"即使件都在也开页面核验一次"的方式。
`--force` 只管本轮请求的件（不问盘上有没有、别的件不动），所以 `--part comment --force`
是"只重采评论"。两者都不需要时不要传：`--force` 会让请求的每张图都重下一次。

## 硬性不变量

1. **详情链接原样用**：href 里的 `xsec_token` 是访问上下文，禁止从笔记 ID 拼接、缩短或删除；
   落盘 URL 里的 token 会过期，过期后按 `keyword` 回搜索页重新发现。
2. **筛选只能点控件**：URL 上拼 `sort=`/`noteType=` 一律无效；点完必须验证页面把该项标成
   生效项，没生效就中止——否则会拿回一批没筛过的结果。
3. **读评论必须前台标签页**（`tabs activate`），后台标签页会被降频，滚到底也不加载。
4. **下载成功 = `state=complete` 且 `filename` 非空**，发现 URL 不等于下载完成。
   **下失败的件绝不能被说成"这篇没有这个件"**：`downloads.json` 的 `examined` 记录的是
   "页面对这个类型给了答案"，问过不等于答过。反之亦然——页面的否定回答（视频笔记没有图集、
   图文笔记没有视频）是笔记的事实，报成 `无此件`，并且**不能落任何文件**：视频笔记的
   `images/` 是空概念，它那张图是封面帧，由 `cover` 件负责。
5. **全文落盘、只回摘要**：正文全文只写 `note.json`，不打到 stdout；`download.py` 的 stdout
   只在 `--json` 时给一份报告，进度一律走 stderr。
6. **每个件自取快照**，可单独反复跑；同一访问内手工连跑多个件时 `comment` 必须最后
   （滚动会把轮播挤出快照窗口）。`ref` 只属于当次快照，不许跨快照复用。
7. **笔记目录命名一次，永不改名**：`downloads.json` 里存的是绝对路径，改目录名会让整篇
   的图片看起来"文件都不在了"，下一轮把整个图集重下一遍。
