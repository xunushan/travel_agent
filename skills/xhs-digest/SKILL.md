---
name: xhs-digest
description: 当需要理解一篇已下载的小红书笔记、将其内容提炼为本地存档，或针对具体问题从已有笔记存档生成整理报告时调用。
---

# 小红书笔记理解与提炼

## 技能概述

接受已下载的笔记目录及可选的问题与要求，理解正文、图片或视频及评论，提炼与笔记主题相关、对决策有价值的信息，生成 `digest/` 存档。提供问题时，基于存档额外生成笔记整理报告。

## 输入与路径

- **笔记目录**：包含下载器保存的 `note.json`、`comments.json` 及 `images/` 或 `videos/`。
- **问题与要求**：可选；未提供时仅生成或复用存档。

执行时将 `$SITE` 设为当前 skill 根目录、`$N` 设为笔记目录；视频管线另将 `$V` 设为选中的视频文件，均使用绝对路径。Python 脚本以 `python3 -I` 运行，笔记与输出目录通过参数传入。

## 执行流程

### 1. 判断存档状态

以本地 `digest/` 判断是否已解析，已有存档优先复用。

| 条件 | 处理 |
|---|---|
| 已有 `digest/`，未提供问题 | 复用已有存档，不重新执行管线 |
| 已有 `digest/`，提供了问题 | 读取 `digest/*.md`，按问题生成报告 |
| 尚无 `digest/`，`note.json` 的 `type` 为 `image` | 执行图文管线 |
| 尚无 `digest/`，`note.json` 的 `type` 为 `video` | 执行视频管线 |

### 2. 理解并保存笔记

根据笔记类型仅加载对应管线，按步骤执行；写文档时再读取对应模板。

| 场景 | 执行文档 |
|---|---|
| 图文笔记 | [image-pipeline.md](references/image-pipeline.md) |
| 视频笔记 | [video-pipeline.md](references/video-pipeline.md) |

正文先读，图片或视频随后理解；视频首次理解同时保存有地点依据的代表性景点画面，评论在精读之后处理；最后综合全部有效信息生成 `note.md`。提供问题时，充分利用其中的目的地、时间、路线与关注事项等线索，在笔记主题范围内优先提炼和总结与问题相关的信息。

| 输出 | 模板 |
|---|---|
| `digest/image.md` | [format-image.md](references/format-image.md) |
| `digest/video.md` | [format-video.md](references/format-video.md) |
| `digest/comment.md` | [format-comment.md](references/format-comment.md) |
| `digest/note.md` | [format-note.md](references/format-note.md) |

### 3. 按问题生成报告

提供问题时，优先使用存档，按 [format-report.md](references/format-report.md) 输出报告。

#### 信息提炼要点

- **相关总结**：仅使用本篇笔记中与当前问题相关的信息，按问题组织结论，写清内容与适用条件，不列覆盖清单，不比较其他笔记。
- **关键素材**：只选与问题直接相关、内容清晰且能支持判断或展示的图片与关键帧；同类或重复内容择优保留，每项须有明确用途。在满足用户要求的前提下，最小化素材数量；能用一张说明的，不保留多张，不列全量素材、不凑数。
- **配图选择**：优先根据 `image.md` 的“附录：全部图片”描述挑选；描述不足以判断时，再阅读已有低清拼图，不读取原图或重新执行管线。视频信息帧根据 `video.md` 中已保留的关键帧说明选择，景点照根据“附录：风景画面”选择；旧存档没有风景附录时不重跑视频补图。
- **缺口与待确认**：仅列会改变决策的不确定事项，说明还需确认什么及相关依据，按重要性排列。
- **信息不足的处理**：存档不足时，允许回读 `note.json` 或 `comments.json` 中相关内容，尽量减少回读；不作为存档增补。图片与视频理解直接复用已有结果，不重新执行管线或重做精读，仍不足则写入报告缺口。

#### 报告输出要求

- 按模板保留“与问题相关的总结”“关键素材”“缺口与待确认”三节。
- 总结写出结论本身并注明来源，使读者无需翻阅存档即可理解本篇提供的相关信息。
- 素材列出本地路径与用途；无有效素材或缺口时简要说明，不补齐形式。
- 问题变化时重新生成报告，笔记存档继续复用。

## 依赖

| 用途 | 依赖 |
|---|---|
| 拼图 | `scripts/sheet.py`，Python 3 与 Pillow |
| 视频探测与抽帧 | `scripts/probe.py`、`scripts/frame_grab.py`，ffmpeg 与 ffprobe |
| 音频转写 | `bin/llama-funasr-sensevoice`、`models/sensevoice-small-q8.gguf`、`models/fsmn-vad.gguf` |

视频转写前按视频管线检查依赖：

```bash
bash "$SITE/scripts/setup-asr.sh"
```

脚本可重复执行，`--check` 仅检查。自动安装失败时按 [asr-setup.md](references/asr-setup.md) 处理；仍无法安装则说明缺口并停止，不替换为云端转写。

脚本负责拼图、抽帧和转写，内容理解与筛选由模型完成。

## 产出结构

```text
notes/<noteId>_<标题>/
  note.json                   # 下载器原始笔记
  comments.json               # 已采集评论
  images/                     # 图片原件
  videos/                     # 视频原件
  digest/
    note.md                   # 笔记提炼，供检索和快速判断
    comment.md                # 评论补充与争议
    image.md                  # 图文笔记的图片提炼
    video.md                  # 视频笔记的视频提炼
    assets/                   # 视频长期素材
      asr-raw.txt             # 原始转写
      asr-corrections.md      # 校正记录
      transcript.md          # 校正后全文
      frames/                # 最终保留的信息关键帧
      scenery/               # 代表性景点画面，video.md 附录说明
    scratch/                  # 拼图、探测帧、候选帧、字幕带、音频及临时裁切
```

## 纪律

- **原件不改写**：保留下载器文件、图片与视频原件；图片引用 `images/` 原路径，原始 ASR 转写不改写。
- **中间产物只落 `digest/scratch/`**：最终提炼文档落 `digest/`，长期视频素材落 `digest/assets/`；笔记根目录不留自生成文件。
- **只读取指定内容**：按当前步骤的输入读取，不加载无关文件，例如 `downloads.json`。
- **存档优先复用**：问题变化不触发重复解析，不重跑图片或视频管线，不重做已有视觉理解。
