import json
import unittest
from datetime import date

import erp_service


class FakeRow:
    def __init__(self, value):
        self._mapping = value


class FakeResult:
    def __init__(self, row=None, rowcount=0):
        self.row = FakeRow(row) if row else None
        self.rowcount = rowcount

    def first(self):
        return self.row


class HistoricalCorrectionConnection:
    def __init__(self, work):
        self.work = work
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "select id,vehicle_entry_id,numero_os,status" in sql:
            return FakeResult({
                "id": self.work["id"],
                "vehicle_entry_id": self.work["vehicle_entry_id"],
                "numero_os": self.work["numero_os"],
                "status": self.work["status"],
            })
        if "select * from erp_work_orders" in sql and "for update" in sql:
            return FakeResult(self.work)
        return FakeResult(rowcount=1)


class WorkOrderHistoricalCorrectionTests(unittest.TestCase):
    def work(self, status="ENTREGUE"):
        return {
            "id": "work-3119",
            "vehicle_entry_id": "entry-3119",
            "numero_os": "3119",
            "status": status,
            "tipo_servico": "TRANSFORMAÇÃO",
            "vendedor": "VENDEDOR ANTIGO",
            "mercado": "VAREJO",
            "codigo_banco": "30200025",
            "conjunto_bancos": "CJ ANTIGO",
            "data_comercial_prevista": date(2026, 8, 15),
            "version": 8,
        }

    def test_corrects_multiple_fields_without_reopening_or_changing_history(self):
        conn = HistoricalCorrectionConnection(self.work())

        result = erp_service.correct_closed_work_order(conn, "work-3119", {
            "motivo": "Conferência histórica do PCP.",
            "entry": {},
            "work_order": {
                "tipo_servico": "OUTROS",
                "vendedor": "VENDEDOR CORRETO",
                "mercado": "LICITAÇÃO",
                "codigo_banco": "30200033",
                "conjunto_bancos": "CJ CORRETO",
                "data_comercial_prevista": "2026-08-20",
                "status": "ATIVA",
                "termino_producao": "2026-08-21",
                "data_saida": "2026-08-21",
            },
        }, "PCP")

        self.assertFalse(result["replayed"])
        self.assertEqual("ENTREGUE", result["status"])
        update_sql, update_params = next(
            (sql, params) for sql, params in conn.calls
            if sql.startswith("update erp_work_orders")
        )
        self.assertIn("vendedor=:vendedor", update_sql)
        self.assertIn("tipo_servico=:tipo_servico", update_sql)
        self.assertNotIn("status=", update_sql)
        self.assertNotIn("termino_producao", update_sql)
        self.assertNotIn("data_saida", update_sql)
        self.assertEqual("OUTRO", update_params["tipo_servico"])
        self.assertEqual(date(2026, 8, 20), update_params["data_comercial_prevista"])
        self.assertFalse(any(
            "erp_work_order_schedules" in sql for sql, _ in conn.calls
        ))

        audit_sql, audit_params = next(
            (sql, params) for sql, params in conn.calls
            if sql.startswith("insert into erp_audit_events")
        )
        self.assertIn("correcao_historica_dados_os", audit_sql)
        before = json.loads(audit_params["before_data"])
        after = json.loads(audit_params["after_data"])
        self.assertEqual("ENTREGUE", before["status"])
        self.assertEqual("ENTREGUE", after["status"])
        self.assertEqual("VENDEDOR ANTIGO", before["vendedor"])
        self.assertEqual("VENDEDOR CORRETO", after["vendedor"])

    def test_requires_reason_before_reading_or_writing(self):
        conn = HistoricalCorrectionConnection(self.work())

        with self.assertRaisesRegex(ValueError, "motivo"):
            erp_service.correct_closed_work_order(conn, "work-3119", {
                "entry": {},
                "work_order": {"vendedor": "NOVO"},
            }, "PCP")

        self.assertEqual([], conn.calls)

    def test_same_values_are_idempotent(self):
        conn = HistoricalCorrectionConnection(self.work("CONCLUIDA"))

        result = erp_service.correct_closed_work_order(conn, "work-3119", {
            "motivo": "Conferência sem divergência.",
            "entry": {},
            "work_order": {
                "vendedor": "VENDEDOR ANTIGO",
                "mercado": "VAREJO",
            },
        }, "PCP")

        self.assertTrue(result["replayed"])
        self.assertFalse(any(
            sql.startswith("update erp_work_orders") for sql, _ in conn.calls
        ))
        self.assertFalse(any(
            sql.startswith("insert into erp_audit_events") for sql, _ in conn.calls
        ))

    def test_open_order_uses_the_normal_edit_flow(self):
        conn = HistoricalCorrectionConnection(self.work("EM_PRODUÇÃO"))

        with self.assertRaisesRegex(ValueError, "edição normal"):
            erp_service.correct_closed_work_order(conn, "work-3119", {
                "motivo": "Tentativa indevida.",
                "entry": {},
                "work_order": {"vendedor": "NOVO"},
            }, "PCP")

        self.assertFalse(any(
            sql.startswith("update erp_work_orders") for sql, _ in conn.calls
        ))


if __name__ == "__main__":
    unittest.main()
