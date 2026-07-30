from datetime import date
from functools import cmp_to_key
import unittest

from erp_service import (
    DEFAULT_SEQUENCE_CRITERIA,
    _compare_sequence_rows,
    _normalized_sequence_criteria,
    operational_work_order_status,
)


class MesSequencingTests(unittest.TestCase):
    def row(self, work_id, item, delivery=None, priority=None, line="LB"):
        return {
            "id": work_id,
            "item_number": item,
            "data_comercial_prevista": delivery,
            "prioridade_manual": priority,
            "linha": line,
            "tipo_veiculo": "MICRO",
            "transformacao": "JI CONFORT",
            "ar_condicionado": "GE",
            "conjunto_bancos": "CJ",
            "cliente_nome": "CLIENTE",
        }

    def test_delivery_date_orders_wip_before_item_number(self):
        rows = [
            self.row("b", 3112, date(2026, 8, 10)),
            self.row("a", 3111, date(2026, 8, 8)),
            self.row("c", 3113, None),
        ]
        ordered = sorted(rows, key=cmp_to_key(
            lambda left, right: _compare_sequence_rows(left, right, DEFAULT_SEQUENCE_CRITERIA)
        ))
        self.assertEqual([row["id"] for row in ordered], ["a", "b", "c"])

    def test_manual_priority_is_secondary_and_persistent_candidate(self):
        rows = [
            self.row("late", 3112, date(2026, 8, 10), priority=2),
            self.row("first", 3111, date(2026, 8, 10), priority=1),
        ]
        ordered = sorted(rows, key=cmp_to_key(
            lambda left, right: _compare_sequence_rows(left, right, DEFAULT_SEQUENCE_CRITERIA)
        ))
        self.assertEqual([row["id"] for row in ordered], ["first", "late"])

    def test_criteria_reject_unknown_or_duplicated_fields(self):
        with self.assertRaises(ValueError):
            _normalized_sequence_criteria([{"field": "unknown", "direction": "ASC"}])
        with self.assertRaises(ValueError):
            _normalized_sequence_criteria([
                {"field": "line", "direction": "ASC"},
                {"field": "line", "direction": "DESC"},
            ])

    def test_operational_status_normalizes_wip_encodings(self):
        self.assertEqual("ATIVA", operational_work_order_status("ATIVA"))
        self.assertEqual("EM_PRODUCAO", operational_work_order_status("EM_PRODUÇÃO"))
        self.assertEqual("EM_PRODUCAO", operational_work_order_status("EM_PRODUCAO"))
        self.assertEqual("EM_PRODUCAO", operational_work_order_status("EM_PRODUCAO".replace(" ", "_")))


if __name__ == "__main__":
    unittest.main()
