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
                "create table bom_components(id integer primary key,item_sku_id integer,component_sku_id integer,quantidade numeric)",
                "create table movements(id integer primary key, sku_id integer, tipo text, quantidade numeric, related_movement_id integer, work_order_id text, movement_status text)",
                "create table erp_purchase_orders(id integer primary key, work_order_id text, vehicle_entry_id text, data_necessidade text, numero_oc text, fornecedor_nome text, status text, technical_status text)",
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
                "insert into erp_purchase_orders values(1,:work,'entry-1','2026-10-05','OC-100','Fornecedor','EMITIDA','ABERTA')"
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

    def test_global_parent_set_purchase_transit_is_exploded_to_os_component(self):
        with self.engine.begin() as conn:
            conn.execute(text("insert into skus values(5,'30180013','CJ REVESTIMENTO L3H2 VITRE','CJ',1)"))
            conn.execute(text("insert into skus values(6,'PP-REV','PP REVESTIMENTO','CJ',1)"))
            conn.execute(text("insert into bom_components values(1,5,6,2),(2,6,4,3)"))
            # The O.C. is general stock, not allocated to this O.S.; receipt
            # of the set will be backflushed into the leaf component 1004.
            conn.execute(text("update erp_purchase_orders set work_order_id=null,vehicle_entry_id=null where id=1"))
            conn.execute(text("update erp_purchase_order_lines set sku_id=5,sku_codigo='30180013',quantidade_pedida=2,quantidade_recebida=0,work_order_id=null where id=1"))

        with self.engine.connect() as conn:
            result = erp_service.work_order_material_cockpit(conn, WORK)

        component = next(row for row in result["items"] if row["codigo"] == "1004")
        self.assertEqual(12.0, component["em_chegada"])
        self.assertEqual("2026-10-05", component["previsao_chegada"])
        self.assertEqual("30180013", component["previsoes"][0]["sku_origem"])
        self.assertFalse(component["previsoes"][0]["vinculada_a_esta_os"])
        self.assertEqual(12.0, result["summary"]["em_transito_total"])

    def test_bom_commitment_and_baixa_are_reflected_on_leaf_materials(self):
        with self.engine.begin() as conn:
            conn.execute(text("delete from movements"))
            conn.execute(text("""
                insert into skus values
                  (5,'CJ-100','Conjunto pai','CJ',1),
                  (6,'CJ-200','Subconjunto intermediário','CJ',1),
                  (7,'2001','Material A','UN',1),
                  (8,'2002','Material B','UN',1),
                  (9,'CJ-DIRETO','Conjunto sem BOM','CJ',1)
            """))
            conn.execute(text("""
                insert into stock_balances(sku_id,saldo_atual) values
                  (5,0),(6,0),(7,10),(8,10),(9,10)
            """))
            conn.execute(text("""
                insert into bom_components values
                  (1,5,6,1),(2,6,7,2),(3,6,8,3)
            """))
            composition = [
                {"item": "CJ-100", "codigo": "CJ-100", "qtd": 1, "level": 0},
                {"item": "CJ-100", "codigo": "CJ-200", "qtd": 1, "level": 1},
                {"item": "CJ-200", "codigo": "2001", "qtd": 2, "level": 2},
                {"item": "CJ-200", "codigo": "2002", "qtd": 3, "level": 2},
                {"item": "CJ-DIRETO", "codigo": "CJ-DIRETO", "qtd": 1, "level": 0},
            ]
            conn.execute(text("update suprimentos_documentos set composicao=:value where id=1"),
                         {"value": json.dumps(composition)})
            conn.execute(text("""
                insert into movements values
                  (10,5,'EMPENHO',1,null,:work,'ATIVA'),
                  (11,7,'BAIXA',2,10,:work,'ATIVA'),
                  (12,9,'EMPENHO',1,null,:work,'ATIVA')
            """), {"work": WORK})

        with self.engine.connect() as conn:
            result = erp_service.work_order_material_cockpit(conn, WORK)

        rows = {row["codigo"]: row for row in result["items"]}
        self.assertEqual({"2001", "2002", "CJ-DIRETO"}, set(rows))
        self.assertNotIn("CJ-100", rows)
        self.assertNotIn("CJ-200", rows)
        self.assertNotIn("CJ-100", rows["2001"]["item"])
        self.assertEqual(2.0, rows["2001"]["necessario"])
        self.assertEqual(3.0, rows["2002"]["necessario"])
        self.assertEqual(2.0, rows["2001"]["empenhado_na_os"])
        self.assertEqual(3.0, rows["2002"]["empenhado_na_os"])
        self.assertEqual(0.0, rows["2001"]["saldo_empenhado"])
        self.assertEqual(3.0, rows["2002"]["saldo_empenhado"])
        self.assertEqual("ATENDIDO", rows["2001"]["status"])
        self.assertEqual("ATENDIDO", rows["2002"]["status"])
        self.assertEqual("ATENDIDO", rows["CJ-DIRETO"]["status"])

    def test_bom_commitment_from_another_work_order_reduces_leaf_free_balance(self):
        with self.engine.begin() as conn:
            conn.execute(text("""
                insert into skus values
                  (5,'CJ-100','Conjunto pai','CJ',1),
                  (7,'2001','Material A','UN',1)
            """))
            conn.execute(text("insert into stock_balances(sku_id,saldo_atual) values(7,10)"))
            conn.execute(text("insert into bom_components values(1,5,7,4)"))
            conn.execute(text("""
                update suprimentos_documentos set composicao=:value where id=1
            """), {"value": json.dumps([
                {"item": "CJ-100", "codigo": "CJ-100", "qtd": 1, "level": 0},
            ])})
            conn.execute(text("insert into movements values(10,5,'EMPENHO',1,null,:work,'ATIVA')"),
                         {"work": OTHER})

        with self.engine.connect() as conn:
            result = erp_service.work_order_material_cockpit(conn, WORK)

        self.assertEqual(1, len(result["items"]))
        row = result["items"][0]
        self.assertEqual("2001", row["codigo"])
        self.assertEqual(4.0, row["saldo_empenhado"])
        self.assertEqual(6.0, row["saldo_disponivel"])
        self.assertEqual("HA_SALDO", row["status"])


if __name__ == "__main__":
    unittest.main()
