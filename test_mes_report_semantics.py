from datetime import date
import unittest

from erp_report import STAGE_HEADERS, _query_report_data, _report_row


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
        self.assertEqual(row["DATA ENTREGA"], date(2026, 8, 21))
        self.assertEqual(row["PEDIDO DE COMPRAS"], "PC 123")
        self.assertEqual(row["Nº SEQUENCIA"], "1")

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

        self.assertEqual(row["DATA ENTREGA"], date(2026, 8, 22))
        self.assertEqual(row["DATA 1"], date(2026, 8, 20))
        self.assertEqual(row["REPROGRAMA 1"], date(2026, 8, 22))

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
        self.assertEqual(row["DATA ENTREGA"], None)
        self.assertTrue(all(row[header] == "?" for header in STAGE_HEADERS))

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
