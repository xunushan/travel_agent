# 库里存什么（数据模型）

**一句话：库是索引与台账，不是笔记仓库。** 正文、评论树、下载清单在盘上的
`notes/<noteId>/note.json|comments.json|downloads.json`；库记的是「这条笔记是什么、
我判断过没有、哪些文件在盘上」。所以库可以随时删掉重建（重跑阶段一会把筛选结论丢，
盘上的笔记文件不受影响）。

文件：`<data-root>/xhs.db`，默认 `~/Documents/travel_agent/xhs.db`。
建表见 `scripts/db.py` 的 `SCHEMA`，`SCHEMA_VERSION` 是表结构的版本——**版本不符直接拒绝**
（不做迁移），因为库可以重建，而迁到一半的库不能再信。

## 六张表

| 表 | 一行代表 | 回答的问题 |
|---|---|---|
| `notes` | 一篇笔记 | 见过吗？筛过吗？判断是什么？内容变了吗？ |
| `media` | 一张图 / 一个视频 | 这个文件下过吗？还在盘上吗？ |
| `comments_meta` | 一篇笔记的评论读取情况 | 评论区读全了吗？要不要再读？ |
| `runs` | 一次运行 | 这次搜的是什么词、什么筛选、什么时候跑的 |
| `run_items` | 一次运行 × 一篇笔记 | 这次的候选有哪些？哪条 keep/drop 了、为什么？ |

没有 assets 表——「资产」现在就是 `notes.status` + `media` + 磁盘文件三处的合取，
见下文「去重今天怎么判」。

## notes —— 一篇笔记的当前状态

跨 run 的汇总：不管在哪一轮遇到的，一篇笔记只有一行。

| 列 | 含义 | 写 | 读 |
|---|---|---|---|
| `note_id` PK | 24 位十六进制，从 href 解出 | 阶段一 | 全部 |
| `url` | 笔记链接（可能不带 token） | 阶段一 | — |
| `title`/`author`/`author_id`/`type` | 标题、作者、作者 ID、`normal`/`video` | 阶段一 | 摘要表 |
| `published_at` | 发布时间，来自 SSR 状态 `note.time` | 阶段一 | 摘要表 |
| `updated_at` | 编辑时间，来自 `note.lastUpdateTime` | 阶段一 | **去重的第一判据** |
| `updated_at_source` | `state` 表示这是读到的编辑时间，NULL 表示没有 | 阶段一 | 排错 |
| `tags_json` | 标签数组（JSON） | 阶段一 | 摘要表 |
| `likes`/`collects`/`comment_count`/`shares` | 四项互动数，来自 `interactInfo` | 阶段一 | 摘要表 |
| `content_hash` | 标题+正文+标签+图片 URL 的 sha256 | 阶段一 | 去重第二判据 |
| `content_len` | 正文字符数（**不过滤标签**） | 阶段一 | 短正文判断 |
| `media_count` | 这篇声称有几张图/视频 | 阶段一 | — |
| `status` | 见下方状态机 | 阶段一/二 | 到处 |
| `schema_version` | `note.json` 的形状版本（**不是表结构**） | 阶段一 | `migrate.py` |
| `note_dir` | 这篇在盘上的目录 | 阶段一/二 | 找封面、找文件 |
| `excerpt` | 正文**去掉尾部 tag 段**后的前 600 字；正文只有 tag 时为 NULL | 阶段一/二 | 摘要表、库内复用 |
| `excerpt_kind` | `full`（全文都在）/ `truncated`（截断了）/ `tags-only`（正文只有标签）/ `empty`（正文是空的）/ NULL（这次没看正文） | 阶段一/二 | 判断这条有没有正文 |
| `first_seen_at`/`last_seen_at` | 第一次 / 最近一次出现在搜索结果里 | 阶段一 | — |
| `last_screened_at` | 最近一次被打开初筛 | 阶段一 | — |
| `last_collected_at`/`last_media_at`/`last_comments_at` | 正文 / 媒体 / 评论各分部最近一次完成 | 阶段二 | 判断哪一块还没做 |

两条容易踩的写入语义（`store_note` 的 COALESCE）：

- **`None` 不覆盖已存的值**。一次只读正文的重跑不会把作者、互动数抹掉；只有真读到了
  才写。所以「这列是 NULL」的意思是**从没读到过**，不是「读到了空」。
- **`content` 为 `None` 和 `""` 是两件事**。`None` = 这次没看正文（只跑了媒体或评论），
  于是 `content_hash`/`content_len` 保持 NULL；`""` = 看了，正文确实是空的。

## media —— 文件级资产

| 列 | 含义 |
|---|---|
| `note_id` + `url` PK | `url` 存的是**图片名**（`host/文件名`），不是完整 URL——CDN 每次读都重新签名，整条 URL 比对会把同一张图当成新图 |
| `kind` | `image` / `cover` / `video` / `audio` |
| `filename` | 落盘路径（`media.py` 写的） |
| `state` | `complete` / `discovered` / `interrupted` / `failed` |
| `discovered_at`/`downloaded_at` | 发现时间 / 完成时间 |

`state='complete'` 只证明**当时下完了**，不证明文件还在——所以每次复用都要再查一次磁盘
（`downloaded_files` 会 `is_file()`）。文件被删就当作没下过，重下。

**阶段一就会写这个表**：初筛时把整个轮播的图片名都记下来（state=`discovered`），只有封面
是 `complete`。这样阶段二第一次下媒体时，不会把轮播里其余的图读成「新增图片」。

## comments_meta —— 评论区读得全不全

| 列 | 含义 |
|---|---|
| `note_id` PK | — |
| `declared_total` | 页面自己声明的评论总数 |
| `collected` / `replies_collected` | 实收主评论数 / 回复数 |
| `thread_ended` | 是否滚到了「没有更多」 |
| `possibly_incomplete` | 读到了中止信号（超时、丢元素），下次值得再读 |
| `captured_at` | 采集时间 |

评论树本身在 `comments.json`；这张表只回答「要不要再读一次」。
`possibly_incomplete` 为真会让阶段二的快路径失效——这正是它存在的意义。

## runs —— 一次运行

| 列 | 含义 |
|---|---|
| `run_id` PK | 自增。阶段一的 `--run-id` 就是它 |
| `kind` | `screen`（阶段一）/ `collect`（阶段二） |
| `started_at`/`finished_at` | 起止时间 |
| `data_root` | 这次跑在哪个数据根下（同一份笔记可能在多个根里各有一份） |
| `keywords` | 分号分隔的搜索词（原样存，不解析） |
| `filters_json` | 施加的筛选，`{维度: 选项}` |
| `args_json` | 这次的 `limit`/`screenLimit`/`excerpt`/`noScreen` |
| `totals_json` | 结果统计：候选数、打开数、失败数，以及**每个关键词各自的**
  候选/打开/库里已有/未展开/重复/是否取到上限 |

**没有「主题」这一层**：`keywords` 是搜索词，不是主题（一个主题如「甘南环线前期调研」
会包含多轮、多个词、多个 run）。

## run_items —— 台账

阶段一的产物、阶段二的输入。

| 列 | 含义 |
|---|---|
| `run_id` + `note_id` PK | 一篇笔记在一次 run 里只有一行 |
| `rank` | 在这个 run 里的位次（1 起，**跨关键词连续**，与摘要表的编号一致）。某个词内部的原页位次 = 这个顺序按 `keyword` 过滤后的顺序 |
| `href` | **带 `xsec_token` 的链接**。这是唯一还能用的 URL 形状，阶段二直接重放它，不用 noteId 重拼 |
| `keyword` | 这篇是哪个词搜出来的（跨词重复的归先出现的词） |
| `decision` | `pending` / `keep` / `drop` |
| `reason` | `decide.py --reason` 的原样文本 |
| `decided_at` | 判断时间 |

没打开过的候选**也有行**（`decision='pending'`），所以「这一轮看了哪些、漏了哪些」查得出来。
但行里只有 `note_id`/`rank`/`href`/`keyword`——**卡片的标题、作者、卡片时间、互动数没有落库**，
那些只在当时的摘要表里出现过。

## notes.status 状态机

```
seen ──打开初筛──> screened ──keep──> approved ──阶段二完成──> collected
  │                   │                   └──drop──> rejected
  └───────────────────┴───────────────> failed
```

- `seen`：在搜索页上发现了，页面没打开（`mark_seen`）
- `screened`：打开过，正文和封面读了，还没判断
- `approved`/`rejected`：`decide.py keep|drop` 的结果
- `collected`：阶段二的采集流程走完了（**不等于内容齐了**，见下）
- `failed`：抓取失败，留着是为了让下次重试

`run_items.decision` 是**每一轮**的记录，`notes.status` 是**最新一次**的汇总。改判就是再记一条
新的 decision，status 跟着变。

## 去重今天怎么判

没有一个字段能回答「这条下过没有」，它是四个条件的合取，分别在三处：

| 判据 | 在哪 | 谁用 |
|---|---|---|
| `notes.status == 'collected'` | 库 | `batch.already_done` |
| 请求的每个 part 的文件都在盘上 | **磁盘**（`artifacts_missing`） | 同上 |
| `comments_meta.possibly_incomplete` 为假 | 库 | 同上 |
| `updated_at`/`content_hash`/图片集没变 | 库（`db.compare`） | `batch.parts_to_run` |

`batch.py` 的顺序是：先查库 → 过了上面四关就不开浏览器（快路径）；没过的才导航到笔记页，
读 SSR 状态，再比一次，然后**只补缺的那部分**（`--part body|media|comments`）。

两个已知的粗糙处：

1. **`collected` 写得太早**。`batch.py` 在采集流程没有抛异常时无条件写 `collected`，
   不检查 `downloads.json` 里有没有 `failed` 的条目。所以「库里说 collected、文件却没下来」
   是可能的，靠 `artifacts_missing` 和 `possibly_incomplete` 兜着。
2. **「下了一半」看不出来**。只有正文没有图、或有图没有评论的状态，只能靠三个
   `last_*` 时间戳反推。

## 缺什么（候选表结构）

按「后续阶段要查什么」列，供判断：

| 想要的答案 | 今天能不能查 | 备注 |
|---|---|---|
| 这条笔记有正文吗、摘要是不是全文 | 能 | `notes.excerpt_kind`；正文只有标签的条目 `excerpt` 是 NULL |
| 这条笔记我见过吗 | 能 | `notes` 一行即知 |
| 我判断过吗、理由是什么 | 能 | `run_items`（每轮）+ `notes.status`（最新） |
| 这条的链接还能用吗 | 能 | `run_items.href`，但要按 run 查 |
| 文件下过吗、还在吗 | 能，但要 join 库 + 查磁盘 | 见上表 |
| 这条是什么时候、哪一次运行下全的 | **不能** | `notes` 没有 `collected_run_id`/`first_complete_at` |
| 这条属于哪个主题 | **不能** | `runs` 只有 `keywords`，没有主题这一层 |
| 这些文件占多少盘 | **不能** | 只能现算文件系统 |
| 未打开的候选，标题/作者/卡片时间是什么 | **不能** | `run_items` 没存卡片元数据 |
