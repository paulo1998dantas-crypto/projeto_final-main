"""Regression coverage for production pointings made before an O.S. exists."""
import asyncio
import inspect
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

import erp_service


class MesPreOsStagePointingTests(unittest.TestCase):
    def test_additive_migration_is_idempotent_and_private(self):
        migration = (
            Path(__file__).with_name("migrations")
            / "20260810_mes_pre_os_stage_pointings.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("create table if not exists public.erp_vehicle_entry_stages", migration)
        self.assertIn("create table if not exists public.erp_vehicle_entry_stage_events", migration)
        self.assertIn("unique (vehicle_entry_id, stage_code)", migration)
        self.assertIn("idempotency_key", migration)
        self.assertIn("enable row level security", migration)
        self.assertIn("revoke all", migration)
        self.assertNotIn("drop table", migration.lower())
        self.assertNotIn("truncate", migration.lower())

    def test_vehicle_entry_creation_prepares_preliminary_stages(self):
        source = inspect.getsource(erp_service.create_entry)
        self.assertIn("_ensure_entry_stage_rows(conn, entry_id)", source)

    def test_work_order_creation_promotes_pointings_in_same_transaction(self):
        source = inspect.getsource(erp_service.create_work_order)
        self.assertIn("_promote_entry_stage_pointings", source)
        promotion = inspect.getsource(erp_service._promote_entry_stage_pointings)
        self.assertIn("for update", promotion.lower())
        self.assertIn("APONTAMENTO_PRE_OS", promotion)
        self.assertIn("PRE_OS:", promotion)
        self.assertIn("transferred_to_work_order_stage_id", promotion)

    def test_preliminary_stage_is_written_with_history(self):
        connection = Mock()
        entry = {"id": "entry-1", "status": "AGUARDANDO_O_S"}
        stage = {
            "id": "stage-1",
            "status": "PENDENTE",
            "parametrizado": False,
            "inicio": None,
            "termino": None,
            "responsavel": "",
            "localizacao": "",
            "observacoes": "",
        }
        with (
            patch.object(erp_service, "_one", side_effect=[entry, None, stage]),
            patch.object(erp_service, "_ensure_entry_stage_rows", return_value=True),
            patch.object(erp_service, "_has_entry_operational_pointing", return_value=False),
        ):
            result = erp_service.update_vehicle_entry_stage(
                connection,
                "entry-1",
                "PREP",
                {"input_code": "S", "expected_status": "?"},
                "OPERADOR",
            )

        self.assertEqual(result["input_code"], "S")
        self.assertTrue(result["has_operational_pointing"])
        sql_calls = [str(call.args[0]).lower() for call in connection.execute.call_args_list]
        self.assertTrue(any("update erp_vehicle_entry_stages" in sql for sql in sql_calls))
        self.assertTrue(any("insert into erp_vehicle_entry_stage_events" in sql for sql in sql_calls))

    def test_concurrent_work_order_opening_rejects_preliminary_write(self):
        connection = Mock()
        with (
            patch.object(
                erp_service,
                "_one",
                side_effect=[
                    {"id": "entry-1", "status": "AGUARDANDO_O_S"},
                    {"id": "work-1", "numero_os": "3115"},
                ],
            ),
            patch.object(erp_service, "_ensure_entry_stage_rows") as ensure,
        ):
            with self.assertRaisesRegex(erp_service.StageConflictError, "foi aberta"):
                erp_service.update_vehicle_entry_stage(
                    connection,
                    "entry-1",
                    "PREP",
                    {"input_code": "S", "expected_status": "?"},
                    "OPERADOR",
                )

        ensure.assert_not_called()
        self.assertEqual(connection.execute.call_count, 2)

    def test_parametrization_cannot_overwrite_promoted_execution(self):
        source = inspect.getsource(erp_service.configure_stages)
        self.assertIn("_has_operational_pointing", source)
        self.assertIn("já foi apontada", source)
        self.assertIn("preserve datas e histórico", source)

    def test_management_card_exposes_pre_os_endpoint_and_preserves_pointing(self):
        template = Path(__file__).with_name("templates") / "gestao_os.html"
        source = template.read_text(encoding="utf-8")
        self.assertIn("openEntryDetail", source)
        self.assertIn("/api/erp/vehicle-entries/${entryId}/stages", source)
        self.assertIn("state.detail?.mode==='ENTRY'", source)
        self.assertIn("Ela será preservada quando a O.S. for aberta", source)
        self.assertIn("Etapas já apontadas antes da O.S. estão preservadas", source)

    def test_pre_os_http_write_uses_stage_permission_and_expected_status(self):
        import main

        user = type("User", (), {"nome": "OPERADOR"})()
        with (
            patch.object(main, "erp_feature_enabled", return_value=True),
            patch.object(main, "require_login", return_value=user),
            patch.object(main, "has_permission", return_value=True),
        ):
            response = asyncio.run(main.erp_vehicle_entry_stage(
                "entry-1", "PREP", object(), {"input_code": "S"}, object()
            ))

        self.assertEqual(response.status_code, 409)
        self.assertIn(b"tela esta desatualizada", response.body)
        route_source = inspect.getsource(main.erp_vehicle_entry_stage)
        self.assertIn("MES_STAGE_WRITE", route_source)


if __name__ == "__main__":
    unittest.main()
