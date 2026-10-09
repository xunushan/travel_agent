# 完整流程

搜一次 → 看一眼清单 → 下你要的几篇。第一步不打开任何笔记，第二步不开已经完整的笔记。
出问题见 [pitfalls.md](pitfalls.md)；筛选的机制与实测记录见仓库 `docs/xhs-筛选机制与实测.md`。

```bash
SITE="$HOME/.claude/skills/xhs-downloader"   # 源码仓库里换成 .../travel_agent/skills/xhs-downloader
```

## 0. 前置

```bash
chrome-agent ensure --launch-if-missing --wait-for-extension --timeout 30 --json
chrome-agent tabs list --json                # 挑一个已登录小红书的标签页
chrome-agent tabs activate <tab-id> --json   # 采评论必须在前台，见 pitfalls 2
```

前置只需做一次；但 tab 可能被手动关掉——隔了一段时间再跑，先 `tabs list`（或一次
`page snapshot`）确认它还活着。

数据根默认 `~/Documents/travel_agent`，可用 `--data-root` 或 `$XHS_DATA_ROOT` 覆盖：
索引 `<root>/xhs.db`，笔记 `<root>/notes/<noteId>_<标题>/`。**两个入口必须用同一个根**，
否则 download 认不出 discover 标过"在库"的笔记。

## 1. 搜：discover.py

### 1.1 看页面真实有哪些筛选选项

```bash
python "$SITE/scripts/discover.py" --tab-id <tab-id> --list-filters
```

输出形如 `笔记类型(常驻行): 全部* | 图文 | 视频 | 用户`，接着是弹层里各组
（排序依据 / 发布时间 / 搜索范围 / 位置距离）的选项，`*` 标当前生效项。

判据：至少读到一组弹层选项。读不到会**显式报错**（不会悄悄退化成"无筛选"）——那说明页面
结构变了，去改 `locators.yaml` 里 `search.filter_*` 那一段。同一个 tab 上看过一次即可。

**这个 tab 必须先停在搜索结果页**（`/search_result/?keyword=…`）。筛选行与"筛选"入口只在
结果页存在：停在首页或笔记页时会报找不到"筛选"入口——那不是页面改版，是走错了页面。
`discover.py run` 自己会导航到结果页，所以"跑完 discover 接着 filters"总是安全的；
手工开的 tab 要先自己搜一次。

### 1.2 搜一轮

```bash
python "$SITE/scripts/discover.py" --tab-id <tab-id> \
  --keyword "扎尕那晨雾" --filters "排序依据=最新" --limit 5 > candidates.json
```

- `--keyword` 一次一个词。
- `--filters` 是**一个参数、分号分隔**，每项是 `维度=选项`，维度可省（`"半年内"`）；
  同维度匹配到多个候选时**报错而不是猜**。不传就是不加筛选。
- `--limit` 是最多返回多少条候选（默认 10）；页面还有更多时输出里 `capped: true`。
- stdout 是清单 JSON，进度与摘要走 stderr，所以 `> candidates.json` 拿到的就是干净 JSON。

脚本整页导航 → 轮询等结果渲染 → **逐项点筛选并验证生效** → 抓卡片（`limit + 1` 张，
用来判断有没有截断）。**不打开任何笔记。**

### 1.3 读清单

```json
{
  "keyword": "扎尕那晨雾",
  "applied": [{"dimension": "排序依据", "option": "最新", "source": "panel"}],
  "capped": false,
  "summary": {"candidates": 5, "inLibrary": 1, "new": 4},
  "candidates": [
    {"rank": 1, "noteId": "6ab…", "url": "https://…/explore/6ab…?xsec_token=…",
     "title": "扎尕那的晨雾要这样等", "publishedAt": "2025-10-02T…",
     "updatedAt": null, "type": null, "stats": null,
     "inLibrary": false, "noteDir": null}
  ]
}
```

每条候选：

| 字段 | 从哪来 |
|---|---|
| `rank` / `noteId` / `url` | 卡片本身。**`url` 原样保留**，里面的 `xsec_token` 是访问上下文 |
| `title` | 卡片标题；已入库的用 `note.json` 里的（更准） |
| `publishedAt` | 由 noteId 前 8 位十六进制解出，**不用开页面**；已入库的用 `note.json` 里的 |
| `updatedAt` / `type` / `stats` | **只有已入库的才有**——它们只在笔记页里，discover 不开笔记 |
| `inLibrary` / `noteDir` | 盘上有没有这篇、在哪 |

判据：`applied` 列出你传的每一项及其来源（`channel` = 常驻行，`panel` = 弹层）；
`summary.new` 是还没下过的篇数。**清单就是调用方的台账**——哪几篇值得下由调用方决定，
工具不替你记。

### 1.4 想读正文再决定？

discover 不提供正文。要判断内容而卡片标题不够时，两条路：

- 直接下 `--part note`（便宜，一页一次快照），读 `<noteDir>/note.json` 的 `content` 全文；
- 封面常常是信息本体（时间表、路线图、机位清单），下 `--part cover` 再 `Read` 那张图。
  **只在文字不足时读图**——一张图约 1–1.5k token。

### 1.5 筛选怎么挑

- **时效性内容**（花期、路况、开放时间）挑「发布时间」的 `一周内`/`半年内`；经典路线、攻略
  这类慢变内容用「不限」反而更全。
- **要图集/路线图/时间表** → 笔记类型挑 `图文`；要**路况、实地视频** → `视频`。
- **排序按目的**：看最新动态用「最新」，想让高赞经典攻略排前面用「最多点赞」/「最多评论」，
  没有明确意图就用「综合」。
- **一轮一个词，别贪多**：十来条足以判断该不该换词。候选太多说明词太宽，换词比调大
  `--limit` 更省时间；这个词有没有被看全，看输出里的 `capped` 与 `truncatedSnapshot`。

## 2. 下：download.py

```bash
python "$SITE/scripts/download.py" --tab-id <tab-id> --from candidates.json --limit 5
```

- **`--from` 就是上一步的清单**，按里面的顺序（或 `rank`）处理前 `--limit` 条。
- 也可以直接点名：`--note-id <id>,<id>`（一个参数、逗号分隔）或 `--url "<详情页链接>"`。
  三种入口可以混用，同一篇只处理一次。**`--note-id` 只给 id 时，若索引里也没存过链接，
  会明确报"先 discover 再下载"而不去拼 URL**——没有 `xsec_token` 的链接打不开。
- `--part` 默认 `all`，可选 `note`/`cover`/`image`/`video`/`comment` 任意组合：
  `--part note,image`。**`comment` 无论怎么写都最后跑**（滚动评论会把笔记容器挤出快照）。
- `--interval` 默认 15 秒，只在真的打开了页面时等；走跳过路径的条目不占它。

### 2.1 输出

stderr 每条一行，`--json` 时 stdout 给一份报告：

```text
[1/5] OK     new     6ab…  扎尕那的晨雾要这样等  note=已下载 cover=已下载 image=已下载 …
[2/5] OK     skip    6ac…  扎尕那  全            note=已存在 cover=已存在 image=已存在   请求的件盘上都在，未开页面
```

`action` 是这五种之一：

| action | 意思 | 开了页面吗 |
|---|---|---|
| `new` | 从没存过，请求的件都取了一遍 | 是 |
| `fill` | 存过、没变，只补了缺的那些件 | 是 |
| `refresh` | 存过但**变了**，请求的件全部重下，不再是当前版本的文件被删掉 | 是 |
| `check` | 用 `--check-update` 开了页面核验，结果没变 | 是 |
| `skip` | 请求的件盘上都在，什么都没做 | **否** |

`status` 是 `OK` / `FAILED`。**有件没下成就是 `FAILED` 并非 0 退出**，`reason` 里点名是哪件、
错在哪；`warnings` 逐条列出异常（缺图、正文被截断、评论可能不全）。

`parts` 里每个件四态：

| 值 | 意思 |
|---|---|
| `已下载` | 这一轮写下来的 |
| `已存在` | 盘上本来就有、这轮没动它 |
| `无此件` | **页面对这个件给了否定答案**：视频笔记没有图集，图文笔记没有视频 |
| `未完成` | 该有但没有（失败，或还没取过） |

**`note` 只要开了页面就一定是 `已下载`**——打开页面的第一件事就是重读笔记本身（标题定目录名，
`lastUpdateTime` 定要不要刷新），所以它总在这一轮被重写。

**`无此件` 与"没下成"必须分开看**：前者是笔记的性质，后者是这一轮的故障。区别在
`downloads.json` 的 `examined`（页面对这个类型给过答案）加上该类型的条目数（有没有那个件）：
**说成"无此件"会让一次失败永远不再重试**，所以"问过"和"没下成"两个条件缺一不可。

判据：
- **原样重跑一遍应当全部 `skip`、一个页面都不开**——这是"避免不必要的重复下载"的验收标准。
- 每条的 `parts` 与请求的 `--part` 完全对应，没有多出没要的件。
- 结束时 `<root>/notes/<noteId>_<标题>/` 里，请求了哪个件就有哪个件对应的文件。

### 2.2 变了怎么办

打开笔记时会读它的 `lastUpdateTime` 和正文，和索引里存的比对；只要有一个不同，这篇就是
`refresh`：**本轮请求的每个件都重下**，然后删掉 `downloads.json` 里不再属于这一版的文件
（删的时候跳过本轮下失败的类型——旧文件可能是那张图唯一的副本）。

**只有打开页面才知道变没变**，所以件都在的笔记默认整篇跳过。要它核验一次就传
`--check-update`：件齐的笔记也会开页面比对，没变就是 `check`，变了就是 `refresh`。

`--force` 是不问、全下（每张图都会重下一遍），与 `--check-update` 正交：
`--check-update` 是"看一眼有没有变"，`--force` 是"不用看，重来"。

## 一条笔记目录里有什么

```text
<root>/notes/<noteId>_<标题>/
  note.json       # 笔记本身：标题/作者/时间(含 updatedAt)/互动数/标签/正文（给智能体直读）
  cover.webp      # 封面，放在目录下而不是 images/ 里——它是独立的件
  images/         # 图集（视频笔记没有这个目录）
  videos/         # 视频（图文笔记没有这个目录）
  comments.json   # 评论线程 + 完整性字段（补采只重写这个文件）
  downloads.json  # 每个媒体 URL、它的 state、落盘文件名、以及 examined
```

**两个媒体目录各自只在真的有那种件时才存在。** 视频笔记没有轮播，它唯一那张图是播放器上的
封面帧（CSS `background-image`），封面由 `cover` 件负责——所以视频笔记的 `images/` 是空概念。
`images/` 里少一个文件、`videos/` 少一个目录，都比多一个假件好。

分工是硬规则：`note.json` 只放内容，不放"怎么采的"（tab、URL 清单、下载状态都在
`downloads.json`），也不放评论（在 `comments.json`）。结构合同见 `schemas/*.schema.json`。

**"我有没有这个件"只问文件**：`note.json` 在不在、`cover.*` 在不在、`images/` 里有没有图、
`comments.json` 有没有 `possiblyIncomplete`。索引里没有任何"已下载"标志位——文件不会跟磁盘
互相矛盾。

`downloads.json` 里的 `examined` 记的是"页面对这个类型给了答案"：图集/视频的读取真看到了
东西（或页面明确说没有）才记。没有它，"这篇没有视频"和"没人找过视频"分不出来，缺图就会被
当成正常。
