import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class MesLatestNotePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (ROOT / "templates" / "gestao_os.html").read_text(
            encoding="utf-8"
        )
        cls.service = (ROOT / "erp_service.py").read_text(encoding="utf-8")

    def test_list_uses_latest_note_from_work_order_or_vehicle_entry(self):
        self.assertIn("left join lateral", self.service)
        self.assertIn("from erp_work_order_notes n", self.service)
        self.assertIn("from erp_vehicle_entry_notes n", self.service)
        self.assertIn("latest_note.note as latest_note", self.service)
        self.assertIn("order by notes.created_at desc,notes.id desc", self.service)

    def test_cards_preview_latest_note_and_make_it_searchable(self):
        self.assertIn("function latestNotePreview(row)", self.template)
        self.assertIn("Última observação", self.template)
        self.assertIn("${latestNotePreview(x)}</article>", self.template)
        self.assertIn("x.latest_note].join(' ')", self.template)


if __name__ == "__main__":
    unittest.main()
