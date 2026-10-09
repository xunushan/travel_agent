#!/usr/bin/env python3
"""按时间戳抽帧 → 关键帧落 digest/assets/frames/。单帧回捞与窗口密抽都用它。

用法：
    # 单帧 / 多帧（--at 可重复，也可逗号分隔）
    python3 -I frame_grab.py <video> <out_dir> --at 305,308,312
    python3 -I frame_grab.py <video> <out_dir> --at 305 --at 312

    # 窗口密抽（ASR 元话语命中后向后开窗）
    python3 -I frame_grab.py <video> <out_dir> --window 305:365 --step 3

    # 小字看不清时裁切放大（机械变换，用来核对图上数字/单位）
    python3 -I frame_grab.py <video> <out_dir> --at 393 --crop 262:150:0:955 --scale 4

产出：
    <out_dir>/tNNNN.jpg（t + 秒数四位；裁切/放大的加 _crop / _x4 后缀）
"""

import argparse
import subprocess
import sys
from pathlib import Path


def parse_at(values):
    times = []
    for value in values or []:
        for piece in value.split(","):
            piece = piece.strip()
            if piece:
                times.append(float(piece))
    return times


def parse_window(spec, step):
    try:
        start_s, end_s = spec.split(":")
        start, end = float(start_s), float(end_s)
    except ValueError:
        sys.exit(f"--window 要写成 START:END（秒），收到 {spec!r}")
    if step <= 0:
        sys.exit("--step 必须为正")
    times, t = [], start
    while t <= end + 1e-6:
        times.append(round(t, 3))
        t += step
    return times


def grab(video, t, out, crop, scale):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1"]
    if crop:
        cmd += ["-vf", f"crop={crop},scale=iw*{scale}:ih*{scale}:flags=lanczos"]
    elif scale != 1:
        cmd += ["-vf", f"scale=iw*{scale}:ih*{scale}:flags=lanczos"]
    cmd += ["-q:v", "2", str(out)]
    subprocess.run(cmd, check=True)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("video", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--at", action="append", help="秒数，逗号分隔，可重复")
    ap.add_argument("--window", help="START:END（秒），配合 --step 密抽")
    ap.add_argument("--step", type=float, default=3.0)
    ap.add_argument("--crop", help="W:H:X:Y，裁切后再放大")
    ap.add_argument("--scale", type=float, default=1.0, help="放大倍数，默认 1")
    args = ap.parse_args()

    times = parse_at(args.at)
    if args.window:
        times += parse_window(args.window, args.step)
    if not times:
        sys.exit("至少要给 --at 或 --window")
    times = sorted(set(times))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for t in times:
        suffix = ("_crop" if args.crop else "") + (f"_x{args.scale:g}" if args.scale != 1 else "")
        out = args.out_dir / f"t{int(round(t)):04d}{suffix}.jpg"
        grab(args.video, t, out, args.crop, args.scale)
        print(out)

    print(f"共 {len(times)} 帧 → {args.out_dir}")


if __name__ == "__main__":
    main()
