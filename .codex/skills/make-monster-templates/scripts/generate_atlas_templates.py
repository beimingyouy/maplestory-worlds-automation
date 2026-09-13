#!/usr/bin/env python3
r"""Preview or apply D:\PythonProject4 atlas monster templates."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PIL import Image


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview isolated monster features or safely replace root guai* templates."
    )
    parser.add_argument("--project-root", type=Path, default=Path(r"D:\PythonProject4"))
    parser.add_argument("--source", action="append", type=Path, required=True)
    parser.add_argument("--templates-per-monster", type=int, default=8)
    parser.add_argument("--preview", type=Path)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def _load_project_api(project_root: Path):
    root_text = str(project_root.resolve())
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from v3.public.monster_atlas import (  # noqa: PLC0415
        generate_monster_templates,
        suggest_feature_templates,
    )

    return generate_monster_templates, suggest_feature_templates


def _save_preview(items, output_path: Path) -> None:
    script_directory = Path(__file__).resolve().parent
    if str(script_directory) not in sys.path:
        sys.path.insert(0, str(script_directory))
    from crop_monster_templates import save_contact_sheet  # noqa: PLC0415

    save_contact_sheet(items, output_path.resolve())


def main() -> int:
    args = parse_args()
    project_root = args.project_root.resolve()
    sources = tuple(path.resolve() for path in args.source)
    if args.templates_per_monster <= 0:
        raise ValueError("templates-per-monster must be positive")
    generate, suggest = _load_project_api(project_root)

    if args.apply:
        report = generate(project_root, sources, args.templates_per_monster)
        print(json.dumps({
            "selected_monsters": len(report.selected_monsters),
            "template_count": report.template_count,
            "output_directory": str(report.output_directory),
            "source_asset_directory": str(report.source_asset_directory),
            "manifest": str(report.manifest_file),
            "preview": str(report.preview_file),
        }, ensure_ascii=False, indent=2))
        return 0

    preview_items = []
    crop_report = []
    try:
        for source_path in sources:
            if not source_path.is_file():
                raise FileNotFoundError(source_path)
            with Image.open(source_path) as source:
                source.load()
                features = suggest(source, args.templates_per_monster)
                crop_report.append({
                    "source": str(source_path),
                    "features": [
                        {"name": name, "rect": list(rect)} for name, rect in features
                    ],
                })
                for name, (x, y, width, height) in features:
                    crop = source.crop((x, y, x + width, y + height)).copy()
                    preview_items.append(("{}-{}".format(source_path.stem, name), crop))
        if args.preview:
            _save_preview(preview_items, args.preview)
        print(json.dumps({"sources": crop_report}, ensure_ascii=False, indent=2))
        if args.preview:
            print("preview={}".format(args.preview.resolve()))
    finally:
        for _, crop in preview_items:
            crop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
