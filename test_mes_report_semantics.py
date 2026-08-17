from datetime import date, datetime, timedelta
from io import BytesIO
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from erp_report import (
    CONTROL_HEADERS,
    CORE_HEADERS,
    STAGE_HEADERS,
    _commercial_deadline,
    build_work_order_report,
    _delay_label,
    _query_report_data,
    _report_row,
)


class MesReportSemanticsTests(unittest.TestCase):
    def base_work_order(self):
        return {
            "id": "wo-1",
            "item_number": 3110,
            "numero_os": "3110",
            "status": "EM_PRODUÇÃO",
            "stage_configuration_status": "CONCLUIDA",
            "data_chegada": date(2026, 7, 24),
            "data_aprovacao": date(2026, 7, 24),
            "data_comercial_calculada": date(2026, 8, 23),
            "data_comercial_prevista": date(2026, 8, 21),
            "marca": "MERCEDES-BENZ",
            "modelo": "SPRINTER 417",
            "versao": "10,5 M³",
            "technical_status": "ABERTA",
            "purchase_orders": "",
            "pedido_compras_legacy": "PC 123",
            "numero_sequencia_legacy": "1",
            "sequenciamento_legacy": "",
            "observacoes_controle_producao": "",
            "observacoes_gerais": "",
        }

    def test_commercial_deadline_is_distinct_from_current_schedule(self):
        row = _report_row(
            self.base_work_order(),
            {},
            [{
                "nova_data": date(2026, 8, 21),
                "vigente": True,
            }],
            [],
            1,
        )

        self.assertEqual(row["DATA COMERCIAL"], date(2026, 8, 23))
        self.assertEqual(row["REPROGRAMA 1"], date(2026, 8, 21))
        self.assertEqual(row["PEDIDO DE COMPRAS"], "PC 123")
        self.assertEqual(row["Nº SEQUENCIA"], "1")
        self.assertNotIn("DATA ENTREGA", CONTROL_HEADERS)

    def test_latest_vigente_schedule_wins_without_losing_history(self):
        row = _report_row(
            self.base_work_order(),
            {},
            [
                {"nova_data": date(2026, 8, 20), "vigente": False},
                {"nova_data": date(2026, 8, 22), "vigente": True},
            ],
            [],
            2,
        )

        self.assertEqual(row["DATA 1"], date(2026, 8, 20))
        self.assertEqual(row["REPROGRAMA 1"], date(2026, 8, 22))

    def test_standard_commercial_deadline_is_calculated_from_line(self):
        work = self.base_work_order()
        work["data_comercial_calculada"] = None
        work["data_comercial_prevista"] = date(2026, 12, 31)
        work["linha"] = "LE"
        self.assertEqual(_commercial_deadline(work), date(2026, 9, 7))
        work["linha"] = "LB"
        self.assertEqual(_commercial_deadline(work), date(2026, 8, 23))

    def test_real_cycle_start_and_production_end_are_adjacent_and_distinct_from_delivery(self):
        work = self.base_work_order()
        work.update({
            "status": "ENTREGUE",
            "termino_producao": "2026-08-20T16:30:00",
            "data_entrega": "2026-08-21T09:00:00",
        })
        stages = {
            "PREP": {
                "stage_code": "PREP", "parametrizado": True, "aplicavel": True,
                "status": "CONCLUÍDA", "inicio": "2026-08-01T08:15:00",
                "termino": "2026-08-01T10:00:00",
            },
            "LIBERAÇÃO": {
                "stage_code": "LIBERAÇÃO", "parametrizado": True, "aplicavel": True,
                "status": "CONCLUÍDA", "inicio": "2026-08-20T15:00:00",
                "termino": "2026-08-20T16:30:00",
            },
        }
        row = _report_row(work, stages, [], [], 1)
        self.assertEqual(row["INÍCIO REAL DE PRODUÇÃO"], "2026-08-01T08:15:00")
        self.assertEqual(row["TÉRMINO PRODUÇÃO"], "2026-08-20T16:30:00")
        self.assertEqual(row["DATA SAÍDA"], "2026-08-21T09:00:00")
        self.assertLess(
            CORE_HEADERS.index("INÍCIO REAL DE PRODUÇÃO"),
            CORE_HEADERS.index("TÉRMINO PRODUÇÃO"),
        )

    def test_post_sale_delivery_is_explicit_in_exported_situation(self):
        work = self.base_work_order()
        work.update({"status": "ENTREGUE", "tipo_servico": "PÓS-VENDA"})
        row = _report_row(work, {}, [], [], 0)
        self.assertEqual(row["SITUAÇÃO"], "ENTREGUE PÓS-VENDAS")

    def test_other_finalization_is_explicit_in_exported_situation(self):
        work = self.base_work_order()
        work.update({"status": "FINALIZADA", "tipo_servico": "INSTALAÇÃO_DE_ACESSÓRIO"})
        row = _report_row(work, {}, [], [], 0)
        self.assertEqual(row["SITUAÇÃO"], "FINALIZADA OUTROS")

    def test_cancelled_order_is_not_reported_as_delayed(self):
        self.assertEqual(
            _delay_label(date(2026, 8, 20), date(2026, 8, 10), "CANCELADA"),
            "CANCELADA",
        )

    def test_vehicle_entry_without_work_order_is_exported_as_awaiting_os(self):
        entry = {
            "id": "entry-3112",
            "report_source": "VEHICLE_ENTRY",
            "status": "AGUARDANDO_O_S",
            "entry_status": "AGUARDANDO_O_S",
            "stage_configuration_status": "PENDENTE",
            "technical_status": "ABERTA",
            "item_number": 3112,
            "data_chegada": date(2026, 8, 5),
            "cliente_nome": "CLIENTE TESTE",
            "entry_notes": "Entrada registrada; aguardando definição da O.S.",
            "avarias": "NÃO",
            "chassi": "9V8VPFC3XTA008976",
            "marca": "MERCEDES-BENZ",
            "modelo": "SPRINTER 417",
            "versao": "FURGÃO",
            "mmv": "",
            "purchase_orders": "",
        }

        row = _report_row(entry, {}, [], [], 1)

        self.assertEqual(row["ITEM"], "JI - 3112")
        self.assertEqual(row["SITUAÇÃO"], "AGUARDANDO O.S.")
        self.assertEqual(row["DATA A CONSIDERAR"], date(2026, 8, 5))
        self.assertEqual(row["CHASSI"], "9V8VPFC3XTA008976")
        self.assertEqual(row["REPROGRAMA 1"], None)
        self.assertTrue(all(row[header] == "" for header in STAGE_HEADERS))

    def test_pre_os_pointing_is_exported_but_unfilled_entry_stages_remain_blank(self):
        entry = {
            "id": "entry-3112", "report_source": "VEHICLE_ENTRY",
            "status": "AGUARDANDO_O_S", "entry_status": "AGUARDANDO_O_S",
            "stage_configuration_status": "PENDENTE", "technical_status": "ABERTA",
            "item_number": 3112, "data_chegada": date(2026, 8, 5),
            "purchase_orders": "",
        }
        row = _report_row(entry, {
            "PREP": {
                "stage_code": "PREP", "parametrizado": True, "aplicavel": True,
                "status": "CONCLUÍDA",
            },
            "SERRA": {
                "stage_code": "SERRA", "parametrizado": False, "aplicavel": True,
                "status": "PENDENTE",
            },
        }, [], [], 1)
        self.assertEqual(row["PREP"], "S")
        self.assertEqual(row["SERRA."], "")

    def test_question_mark_is_reserved_for_open_os_awaiting_parameterization(self):
        work = self.base_work_order()
        work.update({"status": "RASCUNHO", "stage_configuration_status": "PENDENTE"})
        row = _report_row(work, {
            "PREP": {
                "stage_code": "PREP", "parametrizado": False, "aplicavel": True,
                "status": "PENDENTE",
            },
        }, [], [], 1)
        self.assertEqual(row["PREP"], "?")
        self.assertEqual(row["SERRA."], "?")

    def test_delay_text_uses_standard_deadline_language(self):
        self.assertEqual(
            _delay_label(date(2026, 8, 20), date(2026, 8, 22), "FINALIZADA"),
            "FINALIZADO COM ATRASO DE 2 DIA(S)",
        )
        self.assertEqual(
            _delay_label(date.today() + timedelta(days=3), None, "EM_PRODUÇÃO"),
            "FALTAM 3 DIA(S) PARA ATRASAR",
        )

    def test_technical_closure_without_production_end_is_not_reported_as_finished(self):
        planned = date.today() - timedelta(days=2)
        self.assertEqual(
            _delay_label(planned, None, "CONCLUIDA"),
            "EM ATRASO DE 2 DIA(S)",
        )
        self.assertEqual(
            _delay_label(planned, None, "FINALIZADA"),
            "EM ATRASO DE 2 DIA(S)",
        )

    def test_generated_workbook_is_a_snapshot_with_first_and_current_promises(self):
        work = self.base_work_order()
        work.update({
            "termino_producao": datetime(2026, 8, 20, 16, 30),
            "data_entrega": datetime(2026, 8, 21, 9, 0),
            "status": "ENTREGUE",
        })
        report_data = (
            [work],
            {"wo-1": {"PREP": {
                "stage_code": "PREP", "parametrizado": True, "aplicavel": True,
                "status": "CONCLUÍDA", "inicio": datetime(2026, 8, 1, 8, 15),
            }}},
            {"wo-1": [
                {"nova_data": date(2026, 8, 20), "vigente": False},
                {"nova_data": date(2026, 8, 22), "vigente": True},
            ]},
            {"wo-1": []},
        )
        with patch("erp_report._query_report_data", return_value=report_data):
            output, row_count, history_depth = build_work_order_report(None)
        workbook = load_workbook(BytesIO(output.getvalue()), data_only=False)
        sheet = workbook["CONTROLE PRODUÇÃO"]
        headers = [cell.value for cell in sheet[1]]
        values = {header: sheet.cell(2, index + 1).value for index, header in enumerate(headers)}

        self.assertEqual(row_count, 1)
        self.assertEqual(history_depth, 2)
        self.assertNotIn("DATA ENTREGA", headers)
        self.assertEqual(headers[-2:], ["DATA 1", "REPROGRAMA 1"])
        self.assertEqual(values["DATA 1"], datetime(2026, 8, 20))
        self.assertEqual(values["REPROGRAMA 1"], datetime(2026, 8, 22))
        self.assertEqual(values["INÍCIO REAL DE PRODUÇÃO"], datetime(2026, 8, 1, 8, 15))
        self.assertEqual(values["TÉRMINO PRODUÇÃO"], datetime(2026, 8, 20, 16, 30))
        self.assertEqual(values["DATA SAÍDA"], datetime(2026, 8, 21, 9, 0))

    def test_report_query_includes_vehicle_entries_without_work_order(self):
        class FakeRow:
            def __init__(self, value):
                self._mapping = value

        class FakeConnection:
            def __init__(self):
                self.statements = []

            def execute(self, statement, _params=None):
                sql = str(statement)
                self.statements.append(sql)
                if "where w.id is null" in sql:
                    return [FakeRow({
                        "id": "entry-3112",
                        "item_number": 3112,
                        "entry_status": "AGUARDANDO_O_S",
                        "data_chegada": date(2026, 8, 5),
                        "cliente_nome": "CLIENTE TESTE",
                        "entry_notes": "",
                        "avarias": "NÃO",
                        "chassi": "9V8VPFC3XTA008976",
                        "marca": "MERCEDES-BENZ",
                        "modelo": "SPRINTER 417",
                        "versao": "FURGÃO",
                        "mmv": "",
                    })]
                return []

        rows, stages, schedules, observations = _query_report_data(FakeConnection())

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["report_source"], "VEHICLE_ENTRY")
        self.assertEqual(rows[0]["status"], "AGUARDANDO_O_S")
        self.assertEqual(dict(stages), {})
        self.assertEqual(dict(schedules), {})
        self.assertEqual(dict(observations), {})


if __name__ == "__main__":
    unittest.main()
