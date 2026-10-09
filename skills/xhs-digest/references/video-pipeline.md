# 视频管线（五步）→ video.md + note.md

**绝不能逐帧看画面**：450 s / 30 fps，按 1 fps 抽帧就约 54 万 token。

---

## ① ASR 转写

先用 `bash scripts/setup-asr.sh` 自检依赖（装法见 [asr-setup.md](asr-setup.md)）。

```bash
V=$(ls "$N"/videos/*.mp4)
mkdir -p "$N/digest/assets/frames" "$N/digest/scratch"

ffmpeg -hide_banner -loglevel error -y -i "$V" -vn -ac 1 -ar 16000 -c:a pcm_s16le \
  "$N/digest/scratch/asr.wav"

"$SITE/bin/llama-funasr-sensevoice" \
  -m "$SITE/models/sensevoice-small-q8.gguf" \
  -a "$N/digest/scratch/asr.wav" --vad "$SITE/models/fsmn-vad.gguf" \
  --srt > "$N/digest/assets/asr-raw.txt"
```

raw 转写**不改动**，留作底账。

## ② 形态判别

```bash
python3 -I "$SITE/scripts/probe.py" "$V" "$N/digest/scratch/probe"
```

看这 ~11 帧回答三件事，**形态决定方法**：

| 判别 | 影响 |
|---|---|
| 口播 talking-head？动画？实拍？ | 信息在不在风景里——不在就**不要按信息节点抽风景帧** |
| 有没有**烧录字幕带**？位置在哪？ | 有 → 字幕 = 作者原话，是主证据，走第③步；无 → 只靠 ASR ＋ 在 ASR 报数字/名单/时间处抽帧核对 |
| 有没有**信息型图卡**？ | 有 → 这是核心资产，走第④步定点密抽 |

探测帧是等距盲抽，**只用来判形态**，不用来找图卡——图卡位置一律由第④步的 ASR 定。

## ③ 字幕带拼接粗扫（有字幕带时）

字幕带只占整帧的 1/6 左右，裁出来读性价比高（720×180 = 182 token/帧，整帧 1196 token）。

```bash
# 几何要从探测帧上量：crop=W:H:X:Y，取含字幕的那条横带
# 例：720x1280 的视频，字幕在下部 → crop=720:180:0:935
ffmpeg -hide_banner -loglevel error -y -i "$V" \
  -vf "select='not(mod(n,90))',crop=720:180:0:935,tile=1x12" -vsync 0 \
  "$N/digest/scratch/sheet_%02d.jpg"
```

`mod(n,90)` = 每 3 秒采 1 帧（30 fps 下）；`tile=1x12` = 12 帧拼一张长图（每张覆盖 36 s）。
**一次 ffmpeg 调用产出全片长图**，再逐张 Read。

间隔按需要的精度选（每张长图 ≈2k token）：

| 间隔 | 长图数（450 s 片长） | token |
|---|---|---|
| 5 s | 8 | ~16k |
| **3 s（默认）** | 13 | ~26k |
| 2 s | 19 | ~39k |
| 1 s | 38 | ~77k |

**3 s 会漏句**（单句屏显只有 1.8–2.8 s），漏掉的句子按 ASR 时间戳只回捞那一帧字幕带
（182 token/帧）：

```bash
python3 -I "$SITE/scripts/frame_grab.py" "$V" "$N/digest/scratch" --at 123 --crop 720:180:0:935
```

校正后的全文写 `digest/assets/transcript.md`（=`video.md` 的"可读全文"），
校正依据逐条记在 `digest/assets/asr-corrections.md`。

## ④ 图卡定点密抽

**用 ASR 的"元话语"反向定位图卡**——作者往画面上放图卡之前，嘴上那句话就是信号：

> 汇总一下 / 截图保存 / 整理了一下 / 这张图 / 这张表 / 下面看 / 路线怎么规划 / 最后看一下 / 我标出来了

命中就取时间戳，**向后开 60 s 窗口按 2–3 s 密抽**：

```bash
python3 -I "$SITE/scripts/frame_grab.py" "$V" "$N/digest/assets/frames" --window 305:365 --step 3
```

**帧数纪律（硬规则）**：

- 全片关键帧 **≤10 帧**；**只保留信息最全、无遮挡的那一帧**；
- **静态图卡**（汇总表这类，内容不变）：**先抽 1 帧试读，读不全再补**；
- **动态图卡**（底图固定、批注随讲话叠加）：**只留底图那一帧**，后来叠上去的批注
  **不单独留帧**，把新增文字并进底图帧的「关键内容」里；口播里已经讲过的内容更不单独留帧；
- **留帧看遮挡**：被弹幕、进度条、其他图层压住的帧一律不留；同一张图有多帧可选时留最干净的那一帧；
- 帧多了说明定位没做准，回去收窄窗口。

**精看的只有图卡帧**。其余时间轴由字幕带拼接和 ASR 文本覆盖，不逐帧看画面。

## ⑤ 评论与落笔

按 [评论四类处置](image-pipeline.md#评论的四类处置) → `digest/comment.md`。

最后写 `note.md`，**综合正文 ＋ `video.md`**（见 [format-note.md](format-note.md)）——
一句话总结、关键信息、内容标签都要用上字幕/口播与关键帧里读出来的东西，不能只抄正文。

**调用时给了问题的，落笔前先对着问题线索过一遍**——问题里的关键词（目的地、季节、天数、
要点的类型）指明的方位重点挖、重点写；但**不拿问题去裁剪提炼范围**，
与问题无关、读者用得上的信息照样提炼。

---

## 可信度分层

**作者图卡 > 作者字幕 > 口播 > ASR 转写 > 评论区 AI 摘要。**

- **ASR 对地名几乎必错**，且同一份转写里同一地名可能三种拼法 → **不能靠词频选正确写法，
  必须回画面校正**，校正记录写 `digest/assets/asr-corrections.md`；
- **作者图卡也可能有笔误**：实测有漏字，还有把"米"标成"KM"的整列单位错误——用它的数字前先核量级；
- **图卡与口播几乎不重叠**（图卡给站序/距离/换乘，口播给机位/理由/穿搭/窗口期）——**两边都得用**；
- 图卡与口播不一致时，**以定稿卡片为准并注明**。

---

## 复现清单

| 步骤 | 命令 | 备注 |
|---|---|---|
| 0. 依赖 | `bash "$SITE/scripts/setup-asr.sh"` | 缺 `bin/` 或 `models/` 时先跑 |
| 1. ASR | `ffmpeg` 抽音频 → `bin/llama-funasr-sensevoice --srt` | raw 存 `digest/assets/asr-raw.txt`，不改动 |
| 2. 形态 | `python3 -I "$SITE/scripts/probe.py" "$V" "$N/digest/scratch/probe"` | 判口播/字幕带/图卡，别用探测帧找图卡 |
| 3. 字幕带 | `ffmpeg -vf "select='not(mod(n,90))',crop=…,tile=1x12"` | 几何从探测帧上量；漏句按 ASR 时间戳回捞 |
| 4. 图卡 | `python3 -I "$SITE/scripts/frame_grab.py" "$V" "$N/digest/assets/frames" --window …` | ≤10 帧，同类只留最全的一帧 |
| 5. 评论 | Read `comments.json` | 排在精读之后 |
| 6. 产出 | `note.md` / `video.md` / `comment.md` | 按 `format-*.md` 的结构 |

**落点分三处**：`digest/scratch/`＝中间产物（音频、探测帧、字幕带长图、临时裁剪），删掉可重跑；
`digest/assets/`＝长期保留（`asr-raw.txt` 原始转写、`asr-corrections.md` 校正记录、
`transcript.md` 校正后全文、`frames/` 关键帧）。**笔记根目录不留任何产物。**

> ASR 缺失时**不要退回云端转写**，改为报告缺口并停止。
