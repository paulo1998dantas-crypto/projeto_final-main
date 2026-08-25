"""Regression guards for mobile MES stage pointing persistence.

These tests intentionally do not connect to a database.  They protect the
contract that a mobile fields-autosave can never carry a stale status back to
the canonical ERP stage row.
"""
import asyncio
import datetime
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

import erp_service


class MesStageWriteSafetyTests(unittest.TestCase):
    def test_old_mobile_autosave_is_routed_to_metadata_only(self):
        expected = {"metadata_only": True}
        with patch.object(
            erp_service,
            "update_stage_metadata",
            return_value=expected,
        ) as metadata:
            result = erp_service.update_stage(
                object(),
                "work-order",
                "VIDROS",
                {"registrar_historico": False, "input_code": "N"},
                "OPERADOR",
            )

        self.assertEqual(result, expected)
        metadata.assert_called_once()

    def test_explicit_metadata_flag_is_routed_before_any_status_write(self):
        with patch.object(
            erp_service,
            "update_stage_metadata",
            return_value={"metadata_only": True},
        ) as metadata:
            erp_service.update_stage(
                object(),
                "work-order",
                "A/C",
                {"metadata_only": True, "input_code": "N"},
                "OPERADOR",
            )

        metadata.assert_called_once()

    def test_status_normalization_accepts_ui_and_canonical_values(self):
        self.assertEqual(erp_service._stage_status_from_input("S"), "CONCLUÍDA")
        self.assertEqual(erp_service._stage_status_from_input("PARCIAL"), "EM_ANDAMENTO")
        self.assertEqual(erp_service._stage_status_from_input("EM_ANDAMENTO"), "EM_ANDAMENTO")
        self.assertEqual(erp_service._stage_status_from_input("N/A"), "NÃO_APLICÁVEL")
        self.assertEqual(erp_service._stage_status_from_input("CONCLUIDA"), "CONCLUÍDA")

    def test_fields_only_service_does_not_update_stage_status(self):
        source = inspect.getsource(erp_service.update_stage_metadata)
        update_sql = source.split("insert into erp_work_order_stage_events", 1)[0]
        self.assertNotIn("status=", update_sql)
        self.assertIn("'METADADOS'", source)

    def test_status_writer_has_conflict_and_reopen_guards(self):
        source = inspect.getsource(erp_service.update_stage)
        self.assertIn("expected_status", source)
        self.assertIn("confirmed_status_change", source)
        self.assertIn("_has_operational_pointing", source)
        self.assertIn("StageConflictError", source)
        self.assertIn("reopen_reason", source)
        self.assertIn("REABERTURA", source)

    def test_stale_expected_status_cannot_execute_a_stage_write(self):
        connection = Mock()
        work = {"status": "ATIVA", "vehicle_entry_id": "entry"}
        stage = {
            "id": "stage",
            "status": erp_service._stage_status_from_input("S"),
            "inicio": None,
            "termino": None,
        }
        with patch.object(
            erp_service,
            "_locked_work_and_stage",
            return_value=(work, stage),
        ):
            with self.assertRaises(erp_service.StageConflictError):
                erp_service.update_stage(
                    connection,
                    "work-order",
                    "VIDROS",
                    {"input_code": "S", "expected_status": "N"},
                    "OPERADOR",
                )

        connection.execute.assert_not_called()

    def test_finalized_work_order_still_requires_confirmation_for_a_prior_pointing(self):
        connection = Mock()
        work = {"status": "FINALIZADA", "vehicle_entry_id": "entry"}
        stage = {
            "id": "stage",
            "status": erp_service._stage_status_from_input("S"),
            "inicio": None,
            "termino": None,
        }
        with (
            patch.object(
                erp_service,
                "_locked_work_and_stage",
                return_value=(work, stage),
            ),
            patch.object(erp_service, "_has_operational_pointing", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "Confirme a alteracao"):
                erp_service.update_stage(
                    connection,
                    "work-order",
                    "LIBERAÇÃO",
                    {"input_code": "N", "expected_status": "S", "reopen_reason": "teste"},
                    "OPERADOR",
                )

        connection.execute.assert_not_called()

    def test_finalized_work_order_allows_any_stage_pointing_without_reopening_work(self):
        connection = Mock()
        work = {"status": "FINALIZADA", "vehicle_entry_id": "entry"}
        stage = {
            "id": "stage",
            "status": erp_service._stage_status_from_input("N"),
            "parametrizado": True,
            "aplicavel": True,
            "inicio": None,
            "termino": None,
        }
        with (
            patch.object(
                erp_service,
                "_locked_work_and_stage",
                return_value=(work, stage),
            ),
            patch.object(erp_service, "_has_operational_pointing", return_value=False),
        ):
            result = erp_service.update_stage(
                connection,
                "work-order",
                "VIDROS",
                {"input_code": "S", "expected_status": "N"},
                "OPERADOR",
            )

        self.assertEqual(result["status"], "CONCLUÍDA")
        self.assertEqual(result["work_order_status"], "FINALIZADA")
        self.assertTrue(connection.execute.called)

    def test_delivered_or_withdrawn_work_order_rejects_new_stage_pointing(self):
        for work_status in ("ENTREGUE", "RETIRADA"):
            with self.subTest(work_status=work_status):
                connection = Mock()
                work = {"status": work_status, "vehicle_entry_id": "entry"}
                stage = {
                    "id": "stage",
                    "status": erp_service._stage_status_from_input("N"),
                    "inicio": None,
                    "termino": None,
                }
                with patch.object(
                    erp_service,
                    "_locked_work_and_stage",
                    return_value=(work, stage),
                ):
                    with self.assertRaisesRegex(
                        erp_service.StageConflictError,
                        "entregue ou retirado",
                    ):
                        erp_service.update_stage(
                            connection,
                            "work-order",
                            "VIDROS",
                            {"input_code": "S", "expected_status": "N"},
                            "OPERADOR",
                        )

                connection.execute.assert_not_called()

    def test_reopening_clears_a_stale_finish_value_from_the_browser(self):
        connection = Mock()
        old_finish = datetime.datetime(2026, 8, 3, 11, 22, 15)
        work = {"status": "ATIVA", "vehicle_entry_id": "entry"}
        stage = {
            "id": "stage",
            "status": erp_service._stage_status_from_input("S"),
            "parametrizado": True,
            "aplicavel": True,
            "inicio": old_finish,
            "termino": old_finish,
        }
        with (
            patch.object(
                erp_service,
                "_locked_work_and_stage",
                return_value=(work, stage),
            ),
            patch.object(erp_service, "_has_operational_pointing", return_value=True),
        ):
            erp_service.update_stage(
                connection,
                "work-order",
                "VIDROS",
                {
                    "input_code": "N",
                    "expected_status": "S",
                    "confirmed_status_change": True,
                    "reopen_reason": "Correção de apontamento",
                    "termino": old_finish.isoformat(),
                },
                "OPERADOR",
            )

        update_values = connection.execute.call_args_list[0].args[1]
        self.assertTrue(update_values["clear_finish"])

    def test_existing_status_change_requires_explicit_confirmation(self):
        connection = Mock()
        work = {"status": "ATIVA", "vehicle_entry_id": "entry"}
        stage = {
            "id": "stage",
            "status": erp_service._stage_status_from_input("N"),
            "parametrizado": True,
            "aplicavel": True,
            "inicio": None,
            "termino": None,
        }
        with (
            patch.object(
                erp_service,
                "_locked_work_and_stage",
                return_value=(work, stage),
            ),
            patch.object(erp_service, "_has_operational_pointing", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "Confirme a alteracao"):
                erp_service.update_stage(
                    connection,
                    "work-order",
                    "VIDROS",
                    {"input_code": "S", "expected_status": "N"},
                    "OPERADOR",
                )

        connection.execute.assert_not_called()

    def test_first_operational_pointing_after_parametrization_needs_no_confirmation(self):
        """Initial N/P/S/N-A parametrization is not an operational pointing."""
        connection = Mock()
        work = {"status": "ATIVA", "vehicle_entry_id": "entry"}
        stage = {
            "id": "stage",
            "status": erp_service._stage_status_from_input("N"),
            "parametrizado": True,
            "aplicavel": True,
            "inicio": None,
            "termino": None,
        }
        with (
            patch.object(
                erp_service,
                "_locked_work_and_stage",
                return_value=(work, stage),
            ),
            patch.object(erp_service, "_has_operational_pointing", return_value=False),
            patch.object(erp_service, "recalculate_work_order_sequences"),
        ):
            erp_service.update_stage(
                connection,
                "work-order",
                "VIDROS",
                {"input_code": "S", "expected_status": "N"},
                "OPERADOR",
            )

        self.assertTrue(connection.execute.called)

    def test_release_is_blocked_until_other_applicable_stages_are_done(self):
        connection = Mock()
        work = {"status": "EM_PRODUÇÃO", "vehicle_entry_id": "entry"}
        stage = {
            "id": "release-stage",
            "status": erp_service._stage_status_from_input("N"),
            "parametrizado": True,
            "aplicavel": True,
            "inicio": None,
            "termino": None,
        }
        with (
            patch.object(
                erp_service,
                "_locked_work_and_stage",
                return_value=(work, stage),
            ),
            patch.object(
                erp_service,
                "_unfinished_applicable_stage_codes",
                return_value=["EXPE", "ELÉTRICA"],
            ) as pending_stages,
            patch.object(erp_service, "_has_operational_pointing", return_value=True),
        ):
            with self.assertRaisesRegex(ValueError, "EXPE, ELÉTRICA"):
                erp_service.update_stage(
                    connection,
                    "work-order",
                    "LIBERAÇÃO",
                    {
                        "input_code": "S",
                        "expected_status": "N",
                        "confirmed_status_change": True,
                    },
                    "OPERADOR",
                )

        pending_stages.assert_called_once_with(
            connection,
            "work-order",
            completing_stage_id="release-stage",
        )
        connection.execute.assert_not_called()

    def test_release_pending_check_ignores_accessory_and_plotting(self):
        class _Row:
            def __init__(self, **values):
                self._mapping = values

        connection = Mock()
        connection.execute.return_value = [
            _Row(id="release", stage_code="LIBERAÇÃO", status="PENDENTE", aplicavel=True),
            _Row(id="bco", stage_code="BCO", status="PENDENTE", aplicavel=True),
            _Row(id="accessory", stage_code="ACESSÓRIO", status="PENDENTE", aplicavel=True),
            _Row(id="plotting", stage_code="PLOTAGEM", status="PENDENTE", aplicavel=True),
        ]

        self.assertEqual(
            erp_service._unfinished_applicable_stage_codes(
                connection,
                "work-order",
                completing_stage_id="release",
            ),
            ["BCO"],
        )

    def test_cycle_end_is_visible_only_after_the_work_order_is_closed(self):
        start = datetime.datetime(2026, 8, 3, 8, 22)
        release_finish = datetime.datetime(2026, 8, 5, 14, 38)
        stages = [
            {"stage_code": "DESMONT", "status": "CONCLUÍDA", "inicio": start, "termino": start},
            {"stage_code": "LIBERAÇÃO", "status": "CONCLUÍDA", "inicio": release_finish, "termino": release_finish},
        ]
        active = {"status": "EM_PRODUÇÃO", "termino_producao": None}
        closed = {"status": "FINALIZADA", "termino_producao": release_finish}

        self.assertEqual(
            erp_service.productive_cycle_window(active, stages),
            (start, None),
        )
        self.assertEqual(
            erp_service.productive_cycle_window(closed, stages),
            (start, release_finish),
        )

    def test_mobile_page_serializes_and_separates_its_requests(self):
        template = Path(__file__).with_name("templates") / "detalhes.html"
        source = template.read_text(encoding="utf-8")
        self.assertIn("const stageQueues = new WeakMap()", source)
        self.assertIn("enqueueStage(row", source)
        self.assertIn("payload.metadata_only = true", source)
        self.assertIn("/stage-details/", source)
        self.assertIn("status: status", source)
        self.assertIn("expected_status: erpInputCode(row.dataset.status", source)
        self.assertIn("data-has-operational-pointing", source)
        self.assertIn("hasOperationalPointing", source)
        self.assertIn("requiresConfirmation", source)
        self.assertIn("Tem certeza que gostaria de alterar o apontamento", source)
        self.assertIn("payload.confirmed_status_change = true", source)

    def test_management_page_sends_expected_status(self):
        template = Path(__file__).with_name("templates") / "gestao_os.html"
        source = template.read_text(encoding="utf-8")
        self.assertIn("data-input-code=", source)
        self.assertIn("data-has-operational-pointing", source)
        self.assertIn("expected_status:previousCode", source)
        self.assertIn("requiresConfirmation", source)
        self.assertIn("reopen_reason", source)
        self.assertIn("Tem certeza que gostaria de alterar o apontamento", source)
        self.assertIn("payload.confirmed_status_change=true", source)
        self.assertGreaterEqual(
            source.count("data-input-code="),
            source.count("function renderStages()"),
        )

    def test_management_page_keeps_finalized_stages_editable(self):
        template = Path(__file__).with_name("templates") / "gestao_os.html"
        source = template.read_text(encoding="utf-8")
        self.assertIn(
            "const isClosedWork=work=>['ENTREGUE','RETIRADA','CANCELADA','ARQUIVADA'].includes(work.status);",
            source,
        )
        self.assertIn(
            "Produção finalizada: apontamentos e correções permanecem permitidos",
            source,
        )

    def test_closing_form_starts_without_an_unsaved_finalization(self):
        template = Path(__file__).with_name("templates") / "gestao_os.html"
        source = template.read_text(encoding="utf-8")
        self.assertIn("Selecione uma ação de encerramento", source)
        self.assertIn("Nenhum encerramento é gravado automaticamente", source)
        self.assertIn("resetClosingForm()", source)
        self.assertIn("Selecione a ação de encerramento antes de registrar", source)

    def test_http_route_fails_closed_when_a_status_page_is_stale(self):
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        self.assertIn("Esta tela esta desatualizada", source)
        self.assertIn("status_code=409", source)
        self.assertIn("stage-details/{stage_code:path}", source)

    def test_http_route_rejects_missing_expected_status_before_database_write(self):
        # Import is intentionally local: this test uses the application route
        # but exits before opening a database transaction.
        import main

        user = type("User", (), {"nome": "OPERADOR"})()
        with (
            patch.object(main, "erp_feature_enabled", return_value=True),
            patch.object(main, "require_login", return_value=user),
            patch.object(main, "has_permission", return_value=True),
        ):
            response = asyncio.run(main.erp_stage(
                "work-order",
                "VIDROS",
                object(),
                {"input_code": "S"},
                object(),
            ))

        self.assertEqual(response.status_code, 409)
        self.assertIn(b"tela esta desatualizada", response.body)

    def test_http_route_forwards_a_fresh_status_command_to_the_domain_service(self):
        import main

        user = type("User", (), {"nome": "OPERADOR"})()
        connection = object()

        class FakeTransaction:
            def __enter__(self):
                return connection

            def __exit__(self, exc_type, exc, traceback):
                return False

        with (
            patch.object(main, "erp_feature_enabled", return_value=True),
            patch.object(main, "require_login", return_value=user),
            # O operador recebe somente mes.stage.write; ele não precisa da
            # permissão de PCP para registrar uma etapa produtiva.
            patch.object(
                main,
                "has_permission",
                side_effect=lambda _user, permission: permission == main.authz.MES_STAGE_WRITE,
            ),
            patch.object(main.database.engine, "begin", return_value=FakeTransaction()),
            patch.object(
                main.erp_service,
                "update_stage",
                return_value={"status": "CONCLUÍDA", "input_code": "S"},
            ) as update_stage,
        ):
            response = asyncio.run(main.erp_stage(
                "work-order",
                "VIDROS",
                object(),
                {
                    "input_code": "S",
                    "expected_status": "N",
                    "confirmed_status_change": True,
                },
                object(),
            ))

        self.assertTrue(response["ok"])
        self.assertEqual(response["status"], "CONCLUÍDA")
        update_stage.assert_called_once_with(
            connection,
            "work-order",
            "VIDROS",
            {
                "input_code": "S",
                "expected_status": "N",
                "confirmed_status_change": True,
            },
            "OPERADOR",
        )

    def test_legacy_autosave_cannot_revert_a_just_completed_stage(self):
        """Regression for the exact mobile N -> S -> stale-N sequence.

        The legacy screen is still supported during the MES transition.  Its
        old fields autosave carries ``registrar_historico=False`` and may also
        carry the stale input code.  It must update metadata only.
        """
        import main

        class FakeQuery:
            def __init__(self, result):
                self.result = result

            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return self.result

        class FakeSession:
            def __init__(self, stage):
                self.stage = stage
                self.commits = 0
                self.added = []

            def query(self, model):
                result = self.stage if model is main.models.Apontamento else None
                return FakeQuery(result)

            def add(self, item):
                self.added.append(item)

            def commit(self):
                self.commits += 1

        user = type("User", (), {"nome": "OPERADOR"})()
        stage = SimpleNamespace(
            status=main.normalize_status("N"),
            responsavel="",
            inicio=None,
            termino=None,
            localizacao=None,
        )
        session = FakeSession(stage)
        guard_patches = (
            patch.object(main, "require_login", return_value=user),
            patch.object(main, "has_permission", return_value=True),
            patch.object(main, "legacy_operational_schema_available", return_value=True),
        )

        with guard_patches[0], guard_patches[1], guard_patches[2]:
            completed = asyncio.run(main.salvar(
                object(),
                {
                    "chassi": "9V8VPFC3XTA008976",
                    "etapa": "DESMONT",
                    "status": "S",
                    "expected_status": "N",
                    "confirmed_status_change": True,
                },
                session,
            ))
            stale_autosave = asyncio.run(main.salvar(
                object(),
                {
                    "chassi": "9V8VPFC3XTA008976",
                    "etapa": "DESMONT",
                    "input_code": "N",
                    "registrar_historico": False,
                    "responsavel": "OPERADOR MOBILE",
                },
                session,
            ))

        self.assertEqual(completed["stage_status"], main.normalize_status("S"))
        self.assertTrue(stale_autosave["metadata_only"])
        self.assertEqual(stale_autosave["stage_status"], main.normalize_status("S"))
        self.assertEqual(stage.status, main.normalize_status("S"))
        self.assertEqual(stage.responsavel, "OPERADOR MOBILE")

    def test_legacy_metadata_autosave_works_without_any_status_field(self):
        import main

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return stage

        class FakeSession:
            def __init__(self):
                self.commits = 0

            def query(self, _model):
                return FakeQuery()

            def commit(self):
                self.commits += 1

        user = type("User", (), {"nome": "OPERADOR"})()
        stage = SimpleNamespace(
            status=main.normalize_status("S"),
            responsavel="ANTES",
            inicio=None,
            termino=None,
            localizacao=None,
        )
        session = FakeSession()
        with (
            patch.object(main, "require_login", return_value=user),
            patch.object(main, "has_permission", return_value=True),
            patch.object(main, "legacy_operational_schema_available", return_value=True),
        ):
            result = asyncio.run(main.salvar(
                object(),
                {
                    "chassi": "9V8VPFC3XTA008976",
                    "etapa": "DESMONT",
                    "metadata_only": True,
                    "responsavel": "DEPOIS",
                },
                session,
            ))

        self.assertTrue(result["metadata_only"])
        self.assertEqual(result["stage_status"], main.normalize_status("S"))
        self.assertEqual(stage.status, main.normalize_status("S"))
        self.assertEqual(stage.responsavel, "DEPOIS")
        self.assertEqual(session.commits, 1)

    def test_legacy_status_change_requires_confirmation_before_mutation(self):
        import main

        class FakeQuery:
            def filter(self, *_args):
                return self

            def with_for_update(self):
                return self

            def first(self):
                return stage

        class FakeSession:
            def __init__(self):
                self.commits = 0

            def query(self, _model):
                return FakeQuery()

            def commit(self):
                self.commits += 1

        user = type("User", (), {"nome": "OPERADOR"})()
        stage = SimpleNamespace(
            status=main.normalize_status("N"),
            responsavel="",
            inicio=None,
            termino=None,
            localizacao=None,
        )
        session = FakeSession()
        with (
            patch.object(main, "require_login", return_value=user),
            patch.object(main, "has_permission", return_value=True),
            patch.object(main, "legacy_operational_schema_available", return_value=True),
        ):
            response = asyncio.run(main.salvar(
                object(),
                {
                    "chassi": "9V8VPFC3XTA008976",
                    "etapa": "DESMONT",
                    "status": "S",
                    "expected_status": "N",
                },
                session,
            ))

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Confirme a alteracao", response.body)
        self.assertEqual(stage.status, main.normalize_status("N"))
        self.assertEqual(session.commits, 0)

    def test_legacy_stage_route_has_the_same_write_guards(self):
        source = Path(__file__).with_name("main.py").read_text(encoding="utf-8")
        self.assertIn('data.get("status") or data.get("input_code")', source)
        self.assertIn(').with_for_update().first()', source)
        self.assertIn('"stage_status": st', source)
        self.assertIn('expected_raw = data.get("expected_status")', source)
        self.assertIn('confirmed_status_change") is not True', source)


if __name__ == "__main__":
    unittest.main()
