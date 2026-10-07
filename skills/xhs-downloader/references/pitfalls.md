# 踩坑与证据

每条都是实测结论，括号里是证据。改代码前先看这一节；症状对不上时从「常见的报错」往下找。

## 1. 详情链接必须原样使用

`xsec_token`/`xsec_source` 是访问上下文，禁止从笔记 ID 拼接、缩短或删除；优先点搜索结果里的
实时 ref，必须导航时只原样用当前页面发现的完整 href。裸 `/explore/<id>` 不是可复用链接，
落盘 URL 里的 token 也会过期（实测一条旧 URL 导航后落到 `/404`）。

## 2. 后台标签页会节流，读评论必须切前台

Chrome 对后台标签页降频，小红书的下一批评论正由那个被降频的事件循环追加。实测一条声明 610 条的
笔记：后台时滚到最底一分钟也不加载（容器行数停在 40、`maxScrollY` 停在 7092）；切前台后再滚，
行数 40→60→80→100。所以「评论只读到 10 条、怎么滚都不动」几乎总是这个原因。代价是采集期间会
抢占前台焦点，脚本会自己 `tabs activate`。

## 3. 搜索结果渲染慢，不等待会误判「无结果」

结果是客户端渲染的，实测延迟很长：同一页 6s → 0 张卡片，14s → 仍 0 张，更晚才出现 25 张
`section.note-item`。`discover.py` 只返回当次快照里的东西，所以**不轮询等待的调用方会对一次
成功的搜索报「没有结果」**。`search.py run` 用 `render_timeout`（默认 45s）轮询等待；
`discover.py` 直接用时要自己确保页面已渲染。

## 4. 筛选只能点控件，URL 参数无效

实测 2026-10-07：导航到带 `sort=time_descending&noteType=2` 的结果页，页面自己的
`searchContext` 仍是 `general`/`0`，一张卡片都没动。站点把筛选放在前端状态里，**只有点击能设置**。
细节与施加/验证机制见 [filters.md](filters.md)。

## 5. 施加筛选后要验证，不能用「卡片变没变」验证

点一个选项后页面会把它标成生效项（`active`），这是可以验证的；而**对比点击前后的结果卡片行不通**
——实测点「半年内」后前六张卡片完全一样，因为最新结果本来就在半年内。卡片集合没变既可能是
「本来就满足」也可能是「点空了」，两种含义完全相反。筛选静默失效会返回一页看起来跟成功搜索
一模一样的不相关笔记，所以 `apply_filters` 在验证不通过时**直接中止**。

## 6. 快照是「按屏幕位置排序后取前 N 个」

`page snapshot` 把所有可交互或有文本的元素按屏幕纵坐标排序（±50px 视为同一行，再按 x），
取前 N 个，默认 N=500，并返回 `matched`/`truncated`。**被丢掉的是排序靠后的一端，不是「滚出视口
的部分」**：实测一条长评论笔记滚到底后，`--limit 500` 返回的元素 y ∈ [-12073, -7956]——笔记正文
（y=-12037，在视口上方）仍在快照里，而左栏轮播（y=32，在视口里）反而不在。调到 `--limit 3000`
后 `matched=3626, truncated=true`，两者都能通过 `page text --ref` / `page images --ref` 解析。

实践规则：读长评论时把上限提到 `scroll.comments.snapshot_limit`（3000，`collect.py` 自动做）；
`--load`（`page images` 的滚动加载）默认不开，因为它会滚动页面、改变排序位置。

## 7. 评论读取顺序与终止条件

读取顺序固定：**先在笔记刚打开、还没滚动时读正文/图片/播放器，再滚动评论，最后才下载**。
（这就是 `collect.py --part` 各取一次快照、且手工连跑时 `comments` 须最后的原因：评论滚动会把
轮播挤出快照窗口。）评论滚动以「容器文本连续 `max_idle_rounds` 轮没有新增行」为终止条件——
滚动条报 `moved: false` **不代表**到底（实测 `moved` 已为 false 之后滚动上限仍从 3281 涨到 7938、
行数 19→55）。判「够不够」用容器文本（`page text`，上限 20 万字符），不要用 snapshot 的元素个数。

## 8. 展开回复的轮次要覆盖整轮采集

`scroll.comments.max_expand_rounds` 不小于 `max_steps`：每次只按一个「展开 N 条回复」，设小了
会让尾部评论的折叠回复留在折叠态（实测 30 条预算下设 10，回复数 46→77 后就不再增长）并报 warning。

## 9. 评论滚动锚点必须在右半区

左半区是图片轮播，在那里滚动是翻到下一张图，不会加载评论。`comments_scroll_anchors` 依次尝试
评论容器 → 正文容器；正文是兜底，因为刚打开的笔记里 500 个名额可能被结果网格和每行的 `...`
菜单占满，评论容器进不了快照。

## 10. 找回复用 `comment-inner-container`，不是 `comment-item-sub`

同一个父评论下，第 1 条回复的外层是 `comment-item comment-item-sub`，第 2 条及以后只是裸的
`comment-inner-container`。按 `comment-item-sub` 找会静默丢掉每个父评论第 2 条之后的全部回复。
每行（顶层或回复）都恰好有一个 `comment-inner-container`，父评论自己那个由脚本按矩形排除。

## 11. 嵌套树分两段，来源不同

- **顶层评论 ←→ 它的回复**：只认 DOM，`parent-comment` 容器包住房自己的行和整个 `reply-container`
  （作者回复时小红书会省掉 `回复 X ：` 前缀，文本里看不出来）。
- **回复 ←→ 回复**：DOM 里是平铺的（实测三条回复是同一 `reply-container > list-container` 里的
  兄弟节点，x/宽度全相同），只能从文本的 `回复 X ：` 前缀重建。顶层评论作者不做特殊处理——
  特判会把实测的「提问→作者答→追问→作者再答」三级链压成平级。

## 12. 评论行的正文边界要「形态 + 下一行」两个条件

每行 `text` 到「时间+地区」那一行为止：该行之上是正文（`作者`/`置顶评论` 徽标剔除），之下是
`赞`/点赞数/`回复`。判定该行既要求形态像日期或相对时间，**又要求紧邻下一行是那截 UI 尾巴**。
只看前者会吃正文：实测 `70-200的头`（镜头焦距）被 `\d{2}-\d{2}` 当成日期 `70-20`、`0的头` 当地区，
那条回复存成了空正文。

## 13. 掩码、关闭按钮与窄版布局

详情遮罩开着时点背景卡片会打空（`page click` 仍返回 `clicked: true`）。`discover.py` 先试
`detail.close_control`（`button` + `close-icon`），不行再发 `dismiss_keys`（`Escape`）；窄版布局
（实测 viewport 600×740）下关闭按钮是 `display:none`，实际生效的是 Escape。两条都不通就报错中止。

## 14. 图片作用域与下载白名单

先 `page images --ref <轮播 ref>` 发现并核验，再把 URL 白名单交给 `download-images`；不要对
`note-container`、评论容器或整页直接 `download-images --limit`，否则表情、头像、推荐卡片的小图会
一起进来。判断「是不是笔记原图」以**作用域**为准，不以 URL 或尺寸为准（头像是 360px、评论配图
640px，都过不了尺寸阈值）。轮播容器（`detail.image_media`）里的每一张都是本笔记的图；只有退到
`detail.container` 时才按 CDN 主机名和路径过滤。

## 15. 视频：`blob:` 不是文件地址

播放器容器里的 `<video>` 可能只暴露 `blob:`，direct MP4 在页面级 JSON-LD（`VideoObject`）里。
先 scoped media 确认播放器，再做一次不带 ref 的 page-level media discovery。blob/HLS/DASH/
unsupported 只报告，不下载。

## 16. SSR 状态：要整页导航，且不是严格 JSON

状态文本是 `<script>` 元素的文本（`state.py` 用 `page text --ref` 读），**在页面加载时冻结**。
SPA 内部跳转（点卡片）不重新注入：实测「搜索页」里 `search.feeds` 是 `[]`、
`note.noteDetailMap` 是 `{}`。要用状态就必须整页导航（`tabs navigate`）——这正是阶段一逐条抓候选
付出的墙上时间。另外它含 JS 裸 `undefined`（首个失败点 `"pwaAddDesktopPrompt":undefined`），
需要字符串感知的 `undefined → null` 归一化后再解析，详见 `state.py` 与 `tests/test_state.py`。

## 17. 媒体判重：索引说「有」不等于磁盘上还有

`state=complete` 只证明那次下载完成过。文件被删或被移走时，若还按索引跳过，就会得到一个
看起来正常的空档案。所以「已下载」= 状态 complete **且** `Path(filename).is_file()`。
反过来，一次**因为已有而跳过**的下载也必须把文件名写回索引——只写「发现」会抹掉文件记录，
让下一轮再下一次，如此反复。

## 18. 广告屏蔽插件会让 `search.feeds` 为空

探测期间页面上出现过广告屏蔽插件的告警，同一时刻 `search.feeds` 为空，可能影响搜索 XHR。
建议把 `xiaohongshu.com` 加入白名单。

## 19. 每个字段从哪来，边界在哪

`note.py` **状态优先、DOM 兜底**，取到哪个源会记在 `note.json` 的 `capturedFrom`
（`state` / `dom`）。同一个笔记两边的标签数实测一致（DOM `a.tag` 9 个、状态 `tagList` 9 个），
所以换源的收益不是「更全」，而是拿到 `updatedAt`/`shares` 并少几次 DOM 解析。

来自 SSR 状态（`note.noteDetailMap[<noteId>].note`）：

| 字段 | 状态键 | 备注 |
|---|---|---|
| 标题 / 正文 | `title` / `desc` | `desc` 是正文全文 |
| 标签 | `tagList[].name` | 落盘只留 `户外`，不带 `#…[话题]#` |
| 互动数 | `interactInfo` | `likedCount`/`collectedCount`/`commentCount`/`shareCount`（字符串，实测 `"1422"`） |
| 作者 / 作者 ID | `user.nickname` / `user.userId` | |
| 发布时间 | `time` | 毫秒时间戳 |
| **更新时间** | `lastUpdateTime` | 判重的主证据；实测未编辑的笔记 `time == lastUpdateTime` |
| 类型 | `type` | `normal` 等 |
| 图片清单 | `imageList[].url/urlDefault/fileId/livePhoto` | 注意 state 的 URL 带日期段，与 DOM `<img src>` 不是同一串，**不能混用做判重**（见 files.md） |

DOM 兜底（选择器在 `locators.yaml: detail.*`）：

| 字段 | 来源 | 边界 |
|---|---|---|
| 标题 | `h1.title` | 用 h1 限定，推荐卡片也有 `.title` |
| 作者 / ID | `author-container > a.name` | 评论作者也用 `a.name`，必须先框定 |
| 标签 | `hash-tag` | |
| IP 属地 | 页脚 `.date` 的 `日期 [属地]` | **并非每条都有**：实测四种形态 `08-01`、`09-29 河南`、`2025-11-03`、`4天前 西藏`；没有属地时存 null |
| 互动数 | `engage-bar interactions` 内的 `like-wrapper`/`collect-wrapper`/`chat-wrapper` | 实测 2341/3159/610。**页面没有分享数元素**，所以纯 DOM 路径下 `shares` 为 null；状态路径有 `shareCount` |
| 发布时间 | 笔记 ID 前 8 位十六进制解码（+08:00） | 页面只印部分日期（`08-01`、`4天前`），无法定位到具体时刻。两次验证：`6a6df219`→2026-08-01（与页脚 `08-01` 一致）、`6a1cf8f9`→2026-06-01（与另一份抓取的发布日期一致） |

互动区的 class 与评论行自己的计数同名，所以计数一律以 `engage-bar` 的矩形为界在内部找。

## 常见的报错

| 报错 | 含义与处置 |
|---|---|
| `需要 --keywords，例如 --keywords "川西秋色;稻城亚丁 秋"` | `--keywords` 缺失或全为空白 |
| `点了「X=Y」但页面没有把它标成生效项` | 筛选没施加成功，脚本已中止。先 `search.py filters` 看真实选项 |
| `筛选弹层打开了，但里面一个选项也没读到` | 页面改版。对照 [filters.md](filters.md) 改 `locators.yaml: search.filters.*` |
| `run N 不存在` | `--run-id` 给错；新建 run 要去掉 `--run-id` |
| `Tab not found: N` | tab 已关。重新 `tabs list` 拿 id |
| `详情遮罩仍开着：关闭控件不可见` | 见上面第 13 条 |
| `下载完成但找不到文件，未归档：<path>` | 见上面第 17 条；该 URL 会被记为已知但不记为已下载 |
| 评论只读到 10 条、怎么滚都不动 | 见上面第 2 条（后台标签页被节流） |
| `Receiving end does not exist` 一类传输层报错 | `runtime.chrome_agent` 已自动重试；持续失败查 chrome-agent daemon |
