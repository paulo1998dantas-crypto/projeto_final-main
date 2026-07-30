import unittest
from unittest.mock import patch

import erp_service


class FakeRow:
    def __init__(self, value):
        self._mapping = value


class FakeResult:
    def __init__(self, rows=None):
        self.rows = [FakeRow(row) for row in (rows or [])]

    def first(self):
        return self.rows[0] if self.rows else None

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    def __init__(self, exact=None, legacy=None):
        self.exact = exact
        self.legacy = list(legacy or [])
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, params))
        if "pg_advisory_xact_lock" in sql:
            return FakeResult()
        if "where chassi=:chassi for update" in sql:
            return FakeResult([self.exact] if self.exact else [])
        if "where chassi_completo=false" in sql:
            return FakeResult(self.legacy)
        return FakeResult()


class VehicleChassisTests(unittest.TestCase):
    def test_normalizes_complete_vin(self):
        self.assertEqual(
            erp_service._normalize_chassis(" 9bw-zzz377-vt004251 "),
            "9BWZZZ377VT004251",
        )
        self.assertTrue(erp_service._is_complete_vin("9BWZZZ377VT004251"))

    def test_reuses_exact_vehicle_without_legacy_lookup(self):
        existing = {
            "id": "vehicle-1",
            "chassi": "9BWZZZ377VT004251",
            "chassi_completo": True,
        }
        conn = FakeConnection(exact=existing)

        vehicle, created = erp_service._resolve_vehicle(
            conn,
            "9BWZZZ377VT004251",
            {},
        )

        self.assertFalse(created)
        self.assertEqual(vehicle["id"], "vehicle-1")
        self.assertFalse(any("chassi_completo=false" in sql for sql, _ in conn.calls))

    def test_promotes_single_legacy_vehicle_and_keeps_same_id(self):
        legacy = {
            "id": "legacy-vehicle",
            "chassi": "VT004251",
            "chassi_completo": False,
            "legacy_chassi_reduzido": "VT004251",
            "marca": "",
            "modelo": "",
            "versao": "",
            "mmv": "",
        }
        conn = FakeConnection(legacy=[legacy])

        vehicle, created = erp_service._resolve_vehicle(
            conn,
            "9BWZZZ377VT004251",
            {},
        )

        self.assertFalse(created)
        self.assertEqual(vehicle["id"], "legacy-vehicle")
        self.assertEqual(vehicle["chassi"], "9BWZZZ377VT004251")
        self.assertTrue(vehicle["chassi_completo"])
        self.assertIsNone(vehicle["legacy_chassi_reduzido"])
        promotion = [
            params for sql, params in conn.calls
            if sql.startswith("update erp_vehicles")
        ]
        self.assertEqual(
            promotion,
            [{"chassi": "9BWZZZ377VT004251", "id": "legacy-vehicle"}],
        )

    def test_rejects_ambiguous_legacy_suffix(self):
        conn = FakeConnection(legacy=[
            {"id": "legacy-1"},
            {"id": "legacy-2"},
        ])

        with self.assertRaisesRegex(ValueError, "vários veículos legados"):
            erp_service._resolve_vehicle(conn, "9BWZZZ377VT004251", {})

        self.assertFalse(any(sql.startswith("update erp_vehicles") for sql, _ in conn.calls))

    def test_rejects_new_reduced_chassis(self):
        conn = FakeConnection()

        with self.assertRaisesRegex(ValueError, "17 caracteres"):
            erp_service._resolve_vehicle(conn, "VT004251", {})

        self.assertFalse(any(sql.startswith("insert into erp_vehicles") for sql, _ in conn.calls))

    @patch("erp_service._id", return_value="new-vehicle")
    def test_creates_new_complete_vehicle_with_new_contract(self, _mock_id):
        conn = FakeConnection()

        vehicle, created = erp_service._resolve_vehicle(
            conn,
            "9BWZZZ377VT004251",
            {
                "marca": "VOLKSWAGEN",
                "modelo": "CRAFTER",
                "versao": "FURGÃO",
            },
        )

        self.assertTrue(created)
        self.assertEqual(vehicle["id"], "new-vehicle")
        inserts = [
            params for sql, params in conn.calls
            if sql.startswith("insert into erp_vehicles")
        ]
        self.assertEqual(len(inserts), 1)
        self.assertTrue(inserts[0]["chassi_completo"])
        self.assertIsNone(inserts[0]["legacy_chassi_reduzido"])


if __name__ == "__main__":
    unittest.main()
