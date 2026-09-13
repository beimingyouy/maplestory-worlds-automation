#!/usr/bin/env python3
"""Crop pixel-exact monster recognition templates from source PNG images."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create sequential guai*.png crops without rescaling or recoloring."
    )
    parser.add_argument("--spec", required=True, type=Path, help="JSON crop specification")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--prefix", default="guai")
    parser.add_argument("--contact-sheet", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def load_spec(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise ValueError("spec must contain a sources list")
    return data


def resolve_source(spec_path: Path, raw_path: str) -> Path:
    source = Path(raw_path)
    if not source.is_absolute():
        source = spec_path.parent / source
    return source.resolve()


def normalized_rect(raw_rect: Iterable[int], image_size: tuple[int, int]) -> tuple[int, int, int, int]:
    values = list(raw_rect)
    if len(values) != 4 or any(isinstance(value, bool) for value in values):
        raise ValueError(f"invalid crop rectangle: {raw_rect!r}")
    x, y, width, height = (int(value) for value in values)
    image_width, image_height = image_size
    if width <= 0 or height <= 0:
        raise ValueError(f"crop width and height must be positive: {values!r}")
    if x < 0 or y < 0 or x + width > image_width or y + height > image_height:
        raise ValueError(
            f"crop {values!r} is outside source bounds {image_width}x{image_height}"
        )
    return x, y, width, height


def save_contact_sheet(images: list[tuple[str, Image.Image]], output_path: Path) -> None:
    if not images:
        return
    scale = 6
    columns = 4
    cell_width = 260
    cell_height = 190
    rows = (len(images) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), (48, 48, 48))
    draw = ImageDraw.Draw(sheet)
    for index, (name, source_image) in enumerate(images):
        column = index % columns
        row = index // columns
        left = column * cell_width
        top = row * cell_height
        draw.text((left + 6, top + 5), f"{name}  {source_image.width}x{source_image.height}", fill="white")
        preview = source_image.convert("RGBA").resize(
            (source_image.width * scale, source_image.height * scale),
            Image.Resampling.NEAREST,
        )
        checker = Image.new("RGBA", preview.size, (28, 28, 28, 255))
        checker.alpha_composite(preview)
        sheet.paste(checker.convert("RGB"), (left + 6, top + 30))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")


def main() -> int:
    args = parse_args()
    spec_path = args.spec.resolve()
    spec = load_spec(spec_path)
    sources = spec["sources"]
    expected_count = int(spec.get("per_monster", 8))
    if expected_count <= 0:
        raise ValueError("per_monster must be positive")
    if args.start_index <= 0:
        raise ValueError("start-index must be positive")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    prepared: list[tuple[Path, Image.Image]] = []
    previews: list[tuple[str, Image.Image]] = []
    next_index = args.start_index

    try:
        for source_number, source_spec in enumerate(sources, start=1):
            if not isinstance(source_spec, dict):
                raise ValueError(f"source {source_number} must be an object")
            raw_crops = source_spec.get("crops")
            if not isinstance(raw_crops, list) or len(raw_crops) != expected_count:
                raise ValueError(
                    f"source {source_number} must define exactly {expected_count} crops"
                )
            source_path = resolve_source(spec_path, str(source_spec.get("path", "")))
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            with Image.open(source_path) as source_image:
                source_image.load()
                for raw_rect in raw_crops:
                    x, y, width, height = normalized_rect(raw_rect, source_image.size)
                    crop = source_image.crop((x, y, x + width, y + height)).copy()
                    output_name = f"{args.prefix}{next_index}.png"
                    output_path = args.output_dir / output_name
                    if output_path.exists() and not args.overwrite:
                        raise FileExistsError(
                            f"output already exists: {output_path}; pass --overwrite only with permission"
                        )
                    prepared.append((output_path, crop))
                    previews.append((output_name, crop.copy()))
                    next_index += 1

        for output_path, crop in prepared:
            crop.save(output_path, format="PNG")
        if args.contact_sheet:
            save_contact_sheet(previews, args.contact_sheet.resolve())
    finally:
        for _, crop in prepared:
            crop.close()
        for _, preview in previews:
            preview.close()

    print(f"created={len(prepared)}")
    print(f"output_dir={args.output_dir.resolve()}")
    if args.contact_sheet:
        print(f"contact_sheet={args.contact_sheet.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
