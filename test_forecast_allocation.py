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


class ForecastConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "from erp_vehicle_entries where id=:id for update" in sql:
            return FakeResult({
                "item_number": 3113, "data_chegada": None,
                "status": "AGUARDANDO_O_S", "cliente_nome": "Cliente da entrada",
            })
        if "from erp_work_orders" in sql and "vehicle_entry_id=:id" in sql:
            return FakeResult()
        return FakeResult()


class ForecastAllocationTests(unittest.TestCase):
    @patch("erp_service._ensure_stage_rows")
    @patch("erp_service._id", return_value="work-1")
    def test_legacy_forecast_payload_does_not_consume_during_allocation(self, _new_id, _stages):
        conn = ForecastConnection()

        result = erp_service.create_work_order(
            conn,
            "entry-real-vehicle",
            {"forecast_id": "forecast-1", "cliente_nome": "Cliente real"},
            "PCP",
        )

        self.assertFalse(result["replayed"])
        self.assertNotIn("forecast_id", result)
        self.assertNotIn("forecast_codigo", result)
        self.assertFalse(any("suprimentos_forecasts" in sql for sql, _ in conn.calls))
        work_order_insert, work_order_params = next(
            (sql, params) for sql, params in conn.calls if sql.startswith("insert into erp_work_orders")
        )
        self.assertIn("status", work_order_insert)
        self.assertIn("'aguardando_o_s'", work_order_insert)
        self.assertEqual(work_order_params["cliente_nome"], "Cliente da entrada")
        history_insert = next(
            sql for sql, _ in conn.calls
            if sql.startswith("insert into erp_work_order_status_history")
        )
        self.assertIn("'aguardando_o_s'", history_insert)
        self.assertEqual(result["status"], "AGUARDANDO_O_S")

if __name__ == "__main__":
    unittest.main()
