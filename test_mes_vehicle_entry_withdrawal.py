import unittest

import erp_service


class FakeRow:
    def __init__(self, value):
        self._mapping = value


class FakeResult:
    def __init__(self, rows=None):
        self.rows = [FakeRow(row) for row in (rows or [])]

    def first(self):
        return self.rows[0] if self.rows else None


class WithdrawalConnection:
    def __init__(self, entry, work=None):
        self.entry = entry
        self.work = work
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "from erp_vehicle_entries" in sql and "for update" in sql:
            return FakeResult([self.entry] if self.entry else [])
        if "from erp_work_orders" in sql and "vehicle_entry_id" in sql:
            return FakeResult([self.work] if self.work else [])
        return FakeResult()


class VehicleEntryWithdrawalTests(unittest.TestCase):
    def test_requires_reason(self):
        conn = WithdrawalConnection({"id": "entry-1", "item_number": 3201, "status": "AGUARDANDO_O_S"})
        with self.assertRaisesRegex(ValueError, "motivo"):
            erp_service.withdraw_vehicle_entry(conn, "entry-1", "PCP")
        self.assertEqual([], conn.calls)

    def test_withdrawal_updates_only_entry_and_writes_audit(self):
        conn = WithdrawalConnection({"id": "entry-1", "item_number": 3201, "status": "AGUARDANDO_O_S"})
        result = erp_service.withdraw_vehicle_entry(
            conn, "entry-1", "PCP", "Veiculo devolvido ao fornecedor", "2026-08-14T10:30:00"
        )

        self.assertFalse(result["replayed"])
        self.assertEqual("RETIRADA", result["status"])
        self.assertTrue(any(sql.startswith("update erp_vehicle_entries") for sql, _ in conn.calls))
        audit = next(params for sql, params in conn.calls if sql.startswith("insert into erp_audit_events"))
        self.assertEqual("AGUARDANDO_O_S", audit["old_status"])
        self.assertEqual("Veiculo devolvido ao fornecedor", audit["reason"])
        self.assertFalse(any("erp_work_order_status_history" in sql for sql, _ in conn.calls))

    def test_withdrawal_is_idempotent(self):
        conn = WithdrawalConnection({"id": "entry-1", "item_number": 3201, "status": "RETIRADA"})
        result = erp_service.withdraw_vehicle_entry(conn, "entry-1", "PCP", "Ja saiu")
        self.assertTrue(result["replayed"])
        self.assertFalse(any(sql.startswith("update erp_vehicle_entries") for sql, _ in conn.calls))

    def test_rejects_withdrawal_when_an_order_exists(self):
        conn = WithdrawalConnection(
            {"id": "entry-1", "item_number": 3201, "status": "AGUARDANDO_O_S"},
            {"id": "work-1", "numero_os": "3201"},
        )
        with self.assertRaisesRegex(ValueError, "Registre a retirada na O.S."):
            erp_service.withdraw_vehicle_entry(conn, "entry-1", "PCP", "Mudanca de rota")

    def test_rejects_opening_order_from_withdrawn_entry(self):
        conn = WithdrawalConnection({"id": "entry-1", "item_number": 3201, "status": "RETIRADA"})
        with self.assertRaisesRegex(ValueError, "Veiculo retirado sem O.S."):
            erp_service.create_work_order(conn, "entry-1", {}, "PCP")


if __name__ == "__main__":
    unittest.main()
