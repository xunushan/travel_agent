# chrome-agent 故障报告：大快照导致扩展通道断开

写给 chrome-agent 的维护者，用于排查 `page snapshot` 在响应较大时报
`Extension disconnected` 的问题。日期 2026-10-08。

> **状态：已定位并修复（2026-10-08，chrome-agent 侧）。**
> 原报告把成因写成「消息超过大小上限」——**这一条是错的**，下面的「复查结论」给出
> 真正的原因、证据与修复。原始观测记录保留在本页后半部分，其中的**现象**都成立，
> 只有**推断**需要按复查结论读。

## 复查结论：不是消息大小，是消息里有半个字符

`content.js` 的文本截断按 UTF-16 单位切，正好切在一个 emoji 的两个代理项中间，留下
半个字符（孤立代理对，lone surrogate）。它一路都是合法的：

1. 扩展发出去时 `JSON.stringify` 把它写成 `"\ud83c"` 转义（ES2019 起保证 well-formed）；
2. Python 的 `json.loads` 原样接受这个孤立代理，不做校验；
3. native host 转发给 daemon 时用 `json.dumps(..., ensure_ascii=False).encode("utf-8")`
   把字符本身写回字节流 —— **UTF-8 没有孤立代理的编码**，抛
   `UnicodeEncodeError: surrogates not allowed`；
4. 这个异常从读循环里抛出去，**进程退出**，Chrome 关掉端口，调用方收到
   `Failed to forward to extension: Extension disconnected`。

所以报错字符串是误报、扩展也没掉线，原报告这两条判断是对的；错的是「因为消息太大」。

### 证据：失败点是一个固定的字符偏移，不是一个长度

`~/Library/Application Support/ChromeAgent/logs/native-host.log` 里，修复前共 87 次
`surrogates not allowed`，报错位置只落在 9 个固定的字节偏移上：

```
29 次 position 14278     15 次 position 1213149    5 次 position 9501
15 次 position 1212909    5 次 position 4900       4 次 position 154916
4 次 position 149650      4 次 position 14885      1 次 position 141
```

- **最小的偏移是 4900 字节**，出现在 2026-10-07 23:49，离 1 MB 差三个数量级 ——
  「≥ 约 1.3 MB 就断」不成立：触发条件是**某个特定字符**出现在消息里，与长度无关。
- 本报告的 2500 / 2800 / 3000 三次失败都落在 1212909 / 1213149 上，而成功的 2000
  是 1,093,291 字节 —— 还没写到那个字符。**阈值落在 2000 与 2500 之间是这两个事实
  叠出来的巧合**，不是一条边界。
- 修复后同页同 tab 用 141 字符的小消息也复现了同一个错误，可排除「接近上限才出问题」。
- 触发字符不止一种：偏移 4900 / 14278 那批报的是 `\ud83d`，1212909 那批是 `\ud83c`。

### 消息通道真正的大小上限（回答原报告第 1 问）

`page snapshot` 的响应走的是**扩展 → native host**方向，不是 host → Chrome 方向：

| 方向 | 上限 | 出处 |
|---|---|---|
| 扩展 → native host | 64 MiB（67,108,864） | Chromium `native_messaging` 的 `mojom::kMaxMessageBytes`；扩展文档同述 |
| native host → Chrome | 1 MB（1,048,576） | `kMaximumNativeMessageSize`；扩展文档 "The maximum size of a single message from the native messaging host is 1 MB" |
| 本进程另设 | 10 MB（`MAX_MESSAGE_BYTES`） | `chrome_agent/native_host/native_host.py`，比 Chrome 给的更紧 |

这三个都不解释本次故障 —— 本次在小消息上也会挂。（旧文档里的「4 GB」是错的，见
w3c/webextensions#849。）

### 已做的修复

| 层 | 改动 | 文件 |
|---|---|---|
| 生产者 | 截断改为按**码点**切（`textSlice`），并在响应出口统一修复孤立代理（`repairSurrogates`），所有响应都过这一个出口 | `extension/content.js` |
| 放大器 | 单条坏消息不再拖垮进程：读帧失败只丢这一条、流仍对齐（`UnreadableMessageError`）；序列化改 `ensure_ascii=True`，孤立代理写成转义，任何能解析的消息都可发送 | `chrome_agent/native_host/native_host.py` |
| 误报 | 转发失败按原请求 id 回给 daemon，调用方听到具体原因，而不是等到超时再被告诉「扩展断开了」 | 同上 |
| 文档 | `Extension disconnected` 一行加了边界说明：**每次都是同一条命令报这个**就不是通道问题 | `references/troubleshooting.md` |

### 验证（修复安装到本机之后实测）

- `page snapshot --limit 3000` 在同一笔记同一页面返回 **1,647,112 字节**
  （大于原报告的成功值 1.09 MB，也大于失败区间）：孤立代理 0 个，250 个 emoji 完整。
- `page text --max-chars 4` 返回 `吴小明🎶`，截断不再切在字符中间。
- 用站点技能跑完整评论采集：写出 35,863 字节的 `comments.json`，含 4 条顶层评论与
  51 条回复 —— **原报告里 4 次尝试全部死掉的那一步现在跑通了**。
- 全程 `Extension disconnected` 出现 **0 次**：`daemon.log` 最后一行仍是 17:05:34 的
  `Extension registered`（即这次修复的重启），`native-host.log` 之后一行未写。
- 已安装：venv 里 `chrome_agent/native_host/native_host.py` 与仓库同 sha256
  （`86f994dc7489…`），`~/chrome-agent/extension/content.js` 与仓库同 sha256
  （`45f45cf43ec9…`），`extension status` 报 `reloadNeeded: false`、
  磁盘 hash = 已加载 hash = `c44bd6abf9cf…`。

### 剩下三个问题的答复

1. **上限**：见上表；与本次故障无关。
2. **可区分错误**：已做。坏消息不再变成通道故障，且失败会带着原请求 id 回到调用方。
3. **是否需要钳 `--limit` / 分页**：不需要。快照没有触到任何上限；原报告里
   「1.09 MB 活 / 1.3 MB 断」是假象。调用方对这几个报错字符串的重试与换 tab 可以去掉。

## 原始报告（2026-10-08 当日观测，现象成立，推断已被上面的复查修正）

## 一句话结论（原文，已修正）

## 环境

| 项 | 值 |
|---|---|
| OS | macOS (Darwin 24.6.0) |
| chrome-agent | 0.1.0 |
| daemon | running (protocol 1.0) |
| extension | loaded and up to date；`/Users/isuntaiyang/chrome-agent/extension`，磁盘 hash = 已加载 hash = `c00685a5f202` |
| 调用方 | `travel_agent/skills/xhs-downloader` 的自定义脚本，只走 CLI + `--json` |
| tab | id `2224023`，页面 `https://www.xiaohongshu.com/explore/69042313000000000402be51?xsec_token=ABdFbeknNiDMJmzxu35W_1uEL1N8tcxzxU1aky8ghgo3U=&xsec_source=pc_feed` |

## 最小复现（裸命令，无脚本参与）

```bash
chrome-agent page snapshot --tab-id 2224023 --limit 500  --json   # OK  284,652 字节
chrome-agent page snapshot --tab-id 2224023 --limit 1000 --json   # OK  560,253 字节
chrome-agent page snapshot --tab-id 2224023 --limit 2000 --json   # OK  1,093,291 字节
chrome-agent page snapshot --tab-id 2224023 --limit 2500 --json   # 失败
chrome-agent page snapshot --tab-id 2224023 --limit 2800 --json   # 失败
chrome-agent page snapshot --tab-id 2224023 --limit 3000 --json   # 失败
```

失败返回体（逐字）：

```json
{"error": "Failed to forward to extension: Extension disconnected", "method": "page.snapshot"}
```

阈值落在 **2000 与 2500 之间**。成功侧最大测到 1.09 MB / 2000 个元素；失败侧拿不到字节数。

## 关键补充事实

1. **通道不会持续坏**：每次 2500/2800/3000 失败之后，立刻在同一 tab、同一页发
   `--limit 500` 都立即成功——不需要重载扩展、不需要换 tab、不需要重新导航。
   所以「这一条消息有问题」比「扩展掉线」更像真实原因。
   （原文接着写的是「这一条消息**过大**」——复查已否掉这一层：问题是消息里的字符，
   不是消息的长度。）
2. **`extension reload` 无效**：重载扩展后再跑一次同样的 3000 快照，依然失败
   （可排除「注入脚本陈旧」）。
3. **同一条报错现在对应多类原因**，从消息本身分不出来：
   - 本例：请求过大；
   - 另一次真实瞬时故障：另一 tab 连续 3 次同一报错，换新 tab 后成功，过一阵原 tab 自己又好了；
   - 还见过第三种：`Internal error: Could not establish connection. Receiving end does not exist.`
     （该 tab 被导航到一个 404 页之后）。
4. **调用方为什么必然踩到**：xhs-downloader 读评论时每滚一步都取一次大快照，
   `locators.yaml` 里 `scroll.comments.snapshot_limit: 3000`（注释写明是为了装下一个
   66 行评论线程）。在这条笔记上（顶层 5 条 + 92 条回复被展开、声明 628 条），
   DOM 元素数涨过 ~2300 之后，这个 3000 的快照必失败。
   （「元素数涨过 ~2300 就失败」这条推断已被复查否掉：失败与元素数无关，是消息里
   出现了那个字符，而元素越多越可能把它包进去。真正被 3000 限制住的是**能读到几条
   顶层评论**——见本页顶部的复查结论与 `locators.yaml` 该处注释：改为 8000 后同样
   的笔记从 4 条变成 10 条。）

## 调用序列与失败点

循环里每一步都是独立 CLI 调用：

- `page snapshot --tab-id N --limit 3000`（解析滚动锚点 / 展开按钮）
- `page scroll --tab-id N --ref <ref> --dy 900`
- `page click --tab-id N --ref <ref>`（展开回复，间隔 1.0s，最多 30 轮）

完整堆栈（`collect.py` → `comments.py:201 expand_replies` → `runtime.comment_snapshot`
→ `runtime.snapshot` → `runtime.chrome_agent`）：

```text
File ".../comments.py", line 201, in expand_replies
    button = find_first(comment_snapshot(tab_id, config), rules["comment_show_more"])
File ".../runtime.py", line 196, in comment_snapshot
    return snapshot(tab_id, comment_snapshot_limit(config))   # → --limit 3000
File ".../runtime.py", line 187, in snapshot
    return chrome_agent(*args)
RuntimeError: Failed to forward to extension: Extension disconnected
```

失败发生前脚本侧的重试已经跑满（调用方对这几个报错字符串重试 5 次 × 1.5s 退避，
外层整条笔记再重试一次）。**共 4 次尝试全部死在这一点，产出文件一个字节都没写**
（30 步上限 1 次、60 步 3 次，其中 1 次在 `extension reload` 之后）。

## 希望 chrome-agent 侧回答的三个问题（已于「剩下三个问题的答复」一节回答）

1. `page snapshot` 的响应是不是走 Chrome 扩展消息通道？有没有明确的单条消息大小上限
   （实测 ~1.1 MB 活、≥ 约 1.3 MB 断）？
2. 超限时能否返回一个**可区分的错误**（如 `response too large` / `snapshot truncated`），
   而不是 `Extension disconnected`？现在调用方会把它当通道故障，做无用的重试与换 tab。
3. 推荐做法是什么：把 `--limit` 钳到安全上限并在响应里标 `truncated`，还是提供分页/游标？
   只要调用方能知道「被截断了」，就可以自己降级（分两次取，或降低上限重试）。

## 未验证的一条线索

早前一次 12 篇批量采集里，另一条笔记在同一环节连续 3 次同一报错（当时按瞬时故障处理，
换新 tab 后成功）。原文猜「评论 DOM 随滚动增长，越过阈值即断；新 tab 的 DOM 从零开始，
所以侥幸过了」。

**复查后的读法**：机制仍然是同因，但变量不是 DOM 大小而是**那段文本在不在消息里**。
日志支持这一点——2026-10-07 23:49 那批报错（偏移 4900 / 14278）远小于 1 MB，说明
触发条件是内容而非体量；换新 tab 后 DOM 从零开始、评论区还没展开，那个字符自然不在
快照里，于是「好了」。同样地，这次修复之后再没出现过这个报错。

## 本次排查用到的诊断手段（可复用）

- `chrome-agent status` / `chrome-agent extension status`：确认 daemon 与扩展都正常，
  磁盘 hash == 已加载 hash。
- `chrome-agent extension reload`：无效，可作排除项。
- 二分 `--limit`：500 / 1000 / 2000 / 2500 / 2800 / 3000，定位阈值。
- 每次失败后立刻跑一次 `--limit 500`：验证通道是「这条消息坏了」而非「通道坏了」。
