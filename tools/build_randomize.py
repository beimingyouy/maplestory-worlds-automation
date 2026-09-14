# -*- coding: utf-8 -*-
"""Generate a randomized build identity for packaging.

Each build picks a fresh identity so the EXE name, icon, and Windows
version/copyright resource are all random but mutually consistent.

Usage:
    python build_randomize.py --output-dir <dir> [--seed <seed>]

Prints a single JSON object on stdout:
    {"name": "...", "icon": "...", "version_file": "..."}
"""

import argparse
import colorsys
import datetime
import json
import random
import string
from pathlib import Path

from PIL import Image, ImageDraw

NAME_PARTS = [
    "DataSync", "MediaHub", "CloudNote", "FileMate", "BrightTool",
    "QuickAssist", "SysTuner", "NetProbe", "DocFlow", "PixelView",
    "SoundBite", "TaskPilot", "VaultKey", "LanMap", "SyncWise",
    "CoreDock", "AirLift", "NovaSync", "ZenPilot", "SwiftBase",
    "PrimeLink", "EchoNote", "FluxDrive", "GlideHub", "HaloSync",
    "IrisTool", "JetFlow", "KiteNote", "LumaBase", "MetroSync",
    "NimbusTool", "OrbitNote", "PulseHub", "QuartzSync", "RidgeBase",
    "SolarFlow", "TideNote", "UltraBase", "VegaSync", "WaveTool",
]

ICON_SIZES = [
    (16, 16), (24, 24), (32, 32), (48, 48),
    (64, 64), (128, 128), (256, 256),
]


def random_name(rng):
    """Random product name such as 'DataSync4821' or 'NovaSync-1739'."""
    part = rng.choice(NAME_PARTS)
    digits = "".join(rng.choice(string.digits) for _ in range(4))
    if rng.random() < 0.5:
        return "{0}-{1}".format(part, digits)
    return "{0}{1}".format(part, digits)


def _hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(
        h % 1.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, v))
    )
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def generate_icon(path, rng):
    """Draw a simple random glyph on a random colored rounded tile."""
    size = 256
    hue = rng.random()
    bg = _hsv(hue, rng.uniform(0.45, 0.75), rng.uniform(0.85, 0.95))
    fg = _hsv(hue + rng.uniform(0.35, 0.65), rng.uniform(0.55, 0.80), 0.97)
    accent = _hsv(hue + rng.uniform(0.10, 0.25), rng.uniform(0.50, 0.80), 0.90)

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    radius = rng.choice([26, 40, 56, size // 2])
    d.rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=bg
    )

    shape = rng.choice(
        ["circle", "square", "triangle", "diamond", "rings", "bars"]
    )
    cx, cy = size // 2, size // 2
    if shape == "circle":
        r = rng.randint(52, 76)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fg)
        r2 = r // 2
        d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=accent)
    elif shape == "square":
        s = rng.randint(100, 150)
        d.rounded_rectangle(
            [cx - s // 2, cy - s // 2, cx + s // 2, cy + s // 2],
            radius=rng.randint(8, 24), fill=fg,
        )
    elif shape == "triangle":
        s = rng.randint(110, 160)
        d.polygon(
            [(cx, cy - s // 2), (cx - s // 2, cy + s // 2), (cx + s // 2, cy + s // 2)],
            fill=fg,
        )
    elif shape == "diamond":
        s = rng.randint(100, 150)
        d.polygon(
            [(cx, cy - s // 2), (cx + s // 2, cy), (cx, cy + s // 2), (cx - s // 2, cy)],
            fill=fg,
        )
    elif shape == "rings":
        for r, color in [(80, fg), (56, accent), (32, fg)]:
            d.ellipse(
                [cx - r, cy - r, cx + r, cy + r],
                outline=color, width=rng.randint(10, 18),
            )
    else:  # bars
        bar_w = rng.randint(28, 40)
        gap = rng.randint(12, 20)
        heights = [rng.randint(60, 170) for _ in range(3)]
        colors = [fg, accent, fg]
        total_w = 3 * bar_w + 2 * gap
        x = cx - total_w // 2
        base_y = cy + 90
        for h, color in zip(heights, colors):
            d.rounded_rectangle(
                [x, base_y - h, x + bar_w, base_y],
                radius=max(2, bar_w // 4), fill=color,
            )
            x += bar_w + gap

    img.save(path, format="ICO", sizes=ICON_SIZES)
    return path


def write_version_file(path, name):
    """Write a PyInstaller version resource whose copyright matches the name."""
    year = datetime.date.today().year
    company = "{0} Software".format(name)
    copyright_text = "Copyright (C) {0} {1}. All rights reserved.".format(year, company)
    description = "{0} Application".format(name)
    content = """# UTF-8
#
# Version resource generated for the randomized build identity.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(3, 0, 0, 0),
    prodvers=(3, 0, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904b0',
        [StringStruct('CompanyName', '{company}'),
         StringStruct('FileDescription', '{description}'),
         StringStruct('FileVersion', '3.0.0.0'),
         StringStruct('InternalName', '{name}'),
         StringStruct('LegalCopyright', '{copyright_text}'),
         StringStruct('OriginalFilename', '{name}.exe'),
         StringStruct('ProductName', '{name}'),
         StringStruct('ProductVersion', '3.0.0.0')]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""".format(
        company=company,
        description=description,
        name=name,
        copyright_text=copyright_text,
    )
    path.write_text(content, encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--seed", default=None)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    name = random_name(rng)
    icon_path = generate_icon(out_dir / "icon.ico", rng)
    version_path = write_version_file(out_dir / "version_info.txt", name)

    print(json.dumps({
        "name": name,
        "icon": str(icon_path),
        "version_file": str(version_path),
    }))


if __name__ == "__main__":
    main()
