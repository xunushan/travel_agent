#!/usr/bin/env python3
"""视频探测帧：等间隔抽 ~11 帧，供判形态（口播 / 动画？字幕带？图卡？），并打印基本参数。

形态探测帧只用来判形态。**信息型帧的位置不靠它找**——那是 ASR 元话语定位的事
（见 references/video-pipeline.md）。

用法：
    python3 -I probe.py <video> <out_dir> [--count 11]

产出：
    <out_dir>/probe-NNN.jpg   NNN = 该帧时间戳（秒，取整）
    stdout 打印：时长 / 分辨率 / 帧率 / 有无音轨 / 抽出哪些帧
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def ffprobe(video):
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(video),
    ]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("ffprobe not found on PATH")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"ffprobe failed: {exc.stderr.strip()}")

    data = json.loads(raw)
    video_stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        sys.exit(f"no video stream in {video}")

    fps = None
    num, _, den = (video_stream.get("avg_frame_rate") or "0/1").partition("/")
    if float(den or 0):
        fps = round(float(num) / float(den), 3)

    return {
        "duration": float(data["format"]["duration"]),
        "width": video_stream["width"],
        "height": video_stream["height"],
        "fps": fps,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def grab(video, t, out):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{t:.3f}", "-i", str(video),
        "-frames:v", "1", "-q:v", "2", str(out),
    ]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("video", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--count", type=int, default=11)
    args = ap.parse_args()

    info = ffprobe(args.video)
    print(
        f"{args.video.name}  {info['width']}x{info['height']}  "
        f"{info['fps']} fps  {info['duration']:.1f}s  "
        f"音轨={'有' if info['has_audio'] else '无'}"
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    picks = [info["duration"] * (i + 0.5) / args.count for i in range(args.count)]
    for t in picks:
        out = args.out_dir / f"probe-{int(t):03d}.jpg"
        grab(args.video, t, out)
    print("抽帧：" + ", ".join(f"{int(t)}s" for t in picks))


if __name__ == "__main__":
    main()
