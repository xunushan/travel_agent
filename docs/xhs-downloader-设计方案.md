# xhs-downloader 设计方案

## 1. 定位

**一个由智能体驱动的小红书下载工具。** 调用方说清楚三件事——**要不要搜索**（搜的话：关键词、筛选项、top-k）、**下载对象**（搜索发现的候选，或直接指定的笔记）、**下载哪些内容件**——智能体据此选择运行哪个代码文件、传什么参数。

- 工具只做机械执行：发现候选、下载内容件、记账（每个内容件下了没有、要不要重下）。**不做任何内容判断**——选词、筛选、留哪篇、什么时候停，都是调用方的事。
- 工具不是零 token：本质是智能体在操作代码完成下载，读输出、决定跑哪条命令都消耗 token，所以命令、参数、输出都保持简单结构化。
- 不自己开浏览器，通过 `chrome-agent` CLI 操作已登录小红书的 Chrome 标签页（`--tab-id` 指定）。

---

## 2. 功能设计

### 2.1 两个代码文件：discover 与 download

| 代码 | 干什么 | 输入 | 输出 |
| --- | --- | --- | --- |
| **discover** | 搜索并发现候选（**一次一个关键词**） | 关键词、筛选项（可选）、top-k | 候选清单 JSON：note_id / url / 标题 / 互动数 / publishedAt、updatedAt / 类型 / 在库还是新发现 |
| **download** | 对指定笔记下载指定内容件 | 笔记（链接或 noteId 列表——可来自 discover 的候选，也可直接指定）＋ 内容件组合 | 每篇每个内容件的成功/失败与路径 |

**内容件**：`note`（正文全文＋元数据）、`cover`（封面图）、`image`（图集）、`video`（视频）、`comment`（评论树）。相互独立，任意组合。

**典型用法是两者串联**：discover 出候选后，紧接着对候选跑 download——比如调用方说"搜索'甘南小环线'，top-10，下载 note 和 cover"，智能体就跑 discover 再对候选跑 download（note,cover）。也可以只对给定笔记跑 download，比如"这几篇补 image、video、comment"。

筛选项的可选值不写死词表，运行时从页面上读（`discover.py --list-filters`），调用方据此表达筛选意图；选项失效时显式报错，不悄悄退化成无筛选。页面上的维度与实测记录见 [`docs/xhs-筛选机制与实测.md`](xhs-筛选机制与实测.md)。

### 2.2 落盘结构

`--data-root` 指定本次下载数据的存放路径：

```javascript
<root>/
  xhs.db                              # notes 表（见 2.3）
  notes/<note_id>_<title>/            # 目录名 = noteId_标题
    note.json                         # note：标题/作者/时间/互动数/标签/正文全文
    cover.webp                        # cover：封面图，直接放笔记目录下，不进 images/
    images/                           # image：图集
    videos/                           # video：视频
    comments.json                     # comment：评论树 + 完整性字段
    downloads.json                    # image/video 的逐条 URL 下载状态（补缺、重试的依据）
```

- 目录名中的标题做文件名安全处理（去非法字符、截断）；noteId 前缀保证唯一，不怕重名。
- 各内容件自包含、互不依赖。封面就是轮播第一张图，cover.webp 与 images/ 独立存放（可能重复存一份，换取两个内容件互不牵扯）。

### 2.3 状态账：盘面为准，notes 表只记版本键

**"哪个内容件下过没有"一律看盘，不设标志位**——盘上的文件就是唯一事实，不存在表和盘打架的问题：

| 内容件 | "已下载"的盘面判据 |
| --- | --- |
| note | `note.json` 在且完整 |
| cover | `cover.webp` 在 |
| image / video | `downloads.json` 里没有 failed 条目，且对应文件都在 |
| comment | `comments.json` 在且完整（possiblyIncomplete 为假） |

库里的 notes 表只记**索引与版本键**，回答"这篇见没见过、内容是什么版本"：

```sql
CREATE TABLE notes (
    note_id      TEXT PRIMARY KEY,   -- 24 位十六进制，从链接解出
    url          TEXT,               -- 带 xsec_token 的详情页链接，重放用
    title        TEXT,
    note_dir     TEXT,               -- 笔记目录
    updated_at   TEXT,               -- 编辑时间（SSR lastUpdateTime）
    content_hash TEXT                -- 标题+正文+标签 的 sha256
);
```

### 2.4 已入库笔记的再下载行为

规则只有两条：

1. **有就不下，没有就下**：调用方请求的每个内容件，盘上有且完整 → 跳过；没有或不完整 → 下载（图片/视频按 downloads.json 只补缺的那几条）。
2. **变了就全刷新**：打开页面时发现 `updated_at`/`content_hash` 变了 → 重写 note.json，并把这篇**之前下载过的所有内容件全部重新下载**；不再属于当前版本的旧文件清理掉——目录里永远只保留当前版本。

什么时候会打开页面（也只有打开页面才能发现"变了"）：**有缺口件要下载时顺带核验**；或**调用方显式要求"检查更新"**。请求的件盘上都有、且没让检查更新 → 整篇跳过，连页面都不开。

| 例子 | 行为 |
| --- | --- |
| A 已下过 note+cover+image；又要"下载 A 的 note、image" | 盘上都有 → 整篇跳过，不开页面，返回已有路径 |
| A 只下过 note+cover；要"补 video、comment" | 有缺口件 → 打开 A 顺带核验：没变 → 只下 video 和 comment |
| 补 A 的 video 时打开页面，发现作者 10-08 编辑过、换了几张图 | 变了 → note.json 重写，cover、image 全部重下，换掉旧文件，再下本次请求的 video |
| "检查一下 A 有没有更新" | 打开页面核验：没变 → 什么都不动；变了 → 按规则 2 全刷新 |
| "刷新 A 的评论" | 显式重下指定件：无视盘上已有 comments.json，重新采集覆盖（评论随时在涨，和笔记有没有编辑过无关） |
| 搜"甘南小环线" top-10，其中 3 篇上周搜别的词时已入库 | 在库的 3 篇不重开页面，用盘上 note.json 生成清单条目（标注"在库"），只打开其余 7 篇；要新鲜数据就说"检查更新" |

一句话：**"有就不下"是默认；"变了"或"调用方明确要求"才重下，变了就把整篇刷新到当前版本。**

### 2.5 输出与报错

- discover 末尾打汇总行：候选 n 条 / 在库 x / 新发现 y；
- download 对每篇每个内容件报告成功/失败/跳过（含原因）；
- 一切失败显式报告——**不把「没下成」含糊成「没有」**。