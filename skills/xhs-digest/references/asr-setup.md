# ASR 依赖安装（仅视频笔记需要）

视频管线要求**纯本地**转写，用的是一份独立的 llama.cpp 二进制
`bin/llama-funasr-sensevoice` ＋ 两个 GGUF 模型。**不要退回云端 ASR。**

首次执行视频管线前，先跑：

```bash
bash scripts/setup-asr.sh          # 缺什么装什么，幂等
bash scripts/setup-asr.sh --check  # 只自检
```

装好后目录应是这样——**`bin/` 与 `models/` 都在 `.gitignore` 里**，不进仓库，每个环境各自生成：

```text
skills/xhs-digest/
  bin/llama-funasr-sensevoice        3.5 MB
  models/sensevoice-small-q8.gguf    254 MB
  models/fsmn-vad.gguf               1.7 MB
```

脚本装不出来的（缺 cmake、缺 hf、编译环境坏），按下文手工处理。

## 1. 外部工具

| 工具 | 安装 |
|---|---|
| `ffmpeg` / `ffprobe` | `brew install ffmpeg` |
| `python3` + Pillow | `python3 -m pip install pillow` |
| `hf` CLI | `python3 -m pip install -U huggingface_hub` |
| `cmake`（仅自编译时需要） | `brew install cmake` |

## 2. 二进制

### 2.1 先看有没有预编译包

FunASR 官方发行页：https://github.com/modelscope/FunASR/releases

**macOS 只发布 arm64 预编译包**，Intel 机器用不了，只能自编译。
Linux / Windows 有 x64 包。

### 2.2 自编译

```bash
git clone --depth 1 https://github.com/modelscope/FunASR.git /tmp/funasr-src

cmake -S /tmp/funasr-src/runtime/llama.cpp -B /tmp/funasr-src/runtime/llama.cpp/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_CXX_FLAGS="-isystem /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1"

cmake --build /tmp/funasr-src/runtime/llama.cpp/build -j4 --target llama-funasr-sensevoice

cp /tmp/funasr-src/runtime/llama.cpp/build/bin/llama-funasr-sensevoice bin/
```

- `sensevoice` 这个 target 只依赖 ggml，编译量最小；configure 约 1 分钟、编译约 6 分钟；
- **那个 `-isystem` 是 macOS 必需的绕行**：CommandLineTools 自带的
  `/Library/Developer/CommandLineTools/usr/include/c++/v1/` 只剩 `__*` 内部目录，
  没有任何标准头文件，clang 的默认搜索路径把它排在 SDK 完整版之前却又不会回退，
  于是连 `#include <array>` 都报 `fatal error: 'array' file not found`。
  指向 SDK 里的完整 libc++ 即可，不改动系统文件。
  顽固修法是 `sudo rm -rf /Library/Developer/CommandLineTools && xcode-select --install`（系统级改动，谨慎）。
  Linux 上不需要这个 flag。

**验证**（该二进制把 usage 打到 stderr 并以 1 退出，**不能看退出码**）：

```bash
bin/llama-funasr-sensevoice --help 2>&1 | head -1     # 应打出 usage: ...
```

## 3. 模型

```bash
mkdir -p models
hf download FunAudioLLM/SenseVoiceSmall-GGUF sensevoice-small-q8.gguf --local-dir models
hf download FunAudioLLM/fsmn-vad-GGUF fsmn-vad.gguf --local-dir models
```

| 文件 | 大小 | 用途 |
|---|---|---|
| `sensevoice-small-q8.gguf` | 254 MB | 声学模型（q8 量化） |
| `fsmn-vad.gguf` | 1.7 MB | 语音活动检测，做长音频切段与时间戳 |

**不要执行仓库里的 `download-funasr-model.sh`**——用上面的 `hf` 命令显式下载。

## 4. 参数与已知限制

调用形如：

```bash
bin/llama-funasr-sensevoice -m models/sensevoice-small-q8.gguf \
  -a audio.wav --vad models/fsmn-vad.gguf --srt
```

| 参数 | 说明 |
|---|---|
| `-m` | 模型路径 |
| `-a <wav>` / `-f <fbank.bin>` | 输入，二选一 |
| `--vad <gguf>` | 启用 VAD 切段（出时间戳） |
| `--vad-maxseg <ms>` | VAD 单段最大长度，默认 30 s |
| `--backend cpu\|cuda\|vulkan` | 默认 cpu |
| `--srt` / `--ids` / `--keep-tags` | 输出 SRT / 原始 token id / 保留 `<\|lang\|>` 等标签 |

- 诊断信息走 **stderr**、正文走 **stdout**，所以 `> out.srt` 拿到的是干净文件；
- 输入只支持 **16 kHz 单声道 PCM16 WAV**（用 `ffmpeg -ac 1 -ar 16000 -c:a pcm_s16le` 抽）；
- **无词级时间戳**，时间戳来自 VAD 切段，精度是句子级；`--srt` 的块边界可能切在句子中间；
- **不做标点恢复**；
- 中文识别：数字、里程、耗时基本正确；**专有名词（地名）错误率约一半**，必须回画面校正
  （见 [video-pipeline.md](video-pipeline.md)）。
