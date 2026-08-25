import unittest
from pathlib import Path

import erp_service


ENTRY_ID = "5698a8e5-d17a-4d42-94be-fa2ef15fc0b7"
VEHICLE_ID = "979afffd-f0e2-426a-8f86-807c8f9cbda9"


class _Row:
    def __init__(self, values):
        self._mapping = values


class _Result:
    def __init__(self, values=None):
        self.values = values

    def first(self):
        return _Row(self.values) if self.values else None


class _Connection:
    def __init__(self, *, idempotent=False, recent=False):
        self.idempotent = idempotent
        self.recent = recent
        self.statements = []

    def execute(self, statement, params=None):
        sql = str(statement)
        self.statements.append((sql, params or {}))
        existing = {
            "id": ENTRY_ID,
            "vehicle_id": VEHICLE_ID,
            "item_number": 3161,
            "tipo_preliminar": "TRANSFORMAÇÃO",
        }
        if "where idempotency_key=:idempotency_key" in sql:
            return _Result(existing if self.idempotent else None)
        if "interval '30 seconds'" in sql:
            return _Result(existing if self.recent else None)
        return _Result()


def _payload(key=None):
    return {
        "chassi": "8AC907657VE282732",
        "data_chegada": "2026-08-25T13:49:00-03:00",
        "cliente_nome": "BELISA",
        "origem": "MES",
        "avarias": "NÃO",
        "modelo_veicular": "PACK",
        "tipo_preliminar": "TRANSFORMAÇÃO",
        "idempotency_key": key,
    }


class VehicleEntryIdempotencyTests(unittest.TestCase):
    def test_same_idempotency_key_returns_existing_item(self):
        conn = _Connection(idempotent=True)

        result = erp_service.create_entry(conn, _payload("request-3161"), "CM")

        self.assertEqual(result["item_number"], 3161)
        self.assertTrue(result["replayed"])
        self.assertFalse(any("insert into erp_vehicle_entries" in sql for sql, _ in conn.statements))

    def test_recent_identical_request_from_old_client_is_replayed(self):
        conn = _Connection(recent=True)

        result = erp_service.create_entry(conn, _payload(), "CM")

        self.assertEqual(result["id"], ENTRY_ID)
        self.assertTrue(result["replayed"])
        self.assertTrue(any("interval '30 seconds'" in sql for sql, _ in conn.statements))

    def test_migration_uses_nullable_unique_idempotency_key(self):
        migration = (
            Path(__file__).parent
            / "migrations"
            / "20260825_mes_vehicle_entry_idempotency.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("add column if not exists idempotency_key text", migration)
        self.assertIn("where idempotency_key is not null", migration)


if __name__ == "__main__":
    unittest.main()
