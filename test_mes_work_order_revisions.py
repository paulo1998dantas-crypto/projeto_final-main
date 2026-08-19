import unittest
from unittest.mock import patch

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


class RevisionConnection:
    def __init__(self, current):
        self.current = current
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "from erp_vehicle_entries where id=:id for update" in sql:
            return FakeResult({
                "item_number": 2922,
                "data_chegada": None,
                "status": "CANCELADA",
                "cliente_nome": "Cliente da entrada",
            })
        if "from erp_work_orders" in sql and "vehicle_entry_id=:id" in sql:
            return FakeResult(self.current)
        if sql.startswith("update erp_work_orders") and "is_current=false" in sql:
            return FakeResult(rowcount=1)
        return FakeResult()


class WorkOrderRevisionTests(unittest.TestCase):
    @patch("erp_service._promote_entry_stage_pointings")
    @patch("erp_service._ensure_stage_rows")
    @patch("erp_service._id", return_value="new-work")
    def test_replaces_cancelled_order_without_changing_item_number(
        self, _new_id, _stages, promote
    ):
        conn = RevisionConnection({
            "id": "cancelled-work",
            "numero_os": "2922",
            "status": "CANCELADA",
            "revision_number": 1,
            "is_current": True,
            "supersedes_work_order_id": None,
        })

        result = erp_service.create_work_order(conn, "entry-2922", {
            "numero_os": "9999",
            "create_replacement": True,
            "supersedes_work_order_id": "cancelled-work",
            "cliente_nome": "Nova demanda",
        }, "PCP")

        self.assertFalse(result["replayed"])
        self.assertEqual("2922", result["numero_os"])
        self.assertEqual(2, result["revision_number"])
        self.assertEqual("cancelled-work", result["supersedes_work_order_id"])
        promote.assert_not_called()

        demotion = next(
            params for sql, params in conn.calls
            if sql.startswith("update erp_work_orders") and "is_current=false" in sql
        )
        self.assertEqual("cancelled-work", demotion["previous"])
        insertion = next(
            params for sql, params in conn.calls
            if sql.startswith("insert into erp_work_orders")
        )
        self.assertEqual("2922", insertion["number"])
        self.assertEqual(2, insertion["revision"])
        self.assertEqual("cancelled-work", insertion["previous"])
        self.assertEqual("Cliente da entrada", insertion["cliente_nome"])
        self.assertTrue(any(
            sql.startswith("update erp_vehicle_entries") and params.get("id") == "entry-2922"
            for sql, params in conn.calls
        ))

    def test_retry_returns_replacement_instead_of_creating_another(self):
        conn = RevisionConnection({
            "id": "new-work",
            "numero_os": "2922",
            "status": "AGUARDANDO_O_S",
            "revision_number": 2,
            "is_current": True,
            "supersedes_work_order_id": "cancelled-work",
        })

        result = erp_service.create_work_order(conn, "entry-2922", {
            "create_replacement": True,
            "supersedes_work_order_id": "cancelled-work",
        }, "PCP")

        self.assertTrue(result["replayed"])
        self.assertEqual("new-work", result["id"])
        self.assertFalse(any(
            sql.startswith("insert into erp_work_orders") for sql, _ in conn.calls
        ))

    def test_does_not_replace_a_non_cancelled_current_order(self):
        conn = RevisionConnection({
            "id": "active-work",
            "numero_os": "2922",
            "status": "ATIVA",
            "revision_number": 1,
            "is_current": True,
            "supersedes_work_order_id": None,
        })

        with self.assertRaisesRegex(ValueError, "Somente uma O.S. cancelada"):
            erp_service.create_work_order(conn, "entry-2922", {
                "create_replacement": True,
                "supersedes_work_order_id": "different-work",
            }, "PCP")


if __name__ == "__main__":
    unittest.main()
