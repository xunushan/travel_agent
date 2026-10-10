# 图片 OCR 实现方案（macOS Vision，本地零成本）

> **用途**：记录一个本机能力——**macOS 不用下载任何模型/语言包就能做中文 OCR**（系统自带 Vision 框架）。
> **定位**：**能力备查，不是 xhs-digest 的必需环节**。图片笔记的"哪张图值得看"由拼图探查回答、"这张图说了什么"由多模态模型回答，都不经过 OCR；理由见 §5。
> **实测样本**：`notes/696117b2000000001a01e6ef/images/`（19 张 webp，1440×1080 / 1080×1440）。

---

## 1. 结论

| 项 | 结论 |
|---|---|
| 选型 | **macOS Vision**（Swift 直调系统框架）——离线、零依赖、零 API 成本、中文可用 |
| 不选 tesseract | 本机 `tesseract --list-langs` 只有 `eng` / `osd` / `snum`，**中文不可用**；要装 `tesseract-lang` |
| 实测识别效果 | 19 张里只有 2 张（002、019，同一张导览图）出文字，其余 17 张返回 `[]`（都是风景照），与人工判断一致 |
| 耗时 | 19 张 ≈ **2 分钟以上**（本次超过 120 s 前台超时阈值，转后台完成）→ **必须后台跑** |
| 成本 | **0 token**（本地进程，不占模型上下文） |

---

## 2. 为什么不用 tesseract

```console
$ tesseract --list-langs
List of available languages in "/usr/local/share/tessdata/" (3):
eng
osd
snum
```

中文需要另外下载语言包（`brew install tesseract-lang`，或单独取 `chi_sim.traineddata`）。而 Vision 是 macOS 10.15+ 的系统框架，**装了 Xcode Command Line Tools 就能直接调**，不需要任何安装：

```console
$ which swift
/usr/bin/swift
```

对"只需要一个分词器来判断有无文字"这个需求，系统自带的方案更合适：不引入依赖、不挑网络、不占额外磁盘。

---

## 3. 实现

### 3.1 脚本

```swift
// ocr.swift —— 用法：swift ocr.swift <图片目录>
import Foundation
import Vision
import AppKit

let dir = CommandLine.arguments[1]
let exts = [".webp", ".png", ".jpg", ".jpeg"]
// 注意：扩展名白名单必须显式列出——见 §4.1，漏掉会导致"静默返回 0 结果"
let files = (try! FileManager.default.contentsOfDirectory(atPath: dir))
    .filter { f in exts.contains { f.lowercased().hasSuffix($0) } }
    .sorted()

for f in files {
    guard let img = NSImage(contentsOfFile: dir + "/" + f),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }

    let req = VNRecognizeTextRequest()
    req.recognitionLanguages = ["zh-Hans", "en-US"]   // 顺序影响语言打分
    req.recognitionLevel = .accurate                  // .fast 会明显掉字
    req.usesLanguageCorrection = false                // 见 §4.5

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    try? handler.perform([req])

    let lines = (req.results ?? []).compactMap { obs in
        obs.topCandidates(1).first.map { ($0.string, $0.confidence) }
    }
    let label = f.replacingOccurrences(of: ".webp", with: "")
                    .split(separator: "-").last.map(String.init) ?? f

    if lines.isEmpty {
        print("\(label)\t[]")                          // 空数组 = 判定为风景照
    } else {
        let joined = lines.map { "\($0.0)(\(String(format: "%.2f", $0.1)))" }
                          .joined(separator: " | ")
        print("\(label)\t[\(lines.count)] \(joined)")
    }
}
```

### 3.2 调用

```bash
swift /path/to/ocr.swift <notes/<noteId>/images>          # 前台，适合 ≤ 5 张
swift /path/to/ocr.swift <notes/<noteId>/images> > out.txt &   # 批量：放后台
```

### 3.3 输出格式

每张图一行，`编号 → [识别到的行数] 文字(置信度) | 文字(置信度) | …`：

```text
001	[]
002	[93] 九寨沟简易导览图(0.50) | 原始森林（17Km）(0.50) | 长海（18Km）(1.00) | 海拔3010m(1.00) | …
…
018	[]
019	[90] 九寨沟简易导览图(1.00) | 长海（18Km）(0.50) | …
```

**`[]` 就是分流结果**：这一行是风景照，不需要进精读队列；非空的进队列。

### 3.4 关键参数

| 参数 | 取值 | 说明 |
|---|---|---|
| `recognitionLanguages` | `["zh-Hans", "en-US"]` | 顺序影响打分；简体优先 |
| `recognitionLevel` | `.accurate` | `.fast` 快但掉字，分流场景不必省这点时间 |
| `usesLanguageCorrection` | `false` | 见 §4.5 |

---

## 4. 踩过的坑

### 4.1 扩展名白名单写死 → 静默返回 0 结果

第一版脚本写的是 `filter { $0.hasSuffix(".webp") }`。我把放大后的导览图存成 `map2x.png` 再跑，**脚本打印了 0 行、退出码 0、没有任何报错**，看起来像"这张图没有文字"。

这是一个会**伪装成正常结果**的 bug——在分流场景里，它的表现恰好是最危险的那种："我判定这张图是风景照"。对策：扩展名显式白名单（§3.1），且对**空输出做断言**（正常至少应有 1 行/图）。

### 4.2 描白／彩色文字会被静默漏掉

导览图上的路线序号是"橙色填充 + 白色描边"的数字圈，全图 OCR 只抓到 2 个（`7`、`13`），剩下十几个**一个字都没返回**。放大 2× 后仍然只多出一个 `2`。

**这条对"这图有没有文字"的二值判断影响不大**，但它说明了一件事：**OCR 的召回率在装饰性文字上很低，不能拿"OCR 没结果"推断"这里没信息"**。

三档对策，按成本递增：

| 对策 | 做法 | 效果（本次实测） |
|---|---|---|
| 放大 | LANCZOS 2× 后重跑 | 只多出 1 个数字 |
| 色彩通道分离 | `R − B` 阈值化，单独抽出橙色 | 抽到了 `2`，但描边被吃掉，其余仍丢失 |
| **直接交给多模态模型看** | 裁切小区域送模型 | **一次就看清了**（本次绕了弯路，见 §5） |

### 4.3 "返回空"有两种含义

- **真·风景照**（无文字）：17 张，符合预期；
- **有文字但 OCR 没抓到**：导览图上的数字圈就是这一类。

所以 OCR 输出的 `[]` 只能作为**"优先级低"的信号，不能作为"排除"的依据**——最终"哪张图值得看"还是得自己看拼图来定。

### 4.4 小图质量下降

`019.webp`（640×853）是 `002`（1080×1440）的降采样版，OCR 出来的错字明显更多（`则查注沟`、`原始国火2003`、`節竹海`）。分流场景够用（照样能判出"是图卡"），但**不要拿小图的 OCR 文本当数据源**。

### 4.5 不要让语言纠错去"修"专有名词

`usesLanguageCorrection = true` 会用语言模型纠正结果，对通顺句子有帮助，但对**地名**是有害的：九寨沟的 `则查洼沟`、`色嫫头像观测点` 这类词在语言模型里本来就低概率，容易被"纠"成常用词。这与视频管线里 ASR 对专有名词几乎必输的失效模式**完全同构**。

本次取 `false`。若要在纯文字场景下开，应先在地名词表上做回归测试。

---

## 5. 为什么 xhs-digest 最终不用它

三条理由：

1. **省 token 的杠杆不在 OCR**：真正的浪费是"每张图都读一次原图"，而这个问题由**拼一张小图自己看**解决——一张 2400×1440 的拼图 ≈ 4.6 k，替代 19 次原图调用（≈ 38 k）。拼图探查比 OCR 更省事，也更直观（它同时给出了"画的是什么景"，OCR 给不了）。
2. **OCR 给的是散字，不是结构**：导览图的「上／下」标注、箭头走向、图例对应关系、"哪个站名挨着哪个距离数字"，只有看图才能还原；OCR 输出是一串没有坐标关系的词。
3. **多模态模型在同样的图上读得更准**：本次把导览图整张送多模态模型，一次就拿到了站点距离、三条沟长度、海拔、上下车点标注和路线序号圈——**而这条路我原本用 OCR + 放大 + 色彩分离绕了三步才走通，还没走通**（§4.2）。

所以职责划分是：

| 环节 | 用什么 | 产出 |
|---|---|---|
| 探查（哪几张值得看 / 哪张是图卡） | **多模态模型看拼图** | 候选清单 |
| 精读（这张图卡说了什么） | **多模态模型看原图** | 结构化信息 |

OCR 在这条链路里没有位置。把它写进文档是因为它**零依赖、够准、以后别的场景用得上**（例如纯文字截图的批量抽取、或需要把图内文字变成可检索字段时）——但不要在"理解图片"这件事上用它替代自己看。

---

## 6. 工程备注

- 脚本放在**下载目录之外**（本次在 `/tmp/xhs-img-practice/`），图片目录只作为参数传入；
- 需要 Python/PIL 的配套脚本（拼图、放大、色彩通道）用 `python3 -I` 运行——隔离模式不加载脚本目录与当前目录的 `site-packages`/`json.py` 之类，避免被下载目录里的同名文件劫持；
- 批量跑记得重定向到文件并放后台（§3.2），前台 120 s 会超时；
- 换机器时先跑 `which swift` 和 `tesseract --list-langs` 确认前提（§2）。

---

## 7. 相关文档

- [xhs-digest 设计方案](xhs-digest-设计方案.md) ——图片笔记的读法管线（**不使用 OCR**：探查靠拼图、阅读靠多模态）
- 《视频理解方案调研》——视频侧的 ASR/OCR 分工，与本方案的 OCR 定位同构
