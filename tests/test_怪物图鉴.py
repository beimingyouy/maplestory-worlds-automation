import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageDraw, ImageOps

from v3.public.monster_atlas import (
    MAX_TEMPLATES_PER_MONSTER,
    estimated_template_count,
    generate_monster_templates,
    scan_monster_library,
    suggest_feature_crops,
    suggest_feature_templates,
)


def _make_monster(path: Path, body_color, accent_color) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (72, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 6, 61, 56), fill=body_color)
    draw.ellipse((20, 20, 29, 31), fill=(250, 250, 250, 255))
    draw.ellipse((42, 20, 51, 31), fill=(250, 250, 250, 255))
    draw.rectangle((24, 23, 27, 30), fill=accent_color)
    draw.rectangle((44, 23, 47, 30), fill=accent_color)
    draw.arc((25, 30, 47, 45), 5, 175, fill=accent_color, width=3)
    draw.polygon(((16, 12), (22, 0), (29, 14)), fill=accent_color)
    draw.polygon(((43, 14), (51, 1), (57, 15)), fill=accent_color)
    image.save(path, format="PNG")
    image.close()


class MonsterAtlasTests(unittest.TestCase):
    def test_library_uses_real_directory_names_and_natural_level_order(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            _make_monster(
                root / "img" / "monsters" / "level_101-110" / "Monster 10.png",
                (120, 70, 180, 255),
                (25, 15, 35, 255),
            )
            _make_monster(
                root / "img" / "monsters" / "level_11-20" / "Monster 2.png",
                (220, 150, 80, 255),
                (35, 20, 10, 255),
            )
            _make_monster(
                root / "img" / "monsters" / "level_01-10" / "Monster 1.png",
                (80, 180, 220, 255),
                (10, 25, 35, 255),
            )

            groups = scan_monster_library(root)

            self.assertEqual(
                ["level_01-10", "level_11-20", "level_101-110"],
                [group.name for group in groups],
            )
            self.assertEqual("Monster 1", groups[0].monsters[0].name)

    def test_feature_crops_prefer_small_original_pixel_regions(self):
        with tempfile.TemporaryDirectory() as raw_root:
            source_path = Path(raw_root) / "monster.png"
            _make_monster(
                source_path,
                (230, 170, 90, 255),
                (40, 20, 15, 255),
            )
            with Image.open(source_path) as source:
                rects = suggest_feature_crops(source)
                features = suggest_feature_templates(source)

            self.assertEqual(6, len(rects))
            self.assertEqual("left_eye", features[0][0])
            self.assertEqual("right_eye", features[1][0])
            left_eye = features[0][1]
            right_eye = features[1][1]
            self.assertLessEqual(left_eye[2], 15)
            self.assertLessEqual(right_eye[2], 15)
            self.assertNotEqual(left_eye, right_eye)
            self.assertGreater(
                abs((left_eye[0] + left_eye[2] / 2) - (right_eye[0] + right_eye[2] / 2)),
                min(left_eye[2], right_eye[2]) / 2,
            )
            self.assertGreaterEqual(sum(width <= 18 for _, _, width, _ in rects), 5)
            for x, y, width, height in rects:
                self.assertGreaterEqual(x, 0)
                self.assertGreaterEqual(y, 0)
                self.assertLessEqual(x + width, 72)
                self.assertLessEqual(y + height, 64)

    def test_generate_replaces_only_root_guai_and_keeps_exact_source_pixels(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            first = root / "img" / "monsters" / "level_01-10" / "Blue One.png"
            second = root / "img" / "monsters" / "level_11-20" / "Red Two.png"
            _make_monster(first, (80, 170, 230, 255), (10, 30, 60, 255))
            _make_monster(second, (230, 100, 90, 255), (60, 10, 15, 255))

            custom = root / "img" / "自定义"
            named_map = custom / "旧地图"
            named_map.mkdir(parents=True)
            Image.new("RGB", (3, 3), "red").save(custom / "guai_old.png")
            Image.new("RGB", (3, 3), "blue").save(custom / "guai99.jpg")
            preserved = named_map / "guai1.png"
            Image.new("RGB", (4, 4), "green").save(preserved)
            preserved_bytes = preserved.read_bytes()
            (custom / "说明.txt").write_text("keep", encoding="utf-8")

            report = generate_monster_templates(root, (second, first))

            self.assertEqual(24, report.template_count)
            self.assertEqual(
                ["guai{}.png".format(index) for index in range(1, 25)],
                [path.name for path in report.template_files],
            )
            self.assertFalse((custom / "guai_old.png").exists())
            self.assertFalse((custom / "guai99.jpg").exists())
            self.assertEqual(preserved_bytes, preserved.read_bytes())
            self.assertEqual("keep", (custom / "说明.txt").read_text(encoding="utf-8"))
            first_original = (
                report.source_asset_directory
                / "level_01-10"
                / "Blue One_原图.png"
            )
            first_mirrored = (
                report.source_asset_directory
                / "level_01-10"
                / "Blue One_镜像.png"
            )
            self.assertTrue(first_original.is_file())
            self.assertTrue(first_mirrored.is_file())
            with Image.open(first_original) as original, Image.open(first_mirrored) as mirrored:
                self.assertEqual(original.size, mirrored.size)
                self.assertEqual(
                    list(original.convert("RGBA").getdata()),
                    list(mirrored.convert("RGBA").transpose(Image.Transpose.FLIP_LEFT_RIGHT).getdata()),
                )

            manifest = json.loads(report.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(2, manifest["selected_monster_count"])
            self.assertEqual(24, manifest["template_count"])
            self.assertEqual(MAX_TEMPLATES_PER_MONSTER, manifest["max_templates_per_monster"])
            for monster in manifest["monsters"]:
                self.assertEqual(
                    [
                        "left_eye",
                        "left_eye_mirrored",
                        "right_eye",
                        "right_eye_mirrored",
                        "mouth",
                        "mouth_mirrored",
                        "left_hair",
                        "left_hair_mirrored",
                        "center_hair",
                        "center_hair_mirrored",
                        "right_hair",
                        "right_hair_mirrored",
                    ],
                    monster["features"],
                )
                self.assertIn("original", monster["manual_assets"])
                self.assertIn("mirrored", monster["manual_assets"])
                source_path = root / monster["source"]
                with Image.open(source_path) as source:
                    source.load()
                    mirrored_source = ImageOps.mirror(source)
                    for output_name, rect, template_source in zip(
                        monster["templates"],
                        monster["crops"],
                        monster["template_sources"],
                    ):
                        x, y, width, height = rect
                        crop_source = source if template_source == "original" else mirrored_source
                        expected = crop_source.crop((x, y, x + width, y + height)).convert("RGBA")
                        with Image.open(custom / output_name) as actual:
                            self.assertEqual(
                                list(expected.getdata()), list(actual.convert("RGBA").getdata())
                            )
                        expected.close()
                    mirrored_source.close()

    def test_wooden_and_rocky_masks_generate_only_mouth_and_mirror(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            level = root / "img" / "monsters" / "level_21-30"
            level.mkdir(parents=True)
            wooden = level / "Wooden Mask.png"
            rocky = level / "Rocky Mask.png"
            Image.new("RGBA", (84, 85), (90, 60, 30, 255)).save(wooden)
            Image.new("RGBA", (75, 75), (110, 120, 130, 255)).save(rocky)

            self.assertEqual(4, estimated_template_count(root, (wooden, rocky)))
            report = generate_monster_templates(root, (rocky, wooden))

            self.assertEqual(4, report.template_count)
            self.assertEqual(
                ["guai1.png", "guai2.png", "guai3.png", "guai4.png"],
                [path.name for path in report.template_files],
            )
            manifest = json.loads(report.manifest_file.read_text(encoding="utf-8"))
            self.assertEqual(4, manifest["template_count"])
            for monster in manifest["monsters"]:
                self.assertEqual(["mouth", "mouth_mirrored"], monster["features"])
                self.assertEqual(["original", "mirrored"], monster["template_sources"])
                self.assertIn("只识别锯齿嘴巴", monster["policy"])
                original_path = root / monster["source"]
                with Image.open(original_path) as source:
                    original_rect, mirrored_rect = monster["crops"]
                    self.assertEqual(
                        source.width - original_rect[0] - original_rect[2],
                        mirrored_rect[0],
                    )
                    with Image.open(root / "img" / "自定义" / monster["templates"][0]) as first:
                        with Image.open(root / "img" / "自定义" / monster["templates"][1]) as second:
                            self.assertEqual(
                                list(ImageOps.mirror(first.convert("RGBA")).getdata()),
                                list(second.convert("RGBA").getdata()),
                            )

    def test_generation_failure_preserves_existing_templates(self):
        with tempfile.TemporaryDirectory() as raw_root:
            root = Path(raw_root)
            source_path = root / "img" / "monsters" / "level_01-10" / "Blank.png"
            source_path.parent.mkdir(parents=True)
            Image.new("RGBA", (40, 40), (0, 0, 0, 0)).save(source_path)
            custom = root / "img" / "自定义"
            custom.mkdir(parents=True)
            old_template = custom / "guai1.png"
            Image.new("RGB", (5, 5), "purple").save(old_template)
            old_bytes = old_template.read_bytes()

            with self.assertRaises(ValueError):
                generate_monster_templates(root, (source_path,))

            self.assertEqual(old_bytes, old_template.read_bytes())


if __name__ == "__main__":
    unittest.main()
