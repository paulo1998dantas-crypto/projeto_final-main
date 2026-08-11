from datetime import date
from unittest.mock import patch
import unittest

import erp_service


class RecordingConnection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((statement, params or {}))
        return None


class MesScheduleTests(unittest.TestCase):
    def test_reschedule_uses_a_valid_sqlalchemy_date_bind(self):
        connection = RecordingConnection()

        with (
            patch.object(erp_service, "_sequence_schema_ready", return_value=True),
            patch.object(erp_service, "recalculate_work_order_sequences"),
        ):
            result = erp_service.reschedule(
                connection,
                "work-order-id",
                date(2026, 8, 20),
                "Ajuste solicitado pelo cliente",
                "PCP",
            )

        stage_statement = next(
            statement
            for statement, _ in connection.calls
            if "semana_planejada" in str(statement)
        )
        self.assertIn("cast(:date as date)", str(stage_statement))
        self.assertNotIn(":date::date", str(stage_statement))
        self.assertEqual({"date", "id"}, set(stage_statement._bindparams))
        self.assertEqual(date(2026, 8, 20), result["data_comercial_prevista"])

    def test_reschedule_requires_date_and_reason_before_any_write(self):
        for new_date, reason in ((None, "motivo"), (date(2026, 8, 20), "  ")):
            connection = RecordingConnection()
            with self.assertRaises(ValueError):
                erp_service.reschedule(
                    connection,
                    "work-order-id",
                    new_date,
                    reason,
                    "PCP",
                )
            self.assertEqual([], connection.calls)


if __name__ == "__main__":
    unittest.main()
