#!/usr/bin/env python3
"""拼图探查：把一组图片拼成低清角标总览，供多模态模型一次判型、选片。

只做机械变换——排序、缩放、拼接、编号——不做任何判断。

用法：
    python3 -I sheet.py <images_dir> <out_dir> [--prefix contact] [--cell-w 480]
                        [--max-w 2400] [--max-h 1600] [--aspect 1.33] [--quality 82]

产出：
    <out_dir>/<prefix>-1.jpg, <prefix>-2.jpg, …
    每格左上角一个数字角标，编号从 1 起，顺序 = 文件名排序后的顺序
    （即角标 N 对应第 N 个文件，用于定位候选原图，不生成逐张判型表）。
    图多时自动拆张；单张不超过 <max-w>×<max-h>。

拼图只用于判型与选片。只有通过主题与读取目的判断的候选，才按角标定位另读原图，
不要在拼图格上认字。
"""

import argparse
import statistics
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10
        return ImageFont.load_default()


def collect(images_dir, pattern):
    files = [
        p
        for p in sorted(images_dir.glob(pattern))
        if p.is_file() and p.suffix.lower() in EXTS
    ]
    if not files:
        sys.exit(f"no images under {images_dir} matching {pattern!r}")
    return files


def median_aspect(files, sample=12):
    """样本高宽比的中位数——决定格子多高，让拼图层数不至于失控。"""
    ratios = []
    for path in files[:sample]:
        with Image.open(path) as im:
            w, h = im.size
        if w:
            ratios.append(h / w)
    return statistics.median(ratios) if ratios else 1.33


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("images_dir", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--glob", default="*", help="挑文件的通配符，默认全部")
    ap.add_argument("--prefix", default="contact")
    ap.add_argument("--cell-w", type=int, default=480, help="格宽，默认 480 px")
    ap.add_argument("--max-w", type=int, default=2400, help="单张拼图宽度上限")
    ap.add_argument("--max-h", type=int, default=1600, help="单张拼图高度上限")
    ap.add_argument(
        "--aspect",
        type=float,
        default=None,
        help="格高 = 格宽 × 该值；默认取样本中位高宽比",
    )
    ap.add_argument("--quality", type=int, default=82)
    args = ap.parse_args()

    files = collect(args.images_dir, args.glob)
    cell_w = args.cell_w
    cell_h = int(round(cell_w * (args.aspect or median_aspect(files))))
    cols = max(1, args.max_w // cell_w)
    rows = max(1, args.max_h // cell_h)
    per_sheet = cols * rows

    args.out_dir.mkdir(parents=True, exist_ok=True)
    font = load_font(max(16, cell_w // 12))
    pad = max(6, cell_w // 32)

    sheets = 0
    for start in range(0, len(files), per_sheet):
        chunk = files[start : start + per_sheet]
        n_rows = -(-len(chunk) // cols)
        canvas = Image.new("RGB", (cols * cell_w, n_rows * cell_h), (24, 24, 24))
        draw = ImageDraw.Draw(canvas)
        for i, path in enumerate(chunk):
            n = start + i + 1
            r, c = divmod(i, cols)
            x0, y0 = c * cell_w, r * cell_h
            with Image.open(path) as im:
                im = im.convert("RGB")
                im.thumbnail((cell_w, cell_h), Image.LANCZOS)
                canvas.paste(
                    im,
                    (x0 + (cell_w - im.width) // 2, y0 + (cell_h - im.height) // 2),
                )
            draw.rectangle(
                [x0, y0, x0 + cell_w - 1, y0 + cell_h - 1], outline=(90, 90, 90)
            )
            label = str(n)
            tb = draw.textbbox((0, 0), label, font=font)
            bx0, by0 = x0 + pad, y0 + pad
            bx1 = bx0 + (tb[2] - tb[0]) + 2 * pad
            by1 = by0 + (tb[3] - tb[1]) + 2 * pad
            draw.rectangle([bx0, by0, bx1, by1], fill=(0, 0, 0))
            draw.text(
                (bx0 + pad - tb[0], by0 + pad - tb[1]),
                label,
                font=font,
                fill=(255, 235, 59),
            )
        sheets += 1
        out = args.out_dir / f"{args.prefix}-{sheets}.jpg"
        canvas.save(out, "JPEG", quality=args.quality)
        print(
            f"{out}  {canvas.width}x{canvas.height}  "
            f"{len(chunk)} 格  (角标 {start + 1}–{start + len(chunk)} / 共 {len(files)})"
        )

    print(f"共 {len(files)} 张 → {sheets} 张拼图；格 {cell_w}x{cell_h}，每张最多 {per_sheet} 格")


if __name__ == "__main__":
    main()
