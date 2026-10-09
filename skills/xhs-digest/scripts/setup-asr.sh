#!/usr/bin/env bash
# xhs-digest 视频管线依赖自检／安装：FunASR SenseVoiceSmall（llama.cpp，纯本地）
#
#   bash scripts/setup-asr.sh          # 缺什么装什么，已就绪则只做自检
#   bash scripts/setup-asr.sh --check  # 只自检，不下载也不编译
#
# 幂等，可反复执行。安装目标都落在本 skill 目录内：
#   bin/llama-funasr-sensevoice     转写二进制（自编译或已有）
#   models/sensevoice-small-q8.gguf 声学模型  254 MB
#   models/fsmn-vad.gguf            VAD 模型   1.7 MB
set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="$SKILL_DIR/bin/llama-funasr-sensevoice"
MODELS="$SKILL_DIR/models"
SRC="${FUNASR_SRC:-/tmp/funasr-src}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

ok=0 fail=0
# 二进制把 usage 打到 stderr 并以 1 退出，不能用退出码判断可用性。
# 不用管道：pipefail 下 grep -q 提前退出会让上游吃 SIGPIPE，管道整体判失败。
asr_ok() {
  [ -x "$1" ] || return 1
  case "$("$1" --help 2>&1 || true)" in usage:*) return 0 ;; *) return 1 ;; esac
}
say()  { printf '%s\n' "$*"; }
good() { say "  ✓ $*"; ok=$((ok+1)); }
bad()  { say "  ✗ $*"; fail=$((fail+1)); }

say "skill 目录：$SKILL_DIR"

# --- 1. 依赖的外部工具 -------------------------------------------------------
say ""
say "[1/3] 外部工具"
for t in ffmpeg ffprobe; do
  if command -v "$t" >/dev/null 2>&1; then good "${t}（$(command -v "$t")）"
  else bad "$t 缺失 —— 用 brew install ffmpeg 安装"; fi
done
if python3 -c 'import PIL' 2>/dev/null; then good "python3 + Pillow"
else bad "Pillow 缺失 —— 用 python3 -m pip install pillow 安装"; fi

# --- 2. 转写二进制 -----------------------------------------------------------
say ""
say "[2/3] 转写二进制 bin/llama-funasr-sensevoice"
if asr_ok "$BIN"; then
  good "已就绪，可执行"
elif [ "$CHECK_ONLY" = 1 ]; then
  bad "缺失或不可执行：$BIN"
else
  say "  → 从源码编译（macOS 仅 arm64 有预编译包，Intel 机器必须自编译）"
  if ! command -v cmake >/dev/null 2>&1; then
    bad "cmake 缺失 —— 用 brew install cmake 安装后重跑"
  else
    set -e
    [ -d "$SRC" ] || git clone --depth 1 https://github.com/modelscope/FunASR.git "$SRC"
    # macOS 上 CommandLineTools 自带的 libc++ 可能残缺（连 <array> 都找不到），
    # 显式指向 SDK 里的完整版本；Linux 上不需要这个 flag。
    XFLAGS=""
    if [ "$(uname -s)" = "Darwin" ]; then
      SDKCPP=/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk/usr/include/c++/v1
      if [ ! -f "$SDKCPP/array" ]; then
        bad "系统 libc++ 残缺且 SDK 里也找不到 —— 先修好编译环境（见 references/asr-setup.md）"
        exit 1
      fi
      XFLAGS="-isystem $SDKCPP"
    fi
    cmake -S "$SRC/runtime/llama.cpp" -B "$SRC/runtime/llama.cpp/build" \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_FLAGS="$XFLAGS" >/dev/null
    cmake --build "$SRC/runtime/llama.cpp/build" -j4 --target llama-funasr-sensevoice >/dev/null
    mkdir -p "$SKILL_DIR/bin"
    cp "$SRC/runtime/llama.cpp/build/bin/llama-funasr-sensevoice" "$BIN"
    set +e
    if asr_ok "$BIN"; then good "编译完成并已放入 bin/"
    else bad "编译产物不可执行，请人工检查"; fi
  fi
fi

# --- 3. 模型 -----------------------------------------------------------------
say ""
say "[3/3] 模型 models/"
need_dl=0
for f in sensevoice-small-q8.gguf fsmn-vad.gguf; do
  [ -s "$MODELS/$f" ] && good "$f" || { bad "$f 缺失"; need_dl=1; }
done
if [ "$need_dl" = 1 ] && [ "$CHECK_ONLY" = 0 ]; then
  if ! command -v hf >/dev/null 2>&1; then
    bad "hf CLI 缺失 —— 用 python3 -m pip install -U huggingface_hub 安装后重跑"
  else
    mkdir -p "$MODELS"
    hf download FunAudioLLM/SenseVoiceSmall-GGUF sensevoice-small-q8.gguf --local-dir "$MODELS"
    hf download FunAudioLLM/fsmn-vad-GGUF fsmn-vad.gguf --local-dir "$MODELS"
    for f in sensevoice-small-q8.gguf fsmn-vad.gguf; do
      [ -s "$MODELS/$f" ] && good "已下载 $f" || bad "$f 下载失败"
    done
  fi
fi

# --- 结论 --------------------------------------------------------------------
say ""
if [ "$fail" = 0 ]; then say "依赖就绪（$ok 项通过）"; exit 0; fi
say "有 $fail 项未通过，按上面的提示处理后重跑本脚本"
[ "$CHECK_ONLY" = 1 ] && say "（本次是 --check，只自检未安装）"
exit 1
