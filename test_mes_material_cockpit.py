"""Material cockpit semantics: O.S. commitment versus global free balance."""
import json
import unittest
from uuid import uuid4

from sqlalchemy import create_engine, text

import erp_service


WORK = str(uuid4())
OTHER = str(uuid4())


class MaterialCockpitTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        with self.engine.begin() as conn:
            for statement in (
                "create table erp_work_orders(id text primary key, numero_os text, vehicle_entry_id text)",
                "create table suprimentos_documentos(id integer primary key, tipo text, numero text, erp_work_order_id text, composicao text, status text, updated_at text)",
                "create table skus(id integer primary key, sku text, descricao text, unidade text, active boolean)",
                "create table stock_balances(id integer primary key, sku_id integer, saldo_atual numeric)",
                "create table movements(id integer primary key, sku_id integer, tipo text, quantidade numeric, related_movement_id integer, work_order_id text, movement_status text)",
                "create table erp_purchase_orders(id integer primary key, work_order_id text, vehicle_entry_id text, data_necessidade text, numero_oc text, fornecedor_nome text, status text)",
                "create table erp_purchase_order_lines(id integer primary key, purchase_order_id integer, sku_id integer, sku_codigo text, quantidade_pedida numeric, quantidade_recebida numeric, data_necessidade text, status text, work_order_id text)",
            ):
                conn.execute(text(statement))
            conn.execute(text(
                "insert into erp_work_orders values(:id,'4100','entry-1')"
            ), {"id": WORK})
            composition = [
                {"item": "A", "codigo": "1001", "qtd": 2},
                {"item": "B", "codigo": "1002", "qtd": 2},
                {"item": "C", "codigo": "1003", "qtd": 4},
                {"item": "D", "codigo": "1004", "qtd": 5},
            ]
            conn.execute(text(
                "insert into suprimentos_documentos values(1,'os','4100',:work,:composition,'emitido','2026-09-21')"
            ), {"work": WORK, "composition": json.dumps(composition)})
            for sku_id, sku, saldo in (
                (1, "1001", 10), (2, "1002", 10), (3, "1003", 1), (4, "1004", 0)
            ):
                conn.execute(text(
                    "insert into skus values(:id,:sku,:description,'UN',1)"
                ), {"id": sku_id, "sku": sku, "description": f"Material {sku}"})
                conn.execute(text(
                    "insert into stock_balances(sku_id,saldo_atual) values(:id,:saldo)"
                ), {"id": sku_id, "saldo": saldo})
            # 1001 is fully committed to this O.S. and remains attended even
            # while another O.S. also has an active commitment.
            movements = [
                (1, 1, "EMPENHO", 2, WORK),
                (2, 1, "EMPENHO", 3, OTHER),
                # 1002 has free stock, but no commitment to this O.S.
                (3, 2, "EMPENHO", 2, OTHER),
                # 1003 has only a partial commitment to this O.S.
                (4, 3, "EMPENHO", 2, WORK),
            ]
            for movement_id, sku_id, kind, quantity, movement_work in movements:
                conn.execute(text(
                    "insert into movements values(:id,:sku,:kind,:quantity,null,:work,'ATIVA')"
                ), {"id": movement_id, "sku": sku_id, "kind": kind,
                    "quantity": quantity, "work": movement_work})
            conn.execute(text(
                "insert into erp_purchase_orders values(1,:work,'entry-1','2026-10-05','OC-100','Fornecedor','EMITIDA')"
            ), {"work": WORK})
            conn.execute(text(
                "insert into erp_purchase_order_lines values(1,1,4,'1004',5,0,'2026-10-05','PENDENTE',:work)"
            ), {"work": WORK})
        self.addCleanup(self.engine.dispose)

    def test_status_distinguishes_own_commitment_from_free_stock(self):
        with self.engine.connect() as conn:
            result = erp_service.work_order_material_cockpit(conn, WORK)

        rows = {row["codigo"]: row for row in result["items"]}
        self.assertEqual("ATENDIDO", rows["1001"]["status"])
        self.assertEqual(2.0, rows["1001"]["empenhado_na_os"])
        self.assertEqual("HA_SALDO", rows["1002"]["status"])
        self.assertEqual(0.0, rows["1002"]["empenhado_na_os"])
        self.assertEqual("EMPENHO_PARCIAL", rows["1003"]["status"])
        self.assertEqual("PREVISAO_TOTAL", rows["1004"]["status"])
        self.assertEqual(1, result["summary"]["atendidos"])
        self.assertEqual(1, result["summary"]["ha_saldo"])
        self.assertEqual(1, result["summary"]["empenho_parcial"])
        self.assertEqual(5.0, result["summary"]["em_transito_total"])


if __name__ == "__main__":
    unittest.main()
