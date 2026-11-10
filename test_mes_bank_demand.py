"""Bank demand must survive kit expansion and reserve the same SKU across WIP."""
import json
import unittest

from sqlalchemy import text

import erp_service
from test_mes_material_reservations import MaterialReservationTests, WORK


class BankDemandTests(unittest.TestCase):
    add_order = MaterialReservationTests.add_order
    movement = MaterialReservationTests.movement
    cockpit = MaterialReservationTests.cockpit

    def setUp(self):
        MaterialReservationTests.setUp(self)
        with self.engine.begin() as conn:
            conn.execute(text('alter table skus add column grupo text'))
            conn.execute(text('alter table skus add column categoria text'))
            conn.execute(text('alter table movements add column source_type text'))
            conn.execute(text("""
                insert into skus(id,sku,descricao,unidade,active,grupo,categoria) values
                (20,'30200369','CJ BANCOS REC - MC - LB - 3,2,3','cj',1,'30 - CONJUNTO / KIT','20 - BANCOS'),
                (21,'10200064','BCO MC LB RECLINAVEL 3 L NORMAL','pc',1,'10 - INSUMO','20 - BANCOS'),
                (22,'10200062','BCO MC LB RECLINAVEL 3 L NORMAL LD','pc',1,'10 - INSUMO','20 - BANCOS'),
                (23,'10220001','APOIO BRACO BCO','pc',1,'10 - INSUMO','22 - PECAS BCO')
            """))
            conn.execute(text('insert into bom_components values(20,20,21,1),(21,20,22,1),(22,21,23,2)'))
            conn.execute(text('insert into stock_balances(sku_id,saldo_atual) values(21,1),(22,1),(23,100)'))
        self.set_composition([
            # Suprimentos emits the kit's children with level zero; the kit
            # itself is only in `itens`, not in the material snapshot.
            {'item': '30200369', 'codigo': '10200064', 'qtd': 1, 'level': 0},
            {'item': '10200064', 'codigo': '10220001', 'qtd': 2, 'level': 1},
            {'item': '30200369', 'codigo': '10200062', 'qtd': 1, 'level': 0},
        ])

    def set_composition(self, composition):
        with self.engine.begin() as conn:
            conn.execute(text('update suprimentos_documentos set composicao=:value where id=1'),
                         {'value': json.dumps(composition)})

    def banks(self):
        return {row['codigo']: row for row in self.cockpit()['items']}

    def test_expanded_kit_keeps_individual_banks_in_shared_bank_category(self):
        rows = self.banks()
        self.assertEqual({'10200064', '10200062'}, set(rows))
        self.assertEqual(1, rows['10200064']['necessario'])
        self.assertEqual(1, rows['10200064']['saldo_atual'])

    def test_bank_stock_is_reserved_by_prior_os_with_same_expanded_snapshot(self):
        self.add_order(1, composition=[{'item': '30200369', 'codigo': '10200064', 'qtd': 1}])
        row = self.banks()['10200064']
        self.assertEqual(1, row['reserva'])
        self.assertEqual(0, row['saldo_disponivel_sequencia'])
        self.assertEqual(1, row['faltante'])

    def test_prior_bank_internal_components_do_not_reserve_independent_material(self):
        self.set_composition([{'codigo': '10220001', 'qtd': 100}])
        self.add_order(1, composition=[
            {'codigo': '10200064', 'qtd': 1},
            {'item': '10200064', 'codigo': '10220001', 'qtd': 2},
        ])
        row = self.banks()['10220001']
        self.assertEqual(0, row['reserva'])
        self.assertEqual(0, row['faltante'])

    def test_depth_does_not_remove_unrelated_material_after_bank(self):
        self.set_composition([
            {'codigo': '10200064', 'qtd': 1, 'level': 0},
            {'item': 'OUTRO-KIT', 'codigo': '1004', 'qtd': 2, 'level': 1},
        ])
        self.assertEqual({'10200064', '1004'}, set(self.banks()))

    def test_own_bank_commitment_and_linked_baixa_cover_only_own_os(self):
        prior = self.add_order(1, composition=[{'codigo': '10200064', 'qtd': 1}])
        parent = self.movement(prior, 1, sku_id=21)
        self.movement(prior, 1, kind='BAIXA', related=parent, sku_id=21)
        with self.engine.begin() as conn:
            conn.execute(text('update stock_balances set saldo_atual=0 where sku_id=21'))
        row = self.banks()['10200064']
        self.assertEqual(0, row['saldo_empenhado'])
        self.assertEqual(0, row['atendido_na_os_total'])
        self.assertEqual(0, row['reserva'])
        self.assertEqual(1, row['faltante'])
        parent = self.movement(WORK, 1, sku_id=21)
        self.movement(WORK, 1, kind='BAIXA', related=parent, sku_id=21)
        self.assertEqual(0, self.banks()['10200064']['faltante'])
        self.assertEqual(1, self.banks()['10200064']['atendido_na_os_total'])

    def test_other_op_commitment_blocks_bank_stock_without_fulfilling_os(self):
        movement = self.movement(None, 1, sku_id=21)
        with self.engine.begin() as conn:
            conn.execute(text("update movements set source_type='PRODUCTION_ORDER' where id=:id"), {'id': movement})
        row = self.banks()['10200064']
        self.assertEqual(1, row['saldo_empenhado'])
        self.assertEqual(0, row['atendido_na_os_total'])
        self.assertEqual(1, row['faltante'])

    def test_bank_transit_never_reduces_current_shortage(self):
        self.movement(None, 1, sku_id=21)
        with self.engine.begin() as conn:
            conn.execute(text("update erp_purchase_order_lines set sku_id=21,sku_codigo='10200064',quantidade_pedida=5 where id=1"))
        row = self.banks()['10200064']
        self.assertEqual(5, row['em_chegada'])
        self.assertEqual(1, row['faltante'])

    def test_kit_parent_expands_and_its_own_commitment_covers_each_bank_once(self):
        self.set_composition([
            {'codigo': '30200369', 'qtd': 1},
            {'item': '30200369', 'codigo': '10200064', 'qtd': 1},
            {'item': '30200369', 'codigo': '10200062', 'qtd': 1},
        ])
        self.movement(WORK, 1, sku_id=20)
        rows = self.banks()
        self.assertEqual({'10200064', '10200062'}, set(rows))
        for row in rows.values():
            self.assertEqual(1, row['necessario'])
            self.assertEqual(1, row['atendido_na_os_total'])
            self.assertEqual(0, row['faltante'])

    def test_shortage_report_includes_missing_bank_from_expanded_kit(self):
        self.movement(None, 1, sku_id=21)
        with self.engine.begin() as conn:
            for sql in (
                'alter table erp_vehicle_entries add column vehicle_id text',
                'alter table erp_vehicle_entries add column cliente_nome text',
                'alter table erp_work_orders add column proposta_numero text',
                'alter table erp_work_orders add column transformacao text',
                'create table erp_vehicles(id text,chassi text,marca text,modelo text,versao text)',
                "update erp_vehicle_entries set vehicle_id='v1' where id='entry-1'",
                "insert into erp_vehicles values('v1','TEST-CHASSI','FORD','TRANSIT','L3H2')",
            ):
                conn.execute(text(sql))
        with self.engine.connect() as conn:
            report = erp_service.material_shortage_report(conn)
        self.assertEqual(1, report['shortage_lines'])
        self.assertEqual('10200064', report['rows'][0]['codigo'])
        self.assertEqual(1, report['rows'][0]['faltante'])
