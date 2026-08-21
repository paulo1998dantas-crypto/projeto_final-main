import datetime
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import authz
import erp_service
import main


ROOT = Path(__file__).resolve().parent
MIGRATION = ROOT / "migrations" / "20260820_mes_production_profile_stage_pauses.sql"


def _timing_state(*, session=None, pause=None, productive=0, stopped=0):
    return {
        "open_session": session,
        "open_pause": pause,
        "total_productive_seconds": productive,
        "total_paused_seconds": stopped,
    }


class MesProductionProfileTests(unittest.TestCase):
    def test_role_has_only_the_two_shop_floor_permissions(self):
        self.assertEqual(
            authz._default_permissions({"PRODUCAO"}),
            frozenset({authz.MES_DASHBOARD_READ, authz.MES_STAGE_WRITE}),
        )

    def test_production_only_user_is_redirected_to_dedicated_console(self):
        production = authz.Principal(
            id=7,
            nome="Produção",
            username="producao",
            active=True,
            auth_version=1,
            roles=frozenset({"PRODUCAO"}),
            permissions=frozenset(
                {authz.MES_DASHBOARD_READ, authz.MES_STAGE_WRITE}
            ),
        )
        combined = authz.Principal(
            id=8,
            nome="PCP Produção",
            username="pcp-producao",
            active=True,
            auth_version=1,
            roles=frozenset({"PRODUCAO", "PCP"}),
            permissions=authz._default_permissions({"PRODUCAO", "PCP"}),
        )

        self.assertTrue(main.is_production_only(production))
        self.assertTrue(main.can_access_production_console(production))
        self.assertFalse(main.is_production_only(combined))

    def test_production_console_is_search_only_and_actions_are_explicit(self):
        cards = (ROOT / "templates" / "producao.html").read_text(encoding="utf-8")
        stages = (ROOT / "templates" / "producao_etapas.html").read_text(
            encoding="utf-8"
        )
        pointing = (ROOT / "templates" / "producao_apontamento.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('name="q"', cards)
        self.assertNotIn("<select", cards)
        for action in ("INICIAR", "PARAR", "FINALIZAR", "INTERROMPER"):
            self.assertIn(action, pointing)
        self.assertIn("Etapas disponíveis", stages)
        self.assertIn("INICIAR NOVO AJUSTE", pointing)
        self.assertIn("nunca entram no tempo produtivo", pointing)

    def test_migration_is_additive_private_and_tracks_sessions_separately(self):
        sql = MIGRATION.read_text(encoding="utf-8").lower()

        self.assertIn("create table if not exists public.erp_stage_time_sessions", sql)
        self.assertIn("create table if not exists public.erp_stage_time_pauses", sql)
        self.assertIn("enable row level security", sql)
        self.assertIn("revoke all", sql)
        self.assertIn("to service_role", sql)
        self.assertIn("insert into public.erp_permissions", sql)
        self.assertIn("created_at, updated_at", sql)
        self.assertIn("one_open_work_idx", sql)
        self.assertIn("one_open_entry_idx", sql)
        for destructive in ("drop table", "truncate", "delete from"):
            self.assertNotIn(destructive, sql)

    def test_completed_stage_reentry_adds_a_session_and_preserves_first_start(self):
        conn = Mock()
        first_start = datetime.datetime(2026, 8, 20, 11, 0)
        prior_finish = datetime.datetime(2026, 8, 20, 15, 0)
        stage = {
            "id": "stage-1",
            "stage_code": "DESMONT",
            "status": "CONCLUÍDA",
            "parametrizado": True,
            "aplicavel": True,
            "inicio": first_start,
            "termino": prior_finish,
        }
        target = {"status": "FINALIZADA", "vehicle_entry_id": "entry-1"}
        before = _timing_state(productive=14_400, stopped=3_600)
        after = _timing_state(
            session={"id": "new-session"}, productive=14_400, stopped=3_600
        )

        with (
            patch.object(erp_service, "_stage_pause_schema_ready", return_value=True),
            patch.object(
                erp_service,
                "_production_locked_stage",
                return_value=("work", target, stage),
            ),
            patch.object(erp_service, "_production_event_replay", return_value=False),
            patch.object(erp_service, "_pause_summary", side_effect=[before, after]),
            patch.object(erp_service, "_open_stage_session") as open_session,
            patch.object(
                erp_service,
                "update_stage",
                return_value={"input_code": "P", "status": "EM_ANDAMENTO"},
            ) as update_stage,
        ):
            result = erp_service.execute_production_stage_command(
                conn,
                "work",
                "work-1",
                "DESMONT",
                {
                    "action": "INICIAR",
                    "expected_status": "S",
                    "inicio": "2026-08-30T08:00:00-03:00",
                    "idempotency_key": "rework-1",
                },
                "OPERADOR 1",
            )

        open_session.assert_called_once()
        payload = update_stage.call_args.args[3]
        self.assertEqual(payload["input_code"], "P")
        self.assertEqual(
            payload["reopen_reason"],
            "Reentrada produtiva para ajuste após conclusão anterior.",
        )
        self.assertTrue(update_stage.call_args.kwargs["allow_finalized_stage_pointing"])
        self.assertEqual(stage["inicio"], first_start)
        self.assertEqual(result["total_productive_seconds"], 14_400)
        self.assertEqual(result["total_paused_seconds"], 3_600)

    def test_new_completion_closes_only_current_session_and_updates_latest_finish(self):
        conn = Mock()
        stage = {
            "id": "stage-1",
            "stage_code": "DESMONT",
            "status": "EM_ANDAMENTO",
            "parametrizado": True,
            "aplicavel": True,
            "inicio": datetime.datetime(2026, 8, 20, 11, 0),
            "termino": None,
        }
        target = {"status": "FINALIZADA", "vehicle_entry_id": "entry-1"}
        before = _timing_state(
            session={"id": "current-session", "started_at": "2026-08-30T11:00:00Z"},
            productive=14_400,
            stopped=3_600,
        )
        after = _timing_state(productive=18_000, stopped=3_600)

        with (
            patch.object(erp_service, "_stage_pause_schema_ready", return_value=True),
            patch.object(
                erp_service,
                "_production_locked_stage",
                return_value=("work", target, stage),
            ),
            patch.object(erp_service, "_production_event_replay", return_value=False),
            patch.object(erp_service, "_pause_summary", side_effect=[before, after]),
            patch.object(erp_service, "_close_stage_session") as close_session,
            patch.object(
                erp_service,
                "update_stage",
                return_value={"input_code": "S", "status": "CONCLUÍDA"},
            ) as update_stage,
        ):
            result = erp_service.execute_production_stage_command(
                conn,
                "work",
                "work-1",
                "DESMONT",
                {
                    "action": "FINALIZAR",
                    "expected_status": "P",
                    "termino": "2026-08-30T09:00:00-03:00",
                    "idempotency_key": "finish-rework-1",
                },
                "OPERADOR 1",
            )

        close_session.assert_called_once()
        payload = update_stage.call_args.args[3]
        self.assertEqual(payload["input_code"], "S")
        self.assertIsNone(payload["inicio"])
        self.assertEqual(
            payload["termino"],
            datetime.datetime(2026, 8, 30, 12, 0, tzinfo=datetime.timezone.utc),
        )
        self.assertEqual(result["total_productive_seconds"], 18_000)
        self.assertEqual(result["total_paused_seconds"], 3_600)

    def test_productive_duration_is_calculated_per_session_not_wall_clock_span(self):
        source = __import__("inspect").getsource(erp_service._close_stage_session)

        self.assertIn("started_at", source)
        self.assertIn("productive_seconds", source)
        self.assertNotIn("stage.get(\"inicio\")", source)


if __name__ == "__main__":
    unittest.main()
