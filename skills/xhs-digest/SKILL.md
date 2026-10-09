---
name: xhs-digest
description: 把一篇已下载的小红书笔记解析成本地存档 digest/——图片/视频里的信息被定位、理解、整理一次，永不重做；给了问题时再出一份笔记整理报告。图文走拼图探查＋图卡定点精读，视频走 ASR 转写＋字幕带粗扫＋图卡定点密抽，评论按补充与校正处置。用在"把一篇笔记读懂、把图和视频里的信息落到本地、以后按问题取用"上。
---

# 小红书笔记解析

给一篇**已下载**的笔记，产出**解析存档 `digest/`**；调用时给了"问题与要求"，再额外产出一份
**报告**。问题可缺省，缺省则只存档。

**不做**：搜索、下载（xhs-downloader）；跨笔记汇总、本地检索与取舍（xhs-search）。
**不做**：`xhs.db` 的 digests 索引表登记。

## 依赖

全部依赖都在本 skill 目录内，路径相对 skill 根目录：

| 依赖 | 路径 |
|---|---|
| 拼图 | `scripts/sheet.py`（python3 + Pillow） |
| 抽帧／字幕带裁剪拼接 | `scripts/probe.py`、`scripts/frame_grab.py`（ffmpeg / ffprobe） |
| **视频转写**（仅视频笔记） | `bin/llama-funasr-sensevoice` ＋ `models/sensevoice-small-q8.gguf`、`models/fsmn-vad.gguf` |

**跑视频管线前先自检依赖**，缺什么它会装什么：

```bash
bash scripts/setup-asr.sh          # 幂等；--check 只自检
```

装不出来的（缺 cmake／hf、编译环境坏）按 [references/asr-setup.md](references/asr-setup.md) 手工处理。
**ASR 缺失时不要退回云端转写**，改为报告缺口并停止。

脚本一律用绝对路径、`python3 -I` 运行，笔记目录与产出目录作参数传入：

```bash
SITE="$HOME/.claude/skills/xhs-digest"   # 仓库里换成 .../travel_agent/skills/xhs-digest
N="$HOME/Downloads/notes/<noteId>_<标题>"
```

## 按需加载

| 需要什么 | 读哪份 |
|---|---|
| 一步步跑完一篇**图文**笔记 | [references/image-pipeline.md](references/image-pipeline.md) |
| 一步步跑完一篇**视频**笔记 | [references/video-pipeline.md](references/video-pipeline.md) |
| 装视频管线的 ASR 依赖 | [references/asr-setup.md](references/asr-setup.md) |
| 写 `digest/note.md` | [references/format-note.md](references/format-note.md) |
| 写 `digest/comment.md` | [references/format-comment.md](references/format-comment.md) |
| 写 `digest/image.md` | [references/format-image.md](references/format-image.md) |
| 写 `digest/video.md` | [references/format-video.md](references/format-video.md) |
| 出报告（**仅当给了问题**） | [references/format-report.md](references/format-report.md) |

## 产出结构

```text
notes/<noteId>_<标题>/
  digest/
    note.md       # 笔记提炼（轻）
    comment.md    # 评论提炼
    image.md      # 图片提炼（仅图文笔记）
    video.md      # 视频提炼（仅视频笔记）
    assets/       # 长期保留的产物
      asr-raw.txt #   原始转写（底账，不改动）
      asr-corrections.md # ASR 校正记录（一次性校对账，不写进 video.md）
      transcript.md # 校正后文稿（视频的可读全文）
      frames/     #   视频关键帧
    scratch/      # 管线中间产物，删掉可重跑
      contact-*.jpg  # 拼图
      probe-*.jpg    # 探测帧
      sheet_*.jpg    # 字幕带长图
  images/         # 图卡与美照留在原地，image.md 引用原路径，不复制
  videos/         # 下载器落下的视频原件
```

**笔记根目录不留任何自己产出的文件**——中间产物一律进 `digest/scratch/`。

**盘面为准：`digest/` 目录在 = 解析过。**

## 路由表

| 条件 | 去向 |
|---|---|
| 已有 `digest/` 且给了问题 | 跳过管线 → 读 `digest/*.md`（文本细节不够重读 `note.json`/`comments.json`）→ 出报告 |
| 已有 `digest/` 且未给问题 | 什么都不做（已存档） |
| `note.json` 的 `type` = `image` | 图文管线 → [references/image-pipeline.md](references/image-pipeline.md) |
| `note.json` 的 `type` = `video` | 视频管线 → [references/video-pipeline.md](references/video-pipeline.md) |
| 存档不足以回答当前问题 | **定向补读**（图/视频不够→补读对应素材；文本不够→重读原文）→ 按"增补只增不改"增补存档 → 再出报告；补读也不够 → 写进报告缺口 |

脚本只承担**机械变换**（拼图、抽帧、转写）；**所有阅读与判断由多模态模型完成，不经过 OCR**。

## 硬性不变量

1. **图片/视频的理解永不重做**：有 `digest/` 就不再跑管线，哪怕问题变了——不够就定向补读、增补。
2. **读图用多模态直接看，不用 OCR 文本代替**。图上真正的信息量在版式关系里
   （"距离数字挨着哪个站名""哪个站标了「下」"），OCR 给的是没有位置关系的散词。
3. **信息图卡全部精读，记录照片一律不精读**；判型在第②步的拼图上完成，**不依赖正文提没提**。
4. **逐张判型必须写全**，不许只写挑出来的那几张——没读的素材也要登记。
   判型表写在 `image.md` 最末尾的**附录**里，不占正文位置；**记录照片不进正文**。
5. **落点**：机械中间产物（拼图、探测帧、字幕带长图、临时裁剪放大图）只落
   `digest/scratch/`；经理解加工、会被长期引用的产物（原始转写、校正记录、校正后文稿、
   视频关键帧）落 `digest/assets/`。**笔记根目录不留任何自己产出的文件。**
6. **正文先行**：只读 `note.json` 的正文（＋标题），先别碰同目录其它文件，这一步要**定出主旨**，
   后面所有图/视频的理解都只取能体现主旨的内容。**评论排在精读之后**。
   `downloads.json` 不进上下文。
7. **读图是提炼，不是流水账**：先定主旨，再按主旨分 2–4 个方面取内容
   （导览图＋路线主旨 → 路线、上下车标注）；**与主旨无关的细节一律不取**。
8. **全篇站在读者角度写**：只写"读者能拿去用的信息"，不写方法、不写过程——不写用了哪几张图、
   怎么判的型、为什么这么取，不引内部产物路径，不出现「本篇的主旨是…」这类元叙述。
   写成文本后要**一眼可读**：逐项属性写成「名称＝值」并列，不要拆成两列靠位置对应。
   拿不准某条该不该写，就问"读者拿它做什么"，答不上来就不写。
9. **视频关键帧全片 ≤10 帧，只留信息最全、无遮挡的那一帧**；同一张图的后续批注层不单独留帧，
   内容并进底图那一帧。帧多了说明定位没做准，回去收窄窗口。
10. **ASR 的地名必须回画面校正**：同音字，同一份转写里同一地名可能三种拼法，**不能靠词频选写法**。
    校正记录单独写 `digest/assets/asr-corrections.md`，**不进 `video.md`**。
11. **`note.md` 最后写，综合正文与图/视频两侧**：图文笔记综合正文＋`image.md`，视频笔记综合
    正文＋`video.md`。一句话总结、关键信息、内容标签都要用上从图卡/关键帧/口播里读到的东西。
12. **`note.md` 不记采集账**：采集异常（评论没抓全、视频没声音、某张图打不开）不进头部信息栏，
    影响取用的写进报告 §3；也不写素材形态（图卡、截图、视频这类词）。**存疑**只写在
    `image.md` / `video.md`（图上没交代、自相矛盾、与正文冲突），**一句一条**。
    信息不全不记。
13. **增补只增不改**：补读产出增补进对应文件并注明增补日期与触发问题；发现旧内容错误才改，
    改时保留原说法与更正理由。
14. **报告不写建议搜索词**（构造关键词是 xhs-search 的职责）。
15. **问题线索指导提炼，但不设限**：给了问题时，问题里的关键词（目的地、季节、天数、
    要点的类型）就是该重点挖、重点写的方位；但**提炼范围不由问题划死**，与问题无关、
    读者确实用得上的信息照样提炼。缺省无问题时按笔记自身主旨提炼。
