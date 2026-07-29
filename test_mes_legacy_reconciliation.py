import unittest
from pathlib import Path

from mes_legacy_reconciliation import classify_candidates, normalize_chassis, stage_status


class MesLegacyReconciliationTests(unittest.TestCase):
    def test_normalizes_chassis_without_using_short_values(self):
        self.assertEqual(normalize_chassis(" 9v7-vpfc38 ta004249 "), "9V7VPFC38TA004249")
        self.assertEqual(normalize_chassis("TA004249"), "TA004249")
        self.assertEqual(normalize_chassis("AG"), "")

    def test_stage_mapping_preserves_legacy_semantics(self):
        self.assertEqual(stage_status("SIM"), "CONCLUIDA")
        self.assertEqual(stage_status("NÃO"), "PENDENTE")
        self.assertEqual(stage_status("N/A"), "NAO_APLICAVEL")

    def test_unique_chassis_is_a_suggestion_not_an_approval(self):
        matrix = classify_candidates(
            [{"id": 7, "chassi": "9V7VPFC38TA004249", "legacy_stage_rows": 12}],
            [{"id": 91, "numero": "JI-100", "chassi": "9V7VPFC38TA004249"}],
        )
        self.assertEqual(matrix[0]["classification"], "UNIQUE_CANDIDATE_REQUIRES_APPROVAL")
        self.assertEqual(matrix[0]["candidate_orders"][0]["numero_os"], "JI-100")

    def test_repeated_chassis_stays_ambiguous(self):
        matrix = classify_candidates(
            [{"id": 7, "chassi": "9V7VPFC38TA004249"}],
            [
                {"id": 91, "numero": "JI-100", "chassi": "9V7VPFC38TA004249"},
                {"id": 92, "numero": "JI-101", "chassi": "9V7VPFC38TA004249"},
            ],
        )
        self.assertEqual(matrix[0]["classification"], "AMBIGUOUS_MULTIPLE_TARGET_ORDERS")

    def test_startup_has_no_hardcoded_admin_password(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertNotIn('hash_password("2410")', source)
        self.assertIn("MES_BOOTSTRAP_ADMIN_PASSWORD", source)

    def test_legacy_auto_ddl_is_opt_in(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("MES_LEGACY_SCHEMA_AUTO_MIGRATE", source)
        self.assertIn("if legacy_schema_auto_migrate_enabled():", source)


if __name__ == "__main__":
    unittest.main()
