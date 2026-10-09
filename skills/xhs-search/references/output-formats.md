# 输出契约

收尾时读一次。`ROOT` = 数据根，默认 `~/Documents/travel_agent`（`--data-root` 或
`$XHS_DATA_ROOT` 可覆盖，**必须与 xhs-downloader 用同一个根**）。

```text
<ROOT>/answers/<question_id>.md   # 结论文档（＋缺口声明，如有）
<ROOT>/runs/<question_id>.json    # run 日志
<ROOT>/notes/<noteId>_<标题>/     # xhs-downloader 的笔记，digest 都落在里面
```

`question_id` = **问题**归一化后（去空白/标点、统一大小写）的 sha1 前 8 位。

```bash
printf '%s' "$Q" | python3 -c "import sys,hashlib;print(hashlib.sha1(sys.stdin.read().encode()).hexdigest()[:8])"
```

## 结论文档

```text
# <问题>
## 结论      ← 严格按"要求"的结构逐项组织；每项结论写全本体、标注来源 noteId；
                分歧如实并列，不擅自调和；涉时效的结论标注信息年份
## 关键素材   ← 与结论相关的图卡/关键帧/美照：本地路径 + 一句话
```

- 结论的**结构照"要求"走**，不另起一套；要求没覆盖到的枝节不进结论。
- 每条结论后面标来源（`来源：<noteId>`）；同一条由多篇支撑就都列上。
- 两篇说法冲突时**并列写出**（各自标来源），**不调和、不取平均、不擅自选一个**；
  定不了谁对的，同时进缺口声明。
- 时效性结论（价格、班次、花期、开放时间）**标注信息所属年份**，不写成"现在的"。

## 缺口声明

**不进结论文档正文**，作为独立一节附在 `answers/<question_id>.md` 末尾（`## 缺口`），
同时落进 run 日志的 `gaps`。

```json
{
  "gaps": [
    {"item": "要求中未覆盖的项", "tried": "试过的搜索词", "why": "判断无法获取的理由"}
  ]
}
```

**只收录多轮尝试（直至停机）后仍无法获取的项**——缺口的界定与后续处置（换个问题再调、
放弃该项）由调用方决定。运行故障（下载失败、转写失败）**不是**"小红书上没有"，
要么重试，要么在 `why` 里如实写明是采集失败而非内容不存在。

## run 日志

每次运行落一份 JSON，结构固定：

```json
{
  "question": "...", "requirements": "...",
  "rounds": [
    {"keyword": "...", "filters": "...",
     "candidates": ["noteId"],
     "screen": [{"id": "...", "keep": 1, "pri": 1, "why": "..."}]}
  ],
  "reads": [{"id": "noteId", "verdict": "used | duplicate | no-increment"}],
  "stop": {"reason": "closed | rounds-cap | reads-cap", "rounds": 0, "reads": 0},
  "sources": ["noteId"],
  "gaps": []
}
```

- `rounds` 一轮一条；`candidates` 是该轮 discover 返回的全部 noteId，`screen` 是粗筛输出；
- `reads` 逐篇一条，`verdict` 三选一：`used` 用了、`duplicate` 与已读重复、
  `no-increment` 读完无增量；
- `sources` = 结论里真正引用过的笔记（`reads` 的子集）；
- `stop.reason` 与 SKILL.md 的三条停机准则一一对应。
