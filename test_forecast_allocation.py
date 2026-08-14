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
    def __init__(self, forecast_status="ATIVO"):
        self.forecast_status = forecast_status
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "from erp_vehicle_entries where id=:id for update" in sql:
            return FakeResult({"item_number": 3113, "data_chegada": None})
        if "from erp_work_orders" in sql and "vehicle_entry_id=:id" in sql:
            return FakeResult()
        if "from suprimentos_forecasts where id=:id for update" in sql:
            return FakeResult({
                "id": "forecast-1", "codigo": "FCT-00001", "status": self.forecast_status,
                "vehicle_entry_id": None, "work_order_id": None,
            })
        if sql.startswith("update suprimentos_forecasts"):
            return FakeResult(rowcount=1)
        return FakeResult()


class ForecastAllocationTests(unittest.TestCase):
    @patch("erp_service._ensure_stage_rows")
    @patch("erp_service._id", return_value="work-1")
    def test_allocates_active_forecast_without_matching_chassis(self, _new_id, _stages):
        conn = ForecastConnection()

        result = erp_service.create_work_order(
            conn,
            "entry-real-vehicle",
            {"forecast_id": "forecast-1", "cliente_nome": "Cliente real"},
            "PCP",
        )

        self.assertFalse(result["replayed"])
        self.assertEqual(result["forecast_id"], "forecast-1")
        self.assertEqual(result["forecast_codigo"], "FCT-00001")
        allocation = [
            params for sql, params in conn.calls
            if sql.startswith("update suprimentos_forecasts")
        ]
        self.assertEqual(len(allocation), 1)
        self.assertEqual(allocation[0]["entry_id"], "entry-real-vehicle")
        self.assertEqual(allocation[0]["work_id"], "work-1")
        self.assertFalse(any("chassi" in sql for sql, _ in conn.calls if "suprimentos_forecasts" in sql))
        work_order_insert = next(
            sql for sql, _ in conn.calls if sql.startswith("insert into erp_work_orders")
        )
        self.assertIn("status", work_order_insert)
        self.assertIn("'aguardando_o_s'", work_order_insert)
        history_insert = next(
            sql for sql, _ in conn.calls
            if sql.startswith("insert into erp_work_order_status_history")
        )
        self.assertIn("'aguardando_o_s'", history_insert)
        self.assertEqual(result["status"], "AGUARDANDO_O_S")

    @patch("erp_service._ensure_stage_rows")
    def test_rejects_non_active_forecast_before_creating_work_order(self, _stages):
        conn = ForecastConnection(forecast_status="CONVERTIDO")

        with self.assertRaisesRegex(ValueError, "nao esta ativo"):
            erp_service.create_work_order(conn, "entry-1", {"forecast_id": "forecast-1"}, "PCP")

        self.assertFalse(any(sql.startswith("insert into erp_work_orders") for sql, _ in conn.calls))


if __name__ == "__main__":
    unittest.main()
