---
name: make-monster-templates
description: Generate, split, inspect, or repair pixel-exact guai*.png monster-recognition templates from game monster images, including the D:\PythonProject4 atlas under img\monsters, level-grouped multi-selection, single-eye/single-mouth/single-hair crops, original and mirrored source assets, and manual corrections under img\自定义. Use for怪物识别图片、图鉴模板、guai模板 generation, optimization, preview, accuracy, or recognition-speed work. Default to eight templates per monster unless specified.
---

# Make Monster Templates

Create deterministic crops from the supplied monster images. Never redraw, upscale, sharpen, recolor, or use a generative image model: recognition templates must retain the original game pixels.

For atlas-generated images, read [references/mumian-style.md](references/mumian-style.md). Process only monsters the user selected; do not scan all 400 library images unless explicitly requested.

## Atlas workflow

1. Preview selected monsters without replacing templates:

   ```powershell
   python scripts/generate_atlas_templates.py `
     --project-root D:\PythonProject4 `
     --source "D:\PythonProject4\img\monsters\level_01-10\Pig.png" `
     --preview D:\path\atlas-preview.png
   ```

2. Inspect every crop. Require one local feature per image: one eye, one mouth/teeth region, one hair tuft, one horn, or one ornament.
3. Apply only with explicit permission to replace root `img\自定义\guai*`:

   ```powershell
   python scripts/generate_atlas_templates.py `
     --project-root D:\PythonProject4 `
     --source "D:\PythonProject4\img\monsters\level_01-10\Pig.png" `
     --apply
   ```

4. Confirm continuous `guai1.png` through `guaiN.png`, eight templates per monster, and preserved named map subdirectories.
5. Confirm `img\自定义\图鉴原图\<level>` contains `<monster>_原图.png` and `<monster>_镜像.png`. These do not start with `guai`, so the detector ignores them; use them for manual screenshot corrections.
6. Confirm `怪物图鉴模板.json` records feature labels, crop rectangles, and manual-source paths.

## Workflow

1. Inspect the supplied images at original resolution. Treat every supplied monster image as an edit target.
2. If working in `D:\PythonProject4`, inspect a few existing `img\自定义\**\guai*.png` files for typical crop sizes and naming.
3. Use eight crops per monster by default. Follow a user-specified count when present.
4. Select distinct, stable regions in this order:
   - eyes, mouth, teeth, nose, hair, hat, horns, or head ornaments;
   - two-feature combinations such as eye plus mouth;
   - distinctive outline/color transitions on the head or torso;
   - one or two body, limb, or tail regions only when they are visually unique.
5. Avoid crops that are mostly transparent, contain only a flat color, depend on attack flashes, include common UI colors without shape context, or duplicate another crop.
6. Prefer roughly 12–40 px wide and 8–30 px high. Smaller crops are acceptable for exceptionally unique details; larger crops are acceptable for a stable combined feature. Do not add padding or change scale.
7. Number output continuously as `guai1.png` through `guaiN.png` across all supplied monsters. For two monsters at eight each, create `guai1.png`–`guai16.png`.
8. Put the files in the exact folder requested by the user. Create a named subfolder under `img\自定义` when requested. Do not replace an existing non-empty folder without explicit permission.
9. Run `scripts/crop_monster_templates.py` with a JSON crop specification. Use `--contact-sheet` outside the final template folder when visual inspection is useful.
10. Inspect the resulting contact sheet or individual crops. Correct blank, redundant, overly large, or weak templates before reporting completion.

## Crop specification

Use this JSON shape:

```json
{
  "per_monster": 8,
  "sources": [
    {
      "path": "C:\\path\\monster1.png",
      "crops": [
        [20, 10, 18, 14],
        [24, 14, 12, 10]
      ]
    }
  ]
}
```

Each crop is `[x, y, width, height]` in original-image pixels. Coordinates must remain inside the source image.

Run:

```powershell
python scripts/crop_monster_templates.py `
  --spec D:\path\crop-spec.json `
  --output-dir D:\PythonProject4\img\自定义\地图名 `
  --contact-sheet D:\path\templates-preview.png
```

Add `--overwrite` only when the user explicitly asks to replace existing templates.

## Completion report

Report the destination folder, template count, numbering range, templates per monster, and whether original pixels were preserved. Show the contact sheet when it materially helps the user review the result.
