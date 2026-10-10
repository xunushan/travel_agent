# 本地 ASR 依赖安装

视频转写使用 `llama-funasr-sensevoice` 与两个 GGUF 模型。以下 `$SITE` 为 skill 根目录的绝对路径。

## 1. 检查与安装

```bash
# 仅检查，不下载或编译
bash "$SITE/scripts/setup-asr.sh" --check
# 编译缺失的二进制、下载缺失的模型
bash "$SITE/scripts/setup-asr.sh"
```

脚本不会安装外部工具，缺失时按下表处理后重跑。安装完成后再次执行 `--check` 确认结果。

| 依赖 | 安装方式（macOS） | 使用条件 |
|---|---|---|
| ffmpeg / ffprobe | `brew install ffmpeg` | 音频提取与视频处理 |
| Python 3 / Pillow | 安装 Python 3；`python3 -m pip install pillow` | 拼图 |
| cmake / Git / C++ 编译工具链 | `brew install cmake git`；缺少编译工具时执行 `xcode-select --install` | 自编译二进制 |
| hf CLI | `python3 -m pip install -U huggingface_hub` | 下载模型 |

安装产物位于 `$SITE/bin/` 与 `$SITE/models/`，两者已被 `.gitignore` 排除，每个环境单独安装。

| 文件 | 用途 |
|---|---|
| `bin/llama-funasr-sensevoice` | 本地转写二进制 |
| `models/sensevoice-small-q8.gguf` | 声学模型，约 254 MB |
| `models/fsmn-vad.gguf` | 语音活动检测模型，约 1.7 MB |

## 2. 手动安装

自动安装失败时，仅处理失败项。二进制也可从 [FunASR 官方发行页](https://github.com/modelscope/FunASR/releases) 获取与系统及架构匹配的版本；没有匹配版本时自行编译。

### 2.1 编译二进制

```bash
ASR_SRC="/tmp/funasr-src"
# 目录已有源码时跳过 clone
git clone --depth 1 https://github.com/modelscope/FunASR.git "$ASR_SRC"

ASR_CXX_FLAGS=""
# macOS 显式使用 SDK 中的 libc++ 头文件；Linux 不需要此参数
if [ "$(uname -s)" = "Darwin" ]; then
  ASR_CXX_FLAGS="-isystem /Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1"
fi
cmake -S "$ASR_SRC/runtime/llama.cpp" -B "$ASR_SRC/runtime/llama.cpp/build" \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$ASR_CXX_FLAGS"
cmake --build "$ASR_SRC/runtime/llama.cpp/build" -j4 --target llama-funasr-sensevoice
mkdir -p "$SITE/bin"
cp "$ASR_SRC/runtime/llama.cpp/build/bin/llama-funasr-sensevoice" "$SITE/bin/"
```

macOS 若报 `array file not found`，检查上述 SDK 路径是否包含 `array`；缺失时需修复编译工具链。无需删除系统目录。

### 2.2 下载模型

```bash
mkdir -p "$SITE/models"
hf download FunAudioLLM/SenseVoiceSmall-GGUF sensevoice-small-q8.gguf --local-dir "$SITE/models"
hf download FunAudioLLM/fsmn-vad-GGUF fsmn-vad.gguf --local-dir "$SITE/models"
```

使用上述命令显式下载指定模型，不执行上游仓库的 `download-funasr-model.sh`。

### 2.3 验证安装

```bash
bash "$SITE/scripts/setup-asr.sh" --check
```

二进制的 `--help` 可能输出 `usage:` 后以 1 退出，不单凭该退出码判断安装失败；检查脚本已处理此情况。依赖仍无法就绪时说明缺口并停止，不替换为云端转写。

## 3. 调用与限制

```bash
"$SITE/bin/llama-funasr-sensevoice" \
  -m "$SITE/models/sensevoice-small-q8.gguf" \
  -a "$N/digest/scratch/asr.wav" \
  --vad "$SITE/models/fsmn-vad.gguf" --srt \
  > "$N/digest/assets/asr-raw.txt"
```

`$N` 为笔记目录。音频提取及转写流程见 [video-pipeline.md](video-pipeline.md)。

| 参数 | 含义 |
|---|---|
| `-m` | 声学模型路径 |
| `-a <wav>` / `-f <fbank.bin>` | 音频或特征输入，二选一 |
| `--vad <gguf>` | 启用语音活动检测，切段并生成时间戳 |
| `--vad-maxseg <ms>` | 单段最大长度，默认 30 秒 |
| `--backend cpu\|cuda\|vulkan` | 计算后端，默认 CPU |
| `--srt` / `--ids` / `--keep-tags` | 输出 SRT / 原始 token ID / 保留语言等标签 |

- 音频要求：16 kHz、单声道、PCM16 WAV，使用 ffmpeg 的 `-ac 1 -ar 16000 -c:a pcm_s16le` 提取。
- 转写内容输出到 stdout，诊断信息输出到 stderr；重定向 stdout 保存原始转写。
- 时间戳来自 VAD 切段，不提供词级时间戳，SRT 块可能在句中截断；不恢复标点。
- 地名及专有词易误识别，数字也需核对，按视频管线校正，不直接作为确定信息使用。
