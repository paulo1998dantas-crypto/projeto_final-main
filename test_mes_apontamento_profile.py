import unittest
import inspect
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

import authz
import erp_service
import main
from starlette.routing import Match


class ApontamentoProfileTests(unittest.TestCase):
    def test_role_is_limited_to_mes_pointing(self):
        self.assertEqual(
            authz._default_permissions({"APONTAMENTO"}),
            frozenset({authz.MES_DASHBOARD_READ, authz.MES_STAGE_WRITE}),
        )
        user = authz.Principal(
            id=10, nome="Operador", username="operador", active=True,
            auth_version=1, roles=frozenset({"APONTAMENTO"}),
            permissions=authz._default_permissions({"APONTAMENTO"}),
        )
        self.assertTrue(main.is_production_only(user))
        self.assertTrue(main.can_access_production_console(user))
        self.assertFalse(user.can(authz.MES_WORK_ORDERS_MANAGE))

    def test_sector_catalog_is_the_mes_stage_catalog(self):
        sectors = [code for code, _, _ in erp_service.STAGES]
        self.assertIn("A/C", sectors)
        self.assertIn("ELÉTRICA", sectors)
        self.assertEqual(len(sectors), len(set(sectors)))
        html = (Path(__file__).parent / "templates" / "producao_setores.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("{% for sector in sectors %}", html)
        sector_route = next(
            route for route in main.app.routes
            if route.name == "pointing_sector_cards"
        )
        match, values = sector_route.matches({
            "type": "http", "method": "GET", "path": "/producao/setor/A/C"
        })
        self.assertEqual(match, Match.FULL)
        self.assertEqual(values["path_params"]["stage_code"], "A/C")

    def test_sector_cards_keep_uninitialized_pre_os_entries(self):
        cards = [
            {"target_kind": "work", "target_id": "work-1"},
            {"target_kind": "work", "target_id": "work-2"},
            {"target_kind": "entry", "target_id": "entry-1"},
        ]
        conn = Mock()
        work_rows = Mock()
        work_rows.mappings.return_value = iter([{"target_id": "work-1"}])
        entry_rows = Mock()
        entry_rows.mappings.return_value = iter([])
        initialized_rows = Mock()
        initialized_rows.mappings.return_value = iter([])
        conn.execute.side_effect = [work_rows, entry_rows, initialized_rows]
        with patch.object(erp_service, "list_production_targets", return_value=cards):
            actual = erp_service.list_sector_production_targets(conn, "A/C")
        self.assertEqual([card["target_id"] for card in actual], ["work-1", "entry-1"])
        with self.assertRaises(ValueError):
            erp_service.list_sector_production_targets(conn, "INEXISTENTE")

    def test_setup_switches_from_productive_session_without_completing_stage(self):
        conn = Mock()
        stage = {"id": "stage-1", "status": "EM_ANDAMENTO", "inicio": None,
                 "localizacao": None, "parametrizado": True, "aplicavel": True}
        with (
            patch.object(erp_service, "_stage_pause_schema_ready", return_value=True),
            patch.object(erp_service, "_setup_schema_ready", return_value=True),
            patch.object(erp_service, "_production_locked_stage",
                         return_value=("work", {"status": "ATIVA"}, stage)),
            patch.object(erp_service, "_production_event_replay", return_value=False),
            patch.object(erp_service, "_pause_summary", return_value={
                "open_pause": None, "open_session": {"id": "session-1"},
                "total_productive_seconds": 0, "total_paused_seconds": 0,
            }),
            patch.object(erp_service, "_open_stage_setup", return_value=None),
            patch.object(erp_service, "_close_stage_session", return_value=1800),
            patch.object(erp_service, "_add_auto_stage_hours") as add_hours,
        ):
            result = erp_service.execute_production_stage_command(
                conn, "work", "work-1", "A/C", {
                    "action": "SETUP", "expected_status": "P", "auto_time_fields": True,
                    "idempotency_key": "setup-1",
                }, "OPERADOR 1"
            )
        self.assertTrue(result["open_setup"])
        self.assertEqual(result["input_code"], "P")
        add_hours.assert_called_once_with(
            conn, "work", "work-1", "A/C", "production_time_hours", 1800,
            "OPERADOR 1", "setup-1",
        )

    def test_resume_closes_setup_and_starts_same_canonical_stage(self):
        conn = Mock()
        stage = {"id": "stage-1", "status": "EM_ANDAMENTO", "inicio": None,
                 "parametrizado": True, "aplicavel": True}
        setup = {"id": "setup-1", "started_at": "2026-10-04T10:00:00Z"}
        timing = {"open_pause": None, "open_session": None,
                  "total_productive_seconds": 0, "total_paused_seconds": 0}
        with (
            patch.object(erp_service, "_production_execution_operators", return_value=[]),
            patch.object(erp_service, "_stage_pause_schema_ready", return_value=True),
            patch.object(erp_service, "_setup_schema_ready", return_value=True),
            patch.object(erp_service, "_production_locked_stage",
                         return_value=("work", {"status": "ATIVA"}, stage)),
            patch.object(erp_service, "_production_event_replay", return_value=False),
            patch.object(erp_service, "_pause_summary", return_value=timing),
            patch.object(erp_service, "_open_stage_setup", return_value=setup),
            patch.object(erp_service, "_close_stage_setup", return_value=900),
            patch.object(erp_service, "_add_auto_stage_hours") as add_hours,
            patch.object(erp_service, "_open_stage_session") as open_session,
            patch.object(erp_service, "update_stage", return_value={"input_code": "P"}),
        ):
            erp_service.execute_production_stage_command(
                conn, "work", "work-1", "A/C", {
                    "action": "INICIAR", "expected_status": "P", "auto_time_fields": True,
                    "idempotency_key": "resume-1",
                }, "OPERADOR 1"
            )
        add_hours.assert_called_once_with(
            conn, "work", "work-1", "A/C", "setup_time_hours", 900,
            "OPERADOR 1", "resume-1",
        )
        open_session.assert_called_once()

    def test_pre_os_promotion_carries_setup_and_stopped_hours(self):
        source = inspect.getsource(erp_service._promote_entry_stage_pointings)
        self.assertIn("total_stopped_time_hours=:stopped_hours", source)
        self.assertIn("update erp_stage_setup_sessions", source)
        self.assertIn("update erp_stage_auto_time_counters", source)
        self.assertIn("vehicle_entry_stage_id=null", source)

    def test_short_intervals_round_only_after_accumulation(self):
        conn = Mock()
        counter_result = Mock()
        counter_result.first.return_value = SimpleNamespace(
            _mapping={"id": "counter-1", "production_seconds": 60}
        )
        conn.execute.side_effect = [Mock(), counter_result, Mock()]
        stage = {"id": "stage-1", "production_time_hours": Decimal("0.02")}
        with (
            patch.object(erp_service, "_production_locked_stage",
                         return_value=("work", {}, stage)),
            patch.object(erp_service, "update_production_manual_times") as save,
        ):
            erp_service._add_auto_stage_hours(
                conn, "work", "work-1", "A/C", "production_time_hours",
                60, "OPERADOR 1", "short-2",
            )
        self.assertEqual(save.call_args.args[4]["production_time_hours"], Decimal("0.03"))


if __name__ == "__main__":
    unittest.main()
