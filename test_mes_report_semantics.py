from datetime import date
import unittest

from erp_report import _report_row


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


if __name__ == "__main__":
    unittest.main()
