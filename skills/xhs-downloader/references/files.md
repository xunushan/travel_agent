# 目录职责与改动规矩

```text
<本 skill 目录>/                     # 装好后是 ~/.claude/skills/xhs-downloader
  SKILL.md              # 唯一常驻入口：两个入口 + 两条下载规则 + 硬性不变量
  references/           # 重内容，按需加载（flow / pitfalls / files）
  locators.yaml         # 页面语义规则：选择器、作用域、滚动与阈值
  scripts/
    runtime.py          # 通用层：CLI 调用与重试、快照、ref 匹配、文本读取、路径解析（无站点流程）
    state.py            # 详情页：读 SSR 状态（JS 字面量 → JSON），拿 updatedAt/stats 等
    discover.py         # 搜索页 → 候选清单（含 --list-filters）；被 download 复用来开关详情页
    note.py             # 详情页：元数据与正文（状态优先、DOM 兜底）→ note.json
    media.py            # 详情页：图片/视频发现与下载、封面、幂等归档 → downloads.json
    comments.py         # 详情页：评论滚动、展开、解析、嵌套 → comments.json
    collect.py          # 编排：一个已打开的笔记，收哪些件就写哪些文件（无自己的命令行）
    download.py         # 唯一的下层入口：--from/--url/--note-id × --part，维护两条规则与索引
    db.py               # 索引：notes 一张表（不存正文与评论树，也没有"已下载"标志位）
  schemas/              # note / comments / downloads 三份产出合同
  tests/                # 规则单测、产出与合同一致、fake_site.py 假站点
```

模块依赖是单向的：`download → {collect, db, discover, media, runtime}`，
`collect → {note, media, comments, runtime}`，`discover → {db, note, runtime}`，
底层是 `runtime`/`state`。

## 改动规矩

- **改选择器 / 阈值** → 只改 `locators.yaml`。脚本里不得内联站点 selector，也不得另起一套滚动
  终止规则。
- **改产出结构** → 同时改脚本和对应的 `schemas/*.schema.json`；`tests/test_schemas.py` 会用一次
  真实采集校验两者一致。`note-output.schema.json` 里 `additionalProperties: false`——往
  `note.json` 加字段必须同时改 schema 和 `collect.SCHEMA_VERSION`（当前 4），三份产出合同与
  `downloads.json` 的 `examined` 字段都在这一版上。
- **`ref` 的生命周期**：同一元素的 ref 在多次快照间保持不变，元素被移除/重渲染后失效，导航或刷新
  会替换整份注册表。所以流程里给的永远是「怎么找」，不是可复用的 ref。
- **测试**：`python -m pytest -q`（在仓库根跑）。脚本目录被 `tests/conftest.py` 插进 `sys.path`，
  所以脚本之间按裸名互相 import（`import db`）；新增模块要同时进 `test_playbook.patch_browser`
  的模块元组——**任何在 import 期绑定 `chrome_agent` 的模块都必须打桩**，否则测试会真去调 CLI。
  假站点在 `tests/fake_site.py`（`FakeSite` + `site` fixture），`discover` 与 `download` 的测试
  共用它。

## 数据布局

```text
<root>/                         # --data-root > $XHS_DATA_ROOT > ~/Documents/travel_agent
  xhs.db                        # --db 可指到别处
  notes/<noteId>_<标题>/        # 目录名由标题定，建一次就不改
    note.json  cover.webp  images/  videos/  comments.json  downloads.json
```

`runtime.data_root()` / `db_path()` / `notes_dir()` 是唯一的解析处，两个入口共用
`runtime.add_data_arguments()`，所以 `--data-root`/`--db` 的含义一定一致。
**两个入口必须用同一个根**，否则 download 认不出 discover 标过"在库"的笔记。

**目录名里带标题，而标题在笔记页里**，所以顺序被定死了：先开页面读笔记 → 定目录 → 再下媒体。
`downloads.json` 记的是**绝对路径**，所以已存在的目录**永远不重命名**。中断的那一轮留下的
`<root>/notes/<noteId>/`（还没定标题）会被合并进标题目录，而不是留在旁边成为半篇笔记。

## 索引：一张表，六列

正文的载体永远是那三个 JSON 文件；索引只存**判断要不要开页面**需要的东西。删掉 `xhs.db`
不会丢笔记，只会丢掉「这篇的链接和版本」，重跑 discover 就能重建。

```
notes(note_id PK, url, title, note_dir, updated_at, content_hash)
```

- `url` 是**重放用**的详情链接（带 `xsec_token`）：只有它能重新打开这篇笔记，所以按 id 拼 URL
  是禁止的，`--note-id` 找不到链接时会明确报"先 discover 再下载"。
- `updated_at` 是站点的 `lastUpdateTime`，`content_hash` 是标题+正文+排序后标签的 sha256。
- 索引里**没有"已下载"这类列**：那是对文件的镜像，只会跟磁盘互相矛盾。"我有没有这个件"由
  `download.downloaded_parts(note_dir)` 读文件回答；`candidates.json` 就是清单本身，用完即弃，
  不建表。

`store_note` 是**逐列 `COALESCE`，不是 `INSERT OR REPLACE`**：只下图片的一轮没有标题可给，
不能把已经存好的标题抹成 NULL。`content_hash` 只在这一轮真的读了正文时才算——没读正文的一轮
拿 `None` 进去，哈希保持原样；否则每一轮媒体更新都会把笔记报成"编辑过了"。

## 判重口径

`updated_at`（秒级）**只做正向证据**——只有它比库里的大才算「变了」；两侧任一为空时它什么都
证明不了，于是退到 `content_hash` 比较，而不是从一个空字段得出「没变」。`content_hash`
**不含互动数**，**也不含图片 URL**（`note.json` 不允许额外字段，只重读正文时算不出同一串
URL，会误报编辑）。

图片换没换由 `download` 直接比**文件名集合**（`media.media_append_or_merge`），增删都认——
按 URL 比会因为 CDN 每次重新签名而永远报「变了」，于是每轮都重下、并把新文件落在旧文件旁边
当 `-2`。

打开之后的结论见 [flow.md](flow.md#21-输出)（`new`/`fill`/`refresh`/`check`/`skip`）：
请求的件文件在、`downloads.json` 里该 `complete` 的条目指向的**文件仍然存在**、
`comments.json` 没有 `possiblyIncomplete`——三条都成立，下一轮整篇跳过、连页面都不开。

**因为已有而跳过的一轮必须把文件名写回 `downloads.json`**（`collect_media(known_files=…)`
会为跳过的图片写 `complete` 条目）——只写「发现」会抹掉文件记录，让下一轮再下一次，如此反复。
