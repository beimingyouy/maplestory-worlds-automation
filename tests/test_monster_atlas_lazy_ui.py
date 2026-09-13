import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from v3.monster_atlas_dialog import (
    MONSTER_CARD_COLUMNS,
    MonsterAtlasDialog,
    MonsterCard,
    MonsterGroupSection,
)
from v3.public.monster_atlas import (
    estimated_template_count,
    scan_monster_library,
)


PROJECT_ROOT = "D:/PythonProject4"


class MonsterAtlasLazyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.groups = scan_monster_library(PROJECT_ROOT)

    def test_card_constructor_does_not_decode_preview(self):
        entry = self.groups[0].monsters[0]
        card = MonsterCard(entry)

        self.assertFalse(card.preview_loaded)
        self.assertTrue(card.icon().isNull())
        card.load_preview()
        self.assertTrue(card.preview_loaded)
        self.assertFalse(card.icon().isNull())
        card.unload_preview()
        self.assertFalse(card.preview_loaded)
        self.assertTrue(card.icon().isNull())

    def test_group_layout_uses_five_cards_per_row(self):
        section = MonsterGroupSection(self.groups[0], expanded=True)
        section.apply_filter("")

        self.assertEqual(5, MONSTER_CARD_COLUMNS)
        for index in range(min(12, len(section.cards))):
            row, column, _row_span, _column_span = section.grid.getItemPosition(index)
            self.assertEqual(index // 5, row)
            self.assertEqual(index % 5, column)

    def test_opening_dialog_loads_only_nearby_visible_previews(self):
        dialog = MonsterAtlasDialog(PROJECT_ROOT)
        dialog.show()
        for _ in range(5):
            self.app.processEvents()
        cards = [
            card
            for section in dialog._group_sections
            for card in section.cards
        ]
        loaded = [card for card in cards if card.preview_loaded]

        self.assertGreater(len(cards), 100)
        self.assertGreater(len(loaded), 0)
        self.assertLess(len(loaded), len(cards) // 4)
        self.assertTrue(
            all(
                not card.preview_loaded
                for section in dialog._group_sections
                if not section.is_expanded
                for card in section.cards
            )
        )
        dialog.close()
        self.app.processEvents()

    def test_first_three_manual_feature_policies_do_not_fill_to_twelve_each(self):
        entries = {
            entry.name: entry
            for group in self.groups
            for entry in group.monsters
        }
        selected = [
            entries["Blue Snail"].path,
            entries["Orange Mushroom"].path,
            entries["Slime"].path,
        ]

        # 蓝蜗牛2张、橙蘑菇8张、史莱姆2张，均包含原图和镜像。
        self.assertEqual(12, estimated_template_count(PROJECT_ROOT, selected))


if __name__ == "__main__":
    unittest.main()
