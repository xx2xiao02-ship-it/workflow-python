"""生成首帧 2x2/3x3 结构样图：等大、零间隙、无分割线、无文字。"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw


PALETTES = [
    ((38, 74, 125), (220, 156, 83)),
    ((50, 112, 94), (231, 192, 92)),
    ((101, 62, 125), (224, 122, 94)),
    ((36, 102, 120), (220, 116, 167)),
    ((124, 76, 48), (232, 191, 124)),
    ((53, 89, 137), (119, 197, 180)),
    ((98, 77, 45), (205, 112, 65)),
    ((72, 97, 67), (227, 178, 109)),
    ((92, 65, 105), (136, 190, 222)),
]


def build_sample(side: int, size: int = 960) -> Image.Image:
    if side not in (2, 3):
        raise ValueError("side 只能是 2 或 3")
    if size % side:
        raise ValueError("size 必须能被 side 整除")

    cell = size // side
    canvas = Image.new("RGB", (size, size))
    pixels = canvas.load()
    for index in range(side * side):
        row, column = divmod(index, side)
        start, end = PALETTES[index]
        left, top = column * cell, row * cell
        for y in range(cell):
            ratio_y = y / max(cell - 1, 1)
            for x in range(cell):
                ratio_x = x / max(cell - 1, 1)
                ratio = (ratio_x + ratio_y) / 2
                color = tuple(
                    int(start[channel] * (1 - ratio) + end[channel] * ratio)
                    for channel in range(3)
                )
                pixels[left + x, top + y] = color

        # 只在宫格内部绘制视觉主体；不绘制任何边框、隔断线或标签。
        draw = ImageDraw.Draw(canvas)
        margin = max(cell // 8, 8)
        draw.ellipse(
            (
                left + margin,
                top + margin,
                left + cell - margin,
                top + cell - margin,
            ),
            fill=tuple(min(255, value + 25) for value in start),
        )
        draw.polygon(
            [
                (left + cell // 2, top + margin * 2),
                (left + cell - margin * 2, top + cell - margin),
                (left + margin * 2, top + cell - margin),
            ],
            fill=tuple(max(0, value - 25) for value in end),
        )
    return canvas


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="samples/first_frame_grid_demo")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for side in (2, 3):
        target = output_dir / f"first_frame_grid_{side}x{side}.png"
        build_sample(side).save(target, format="PNG")
        print(f"{target} 960x960")


if __name__ == "__main__":
    main()
