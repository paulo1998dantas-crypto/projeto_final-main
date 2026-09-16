import unittest

import erp_service


class FakeRow:
    def __init__(self, value):
        self._mapping = value


class FakeResult:
    def __init__(self, rows=None, scalar_value=None, rowcount=0):
        self.rows = [FakeRow(row) for row in (rows or [])]
        self.scalar_value = scalar_value
        self.rowcount = rowcount

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar(self):
        return self.scalar_value


class DeletionConnection:
    def __init__(self, entry, dependencies=None, max_item=3185, sequence_last=3185):
        self.entry = entry
        self.dependencies = dependencies or {}
        self.max_item = max_item
        self.sequence_last = sequence_last
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "from erp_vehicle_entries e" in sql and "for update" in sql:
            return FakeResult([self.entry] if self.entry else [])
        if "select count(*) from erp_work_orders" in sql:
            return FakeResult([self.dependencies])
        if sql.startswith("select max(item_number)"):
            return FakeResult(scalar_value=self.max_item)
        if "select last_value from public.erp_vehicle_entries_item_number_seq" in sql:
            return FakeResult(scalar_value=self.sequence_last)
        if sql.startswith("delete from erp_vehicle_entries where"):
            self.max_item = 3184
            return FakeResult(rowcount=1)
        return FakeResult()


class VehicleEntryDeletionTests(unittest.TestCase):
    def test_requires_reason(self):
        conn = DeletionConnection({"id": "entry-1", "item_number": 3185, "status": "AGUARDANDO_O_S"})
        with self.assertRaisesRegex(ValueError, "motivo"):
            erp_service.delete_vehicle_entry(conn, "entry-1", "PCP")
        self.assertEqual([], conn.calls)

    def test_deletes_only_clean_entry_and_reuses_latest_item_number(self):
        conn = DeletionConnection(
            {"id": "entry-1", "item_number": 3185, "status": "AGUARDANDO_O_S", "chassi": "VIN-3185"},
            {"work_order_count": 0, "purchase_order_count": 0, "allocation_count": 0,
             "forecast_count": 0, "stage_event_count": 0, "time_session_count": 0,
             "time_pause_count": 0, "stage_activity_count": 0},
        )
        result = erp_service.delete_vehicle_entry(conn, "entry-1", "PAULO", "Entrada duplicada")

        self.assertEqual("EXCLUIDA", result["status"])
        self.assertTrue(result["sequence_reused"])
        self.assertTrue(any(sql.startswith("insert into erp_audit_events") for sql, _ in conn.calls))
        self.assertTrue(any(sql.startswith("delete from erp_vehicle_entries where") for sql, _ in conn.calls))

    def test_blocks_entry_with_work_order(self):
        conn = DeletionConnection(
            {"id": "entry-1", "item_number": 3185, "status": "AGUARDANDO_O_S"},
            {"work_order_count": 1, "purchase_order_count": 0, "allocation_count": 0,
             "forecast_count": 0, "stage_event_count": 0, "time_session_count": 0,
             "time_pause_count": 0, "stage_activity_count": 0},
        )
        with self.assertRaisesRegex(ValueError, "O.S."):
            erp_service.delete_vehicle_entry(conn, "entry-1", "PCP", "Erro")
        self.assertFalse(any(sql.startswith("delete from erp_vehicle_entries") for sql, _ in conn.calls))

    def test_blocks_non_pending_status(self):
        conn = DeletionConnection({"id": "entry-1", "item_number": 3185, "status": "RETIRADA"})
        with self.assertRaisesRegex(ValueError, "aguardando O.S."):
            erp_service.delete_vehicle_entry(conn, "entry-1", "PCP", "Erro")


if __name__ == "__main__":
    unittest.main()
