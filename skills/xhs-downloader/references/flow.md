# 完整流程

按顺序照跑即可。每一步都给出命令与判据。定搜索词/筛选的判断要点见
[filters.md](filters.md)，选笔记的判断要点见 [selection.md](selection.md)。

```bash
SITE="$HOME/.claude/skills/xhs-downloader"   # 源码仓库里换成 .../travel_agent/skills/xhs-downloader
```

## 0. 前置

```bash
chrome-agent ensure --launch-if-missing --wait-for-extension --timeout 30 --json
chrome-agent tabs list --json                # 挑一个已登录小红书的标签页
chrome-agent tabs claim <tab-id> --json
chrome-agent tabs activate <tab-id> --json   # 采评论必须在前台，见 pitfalls 2.2
```

数据根默认 `~/Documents/travel_agent`，可用 `--data-root` 或 `$XHS_DATA_ROOT` 覆盖；
库在 `<root>/xhs.db`，笔记在 `<root>/notes/<noteId>/`。阶段一、二必须用同一个根，
否则第二阶段找不到第一阶段建的索引。

## 阶段一：搜 → 筛 → 定清单

### 1.1 看页面真实有哪些筛选选项

```bash
python "$SITE/scripts/search.py" filters --tab-id <tab-id>
```

输出形如 `笔记类型(常驻行): 全部* | 图文 | 视频 | 用户`，接着是弹层里各组
（排序依据 / 发布时间 / 搜索范围）的选项，`*` 标当前生效项。

判据：至少读到一组弹层选项。读不到会**显式报错**（不会悄悄退化成「无筛选」）——
那说明页面结构变了，去 `references/filters.md` 与 `locators.yaml: search.filters.*`。
同一个 tab 上看过一次即可，不必每轮都看。

**这个 tab 必须先停在搜索结果页**（`/search_result/?keyword=…`）。筛选行与「筛选」入口只在
结果页存在：停在首页或笔记页时，脚本会报找不到「筛选」入口——那不是页面改版，是走错了页面。
`search.py run` 自己会导航到结果页，所以「run 之后接着 filters」总是安全的；手工开的 tab
要先自己搜一次。

### 1.2 一轮搜索

```bash
python "$SITE/scripts/search.py" run --tab-id <tab-id> \
  --keywords "川西秋色;稻城亚丁 秋" \
  --filters "排序依据=最新;半年内" \
  --limit 10 --screen-limit 10 --excerpt 150
```

- `--keywords` / `--filters` 都是**一个参数、分号分隔**，不重复传。`--filters` 每项是
  `维度=选项`，维度可省（`"半年内"`）；同维度匹配到多个候选时**报错而不是猜**。
- 脚本对每个搜索词：整页导航 → 轮询等结果渲染（实测 >14s，默认超时 45s）→ **逐项点筛选并
  验证生效** → 再抓卡片。
- 然后按 `--screen-limit` **逐词**打开候选，抽 SSR 状态 + 正文 + 一张封面，落盘并入库。
  两个配额都是**每个搜索词各算一份**：`--limit 10 --screen-limit 10` 配两个关键词，
  是每个词各取 10 张卡片、各打开 10 篇。输出按词分成小节，每节标出这个词自己
  取了多少、开了多少、剩下多少没打开。
  **不读评论、不下图集与视频**——这是阶段一便宜的根本原因。
- 库里已有的候选直接列出来不重开（`[已筛过:keep|drop]` / `[已下载]`），所以同一轮重跑很便宜。

输出是一张紧凑摘要表，每条 2–4 行：

```text
=== run 7 · 川西秋色;稻城亚丁 秋 · 排序依据=最新；半年内 · 20 候选 ===
 1 * 6aba9036000000001c00d596  川西赏秋时间表！国庆之后，我就冲…  [冷三岁] 09-28 2026-09-28 91/32/18/0 图文
     川西、秋色、自驾 | 10月中下旬开始，川西进入最佳观赏期。先说结论…
     cover: /Users/…/notes/6aba9036000000001c00d596/images/cover.webp
 2   6abfda1f000000001c00e7a3  川西晚秋🏔️｜走进雪山彩林里🍂  [飞奔的熊大] 4天前 2026-10-03 48/6/2/0 图文  [已筛过:drop]
     …
=== 20 张卡片 → 打开 14 · 库里已有 6(keep 4/drop 2/待定 0) · 正文与封面在 /Users/…/notes ===
已施加筛选：排序依据=最新(panel)、半年内(panel)
```

每行字段：`序号 保留标记 完整 noteId 标题 [作者] 卡片时间 发布时间 赞/藏/评/享 类型 [索引状态]`，
第二行是标签 + 正文摘要，第三行是封面**本地路径**，异常另起 `!` 一行。
`*` = 本轮已选 keep。

判据：摘要表非空且每条一行；每行都有 noteId（全量打印，供下一条命令直接用）；
`已施加筛选` 列出每一项及其来源（`channel` = 常驻行，`panel` = 弹层）。

### 1.3 读表并做判断

对每条回答 keep 还是 drop，并回答一个整体问题：**够不够做决定，还是该换个搜法**。

- **要看图才判得了**时，Read 摘要里给的封面路径。封面常常就是信息本体（时间表、路线图、
  机位清单），只看正文会漏判；但读图花 token，**只在文字不足时读**。
- **够了就停**：已经能支持判断就不再开新一轮。目标通常是一轮里留下 ≤10 篇。
- 不够就换词或换筛选**再来一轮**（`search.py run` 不带 `--run-id` 即新建一个 run），
  或对同一轮追加候选。

```bash
python "$SITE/scripts/decide.py" keep --run-id 7 --note-id <id>,<id> --reason "有时间表和机位"
python "$SITE/scripts/decide.py" drop --run-id 7 --note-id <id> --reason "只有风景照"
python "$SITE/scripts/decide.py" show --run-id 7 [--decision keep] [--limit 20] [--json]
```

判据：`keep`/`drop` 打印「已记录 N 条」；不属于本 run 的 id 会被单独列出来而不是静默吞掉。
`show` 打印

```text
  1  6aba9036000000001c00d596  keep  川西赏秋时间表！国庆之后…  有时间表和机位
合计 20 候选：待定 14 / keep 5 / drop 1
```

`show --json` 输出 `[{noteId, href, url, decision, rank, reason, title, …}]`，
可直接喂 `batch.py --plan`。

## 阶段二：下载清单里的笔记

```bash
python "$SITE/scripts/batch.py" --tab-id <tab-id> --run-id 7 \
  --data-root ~/Documents/travel_agent \
  --part body,media,comments --dedup skip \
  --download-images --download-media --comment-limit 10 --interval 15
```

- `--run-id` 采这一轮里被标成 keep 的笔记；自备清单时用 `--plan <show --json 的输出>`。
- `--part` 是 `body,media,comments` 的任意组合（默认 `all`）。阶段一已抓过正文，所以
  阶段二通常只需 `--part media,comments`。
- **快路径**：先查库与磁盘。正文没变、文件都在、评论线程上次读完整 → 这一条根本不打开页面。
- `--dedup skip`（默认）= 比对后没变化就停手；`refresh` = 不管有没有变化都跑完；
  `force` = 完全不信索引，重新下载所有图片。判重的口径见 [files.md](files.md#判重)。
- 想先拿一条试手：`--only <noteId>`。`--limit N` 限制总条数（**`--limit 0` 是不限，不是 0 条**）。
- 评论必须前台标签页，且**笔记之间**要留 `--interval`；一条笔记内多张图一起下没问题，
  不要并发拉多条。

判据：每条一行 `[i/n] OK <noteId> <action> <类型> 正文N 评论N 图N 下载N …`，
`action` 是 `skip_dup` / `recollect` / `redownload_media` / `refresh_comments` /
`refresh_stats` 之一；异常条目打印 `FAILED` + 原因并以非 0 退出。
**原样重跑一遍应当是全部 `skip_dup`、下载数 0、不产生任何新文件。**

## 一条笔记目录里有什么

```text
<root>/notes/<noteId>/
  note.json       # 笔记本身：标题/作者/时间(含 updatedAt)/互动数/标签/正文（给智能体直读）
  comments.json   # 评论线程 + 完整性字段（补采只重写这个文件）
  downloads.json  # 发现与下载的验证记录：URL、state、落盘文件名
  images/  videos/
```

三个文件的分工是硬规则：`note.json` 只放内容，不放「怎么采的」（tab、URL 清单、下载状态都在
`downloads.json`），也不放评论（在 `comments.json`）。结构合同见 `schemas/*.schema.json`。

## 单条采集（不用清单时）

```bash
python "$SITE/scripts/collect.py" --tab-id <tab-id> --part body,media \
  --download-images --prefix <note-id> --output-dir <root>/notes/<note-id>
```

`collect.py` 是阶段二的下层：`--part body|media|comments|cover|all` 各写各的文件、
各取一次快照，因此可单独反复调用、顺序无关（手工连跑多个 part 时 `comments` 仍须最后）。
`--comments-only` 是 `--part comments` 的别名。
