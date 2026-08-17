import unittest

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


class BankCorrectionConnection:
    def __init__(self, work):
        self.work = work
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "from erp_work_orders" in sql and "for update" in sql:
            return FakeResult(self.work)
        return FakeResult(rowcount=1)


class WorkOrderBankCorrectionTests(unittest.TestCase):
    def test_corrects_bank_on_delivered_order_without_reopening_it(self):
        conn = BankCorrectionConnection({
            "id": "work-1",
            "numero_os": "3119",
            "status": "ENTREGUE",
            "codigo_banco": "30200025",
            "conjunto_bancos": "CJ ANTIGO",
            "version": 7,
        })

        result = erp_service.correct_work_order_bank(conn, "work-1", {
            "codigo_banco": "30200033",
            "conjunto_bancos": "CJ NOVO",
            "motivo": "Correção após conferência do PCP.",
        }, "PCP")

        self.assertFalse(result["replayed"])
        self.assertEqual("ENTREGUE", result["status"])
        update_sql, update_params = next(
            (sql, params) for sql, params in conn.calls
            if sql.startswith("update erp_work_orders")
        )
        self.assertIn("set codigo_banco=:codigo_banco", update_sql)
        self.assertNotIn("status=", update_sql)
        self.assertEqual("30200033", update_params["codigo_banco"])
        audit_sql, audit_params = next(
            (sql, params) for sql, params in conn.calls
            if sql.startswith("insert into erp_audit_events")
        )
        self.assertIn("correcao_codigo_banco", audit_sql)
        self.assertEqual("30200025", audit_params["codigo_anterior"])
        self.assertEqual("30200033", audit_params["codigo_novo"])
        self.assertEqual("ENTREGUE", audit_params["status"])

    def test_requires_a_reason(self):
        conn = BankCorrectionConnection({
            "id": "work-1",
            "numero_os": "3119",
            "status": "FINALIZADA",
            "codigo_banco": "30200025",
            "conjunto_bancos": "CJ ANTIGO",
            "version": 2,
        })

        with self.assertRaisesRegex(ValueError, "motivo"):
            erp_service.correct_work_order_bank(conn, "work-1", {
                "codigo_banco": "30200033",
                "conjunto_bancos": "CJ NOVO",
            }, "PCP")

        self.assertEqual([], conn.calls)

    def test_same_bank_is_idempotent(self):
        conn = BankCorrectionConnection({
            "id": "work-1",
            "numero_os": "3119",
            "status": "CONCLUIDA",
            "codigo_banco": "30200033",
            "conjunto_bancos": "CJ NOVO",
            "version": 9,
        })

        result = erp_service.correct_work_order_bank(conn, "work-1", {
            "codigo_banco": "30200033",
            "conjunto_bancos": "CJ NOVO",
            "motivo": "Conferência sem alteração.",
        }, "PCP")

        self.assertTrue(result["replayed"])
        self.assertFalse(any(
            sql.startswith("update erp_work_orders") for sql, _ in conn.calls
        ))


if __name__ == "__main__":
    unittest.main()
