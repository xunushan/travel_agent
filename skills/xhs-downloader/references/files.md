# 目录职责与改动规矩

```text
<本 skill 目录>/                     # 装好后是 ~/.claude/skills/xhs-downloader
  SKILL.md              # 唯一常驻入口：路由表 + 两阶段 + 硬性不变量
  references/           # 重内容，按需加载（flow / pitfalls / filters / selection / files）
  locators.yaml         # 页面语义规则：选择器、作用域、滚动与阈值
  scripts/
    runtime.py          # 通用层：CLI 调用与重试、快照、ref 匹配、文本读取、路径解析（无站点流程）
    state.py            # 详情页：读 SSR 状态（JS 字面量 → JSON），拿 updatedAt/shares 等
    discover.py         # 搜索结果页：发现候选、打开并核验落点
    note.py             # 详情页：元数据与正文（状态优先、DOM 兜底）→ note.json
    media.py            # 详情页：图片/视频发现与下载、封面、幂等归档 → downloads.json
    comments.py         # 详情页：评论滚动、展开、解析、嵌套 → comments.json
    collect.py          # 编排 + CLI：--part body|media|comments|cover|all
    search.py           # 阶段一：filters（读真实选项）/ run（施加筛选、批量初筛、打摘要表）
    decide.py           # 阶段一：keep / drop / show（只写库，不抓页面）
    batch.py            # 阶段二：按 run 或 plan 下载清单里的笔记，含快路径与去重
    db.py               # sqlite 索引与去重台账（不存正文与评论树）
    migrate.py          # 离线把旧目录改写成当前结构
  schemas/              # note / comments / downloads 三份产出合同
  tests/                # 规则单测、产出与合同一致、e2e_search.py 手动冒烟
```

## 改动规矩

- **改选择器 / 阈值** → 只改 `locators.yaml`。脚本里不得内联站点 selector，也不得另起一套滚动
  终止规则。
- **改产出结构** → 同时改脚本和对应的 `schemas/*.schema.json`；`tests/test_schemas.py` 会用一次
  真实采集校验两者一致。`note-output.schema.json` 里 `additionalProperties: false`——往
  `note.json` 加字段必须同时改 schema 和 `collect.SCHEMA_VERSION`（当前 3，`migrate.py` 跟它对齐），
  并在 `migrate.py` 里补默认值。
- **`ref` 的生命周期**：同一元素的 ref 在多次快照间保持不变，元素被移除/重渲染后失效，导航或刷新
  会替换整份注册表。所以流程里给的永远是「怎么找」，不是可复用的 ref——每次都从当次快照重新解析。
  同理，`--part` 每个 part 自取一次快照。
- **测试**：`python -m pytest -q`（在仓库根跑）。脚本目录被 `tests/conftest.py` 插进 `sys.path`，
  所以脚本之间按裸名互相 import（`import db`），新增模块要同时进 `conftest.py` 的路径与
  `test_playbook.patch_browser` 的模块元组——**任何在 import 期绑定 `chrome_agent` 的模块都必须打桩**，
  否则测试会真去调 CLI。

## 数据布局

```text
<root>/                         # --data-root > $XHS_DATA_ROOT > ~/Documents/travel_agent
  xhs.db                        # --db 可指到别处
  notes/<noteId>/               # 阶段二可用 --output-root 指到别处（默认就是这里）
    note.json  comments.json  downloads.json  images/  videos/
```

`runtime.data_root()` / `db_path()` / `notes_dir()` 是唯一的解析处，四个入口共用
`runtime.add_data_arguments()`，所以四个入口的 `--data-root`/`--db` 含义一定一致。
**两个阶段必须用同一个根**，否则阶段二找不到阶段一建的索引。

## 判重

DB 只做**索引与台账**，正文载体仍是那三个 JSON 文件（DB 不存正文、不存评论树）。

```
notes(note_id PK, url, title, author, …, published_at, updated_at, updated_at_source,
      tags_json, likes, collects, comment_count, shares, content_hash, content_len,
      media_count, status, schema_version, note_dir, excerpt, first_seen_at, last_seen_at,
      last_screened_at, last_collected_at, last_media_at, last_comments_at)
media(note_id, url, kind, filename, state, discovered_at, downloaded_at,
      PRIMARY KEY(note_id,url))          -- 幂等下载的依据
comments_meta(note_id PK, declared_total, collected, replies_collected,
              thread_ended, possibly_incomplete, captured_at)
runs(run_id PK, kind, started_at, finished_at, data_root, keywords, filters_json,
     args_json, totals_json)
run_items(run_id, note_id, rank, href, keyword, decision, reason, decided_at,
          PRIMARY KEY(run_id,note_id))    -- 阶段一的产物、阶段二的输入
```

**口径**：`updatedAt`（站点的 `lastUpdateTime`，秒级）**只做正向证据**——只有它比库里的大才算
「变了」；两侧任一为空时它什么都证明不了，于是退到 `content_hash` 比较，而不是从一个空字段得出
「没变」。`content_hash` = 标题 + 正文 + 排序后的标签的 sha256；**不含互动数**（每次都在漂，
折进去会让每条笔记每次看起来都被编辑过），**也不含图片 URL**（`note.json` 不允许额外字段，
图片不在 note 字典里；只重读正文时算不出同一串 URL，会误报编辑）。图片换没换由
`media_urls_changed` 直接比 URL 集合，增删都认。

打开之后的六种结论，顺序即设计：

| 结论 | 条件 |
|---|---|
| `new` | 库里没有 |
| `redownload_media` | 内容变了且图片集合也变了；或内容没变但图片集合变了 |
| `recollect` | 只有标题/正文/标签变了（重新读一遍正文，顺带刷新互动数） |
| `refresh_comments` | 没变，但评论数**增长**了（下降只说明站点重新计数，不是新信息） |
| `refresh_stats` | 没变，只有互动数漂移 |
| `skip_dup` | 都没变（只更新 `last_seen_at`） |

`--dedup skip`（默认）= 允许停在 `skip_dup`；`refresh` = 永不停在 `skip_dup`；`force` = 一律全量重采。

**已下载 = `state=complete` 且 `filename` 非空且文件仍在磁盘**（`db.downloaded_files()`）。
反过来，一次**因为已有而跳过**的下载必须把文件名写回索引（`media_rows(..., known)`）——只写
「发现」会抹掉文件记录，让下一轮再下一次，如此反复。

`notes.status`（`seen → screened → approved/rejected → collected`，另有 `failed`）让「这条已经
筛过并 drop 了」下次一查即知，**不再打开页面**；阶段一重跑时这些候选仍会列进摘要表，带
`[已筛过:keep|drop]`／`[已下载]` 标记。
