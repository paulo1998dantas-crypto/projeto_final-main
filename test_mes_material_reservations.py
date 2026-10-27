"""Read-only, sequence-wide allocation of free stock to remaining O.S. demand."""
import json
import unittest
from uuid import uuid4

from sqlalchemy import event, text

import erp_service
import test_mes_material_cockpit as fixtures

WORK = fixtures.WORK


class MaterialReservationTests(unittest.TestCase):
    def setUp(self):
        fixtures.MaterialCockpitTests.setUp(self)
        with self.engine.begin() as conn:
            for sql in (
                "alter table erp_work_orders add column status text default 'ATIVA'",
                "alter table erp_work_orders add column technical_status text default 'ABERTA'",
                "alter table erp_work_orders add column is_current boolean default true",
                "alter table erp_work_orders add column data_comercial_prevista text",
                "create table erp_vehicle_entries(id text primary key,item_number integer)",
                "create table erp_work_order_sequences(work_order_id text primary key,sequencia integer,ativo boolean)",
                "insert into erp_vehicle_entries values('entry-1',1)",
                "delete from movements",
                "update stock_balances set saldo_atual=5 where sku_id=1",
            ):
                conn.execute(text(sql))
            conn.execute(text("insert into erp_work_order_sequences values(:work,6,true)"), {"work": WORK})
            conn.execute(text("update suprimentos_documentos set composicao=:value where id=1"),
                         {"value": json.dumps([{"codigo": "1001", "qtd": 1}])})
        self.order_index = 0

    def add_order(self, rank, amount=1, *, composition=None, status='ATIVA', technical='ABERTA', current=True):
        self.order_index += 1
        key = str(uuid4())
        entry = f'prior-entry-{self.order_index}'
        number = f'PRIOR-{self.order_index}'
        with self.engine.begin() as conn:
            conn.execute(text("insert into erp_vehicle_entries values(:entry,:item)"),
                         {"entry": entry, "item": 1000 + self.order_index})
            conn.execute(text("""
                insert into erp_work_orders(id,numero_os,vehicle_entry_id,status,technical_status,is_current)
                values(:id,:number,:entry,:status,:technical,:current)
            """), {"id": key, "number": number, "entry": entry, "status": status,
                    "technical": technical, "current": current})
            conn.execute(text("insert into erp_work_order_sequences values(:id,:rank,true)"),
                         {"id": key, "rank": rank})
            conn.execute(text("""
                insert into suprimentos_documentos(tipo,numero,erp_work_order_id,composicao,status,updated_at)
                values('os',:number,:id,:composition,'emitido','2026-10-20')
            """), {"number": number, "id": key, "composition": json.dumps(
                composition if composition is not None else [{"codigo": "1001", "qtd": amount}])})
        return key

    def movement(self, work, amount, kind='EMPENHO', *, related=None, sku_id=1, status='ATIVA'):
        with self.engine.begin() as conn:
            conn.execute(text("""
                insert into movements(sku_id,tipo,quantidade,related_movement_id,work_order_id,movement_status)
                values(:sku,:kind,:amount,:related,:work,:status)
            """), {"sku": sku_id, "kind": kind, "amount": amount, "related": related,
                    "work": work, "status": status})
            return conn.execute(text("select max(id) from movements")).scalar()

    def cockpit(self, work=WORK):
        with self.engine.connect() as conn:
            return erp_service.work_order_material_cockpit(conn, work)

    def item(self, work=WORK):
        return next(row for row in self.cockpit(work)['items'] if row['codigo'] == '1001')

    def test_five_units_one_committed_sixth_card_is_short(self):
        prior = [self.add_order(i) for i in range(1, 6)]
        later = [self.add_order(i) for i in range(7, 31)]
        self.movement(prior[0], 1)
        result = self.cockpit()
        row = result['items'][0]
        self.assertEqual(4, row['saldo_disponivel'])
        self.assertEqual(4, row['reserva'])
        self.assertEqual(0, row['saldo_disponivel_sequencia'])
        self.assertEqual(1, row['faltante'])
        self.assertEqual('SEM_PREVISAO', row['status'])
        self.assertEqual(6, result['reserva_sequencia']['posicao'])
        self.assertEqual('ATENDIDO', self.item(prior[0])['status'])
        self.assertEqual(0, self.item(prior[0])['reserva'])
        fifth = self.item(prior[4])
        self.assertEqual(3, fifth['reserva'])
        self.assertEqual(1, fifth['saldo_disponivel_sequencia'])
        self.assertEqual(0, fifth['faltante'])
        self.assertEqual('HA_SALDO', fifth['status'])
        self.assertEqual(1, self.item(later[-1])['faltante'])

    def test_reservation_does_not_exceed_free_stock(self):
        self.add_order(1, amount=10)
        row = self.item()
        self.assertEqual(10, row['demanda_anterior'])
        self.assertEqual(5, row['reserva'])
        self.assertEqual(1, row['faltante'])

    def test_bank_equivalence_recalculates_grouped_stock_and_prior_reservation(self):
        source = '10200170'
        target = '10200085'
        source_description = 'BCO MC LB FIXO 4 L PME S/ AP BRACO 100 MM 3P TECIDO'
        target_description = 'BCO MC LB FIXO 4 L NORMAL S/ AP BRACO 370 MM 3P TECIDO'
        with self.engine.begin() as conn:
            conn.execute(text('alter table skus add column grupo text'))
            conn.execute(text('alter table skus add column categoria text'))
            conn.execute(text("""
                insert into skus(id,sku,descricao,unidade,active,grupo,categoria) values
                  (10,:source,:source_description,'CJ',1,'BCO','20 - BANCOS'),
                  (11,:target,:target_description,'CJ',1,'BCO','20 - BANCOS')
            """), {'source': source, 'target': target,
                   'source_description': source_description, 'target_description': target_description})
            conn.execute(text('insert into stock_balances(sku_id,saldo_atual) values(10,2),(11,3)'))
            conn.execute(text('update suprimentos_documentos set composicao=:value where id=1'),
                         {'value': json.dumps([{'codigo': source, 'qtd': 1}])})
        prior = self.add_order(5, composition=[{'codigo': source, 'qtd': 1}])
        self.movement(None, 1, sku_id=11)

        with self.engine.connect() as conn:
            equivalences, outcomes = erp_service._material_cockpit_bank_equivalences(conn)
            result = erp_service.work_order_material_cockpit(
                conn, WORK, sku_equivalences=equivalences,
            )

        self.assertEqual(target, equivalences[source])
        self.assertEqual('Equivalente cadastrado', outcomes[source]['resultado'])
        row = next(item for item in result['items'] if item['codigo'] == target)
        self.assertEqual(5, row['saldo_atual'])
        self.assertEqual(1, row['saldo_empenhado'])
        self.assertEqual(1, row['reserva'])
        self.assertEqual(3, row['saldo_disponivel_sequencia'])
        self.assertEqual(0, row['faltante'])

    def test_bank_equivalence_rules_keep_the_requested_specificity(self):
        descriptions = {
            '10201001': 'BCO JL BJD 2 L S/ AP BRACO 100 MM NORMAL',
            '10201002': 'BCO JL NORMAL 2 L S/ AP BRACO 100 MM NORMAL',
            '10201003': 'BCO MC 1 L C/ TRILHO LD COURVIN',
            '10201004': 'BCO MC 1 L S/ TRILHO LD COURVIN',
            '10201005': 'BCO MC 3 L C/ TRILHO LD COURVIN',
            '10201006': 'BCO MC 3 L NORMAL LD COURVIN',
        }
        with self.engine.begin() as conn:
            conn.execute(text('alter table skus add column grupo text'))
            conn.execute(text('alter table skus add column categoria text'))
            conn.execute(text("""
                insert into skus(id,sku,descricao,unidade,active,grupo,categoria)
                values(:id,:sku,:descricao,'CJ',1,'BCO','20 - BANCOS')
            """), [
                {'id': 20+i, 'sku': sku, 'descricao': description}
                for i, (sku, description) in enumerate(descriptions.items())
            ])

        with self.engine.connect() as conn:
            equivalents, _ = erp_service._material_cockpit_bank_equivalences(conn)

        self.assertEqual('10201002', equivalents['10201001'])
        self.assertEqual('10201004', equivalents['10201003'])
        self.assertEqual('10201006', equivalents['10201005'])

    def test_native_shortage_report_returns_live_chassis_rows(self):
        with self.engine.begin() as conn:
            conn.execute(text('alter table erp_vehicle_entries add column vehicle_id text'))
            conn.execute(text('alter table erp_vehicle_entries add column cliente_nome text'))
            conn.execute(text('alter table erp_work_orders add column proposta_numero text'))
            conn.execute(text('alter table erp_work_orders add column transformacao text'))
            conn.execute(text('create table erp_vehicles(id text primary key,chassi text,marca text,modelo text,versao text)'))
            conn.execute(text("update erp_vehicle_entries set vehicle_id='vehicle-1',cliente_nome='CLIENTE',item_number=45 where id='entry-1'"))
            conn.execute(text("insert into erp_vehicles values('vehicle-1','9TESTCHASSI000001','MARCA','MODELO','VERSAO')"))
            conn.execute(text("update erp_work_orders set proposta_numero='P-45',transformacao='TRANSFORMACAO',data_comercial_prevista='2026-11-05' where id=:id"), {'id': WORK})
            conn.execute(text('update stock_balances set saldo_atual=0 where sku_id=1'))

        with self.engine.connect() as conn:
            result = erp_service.material_shortage_report(conn)

        self.assertEqual(1, result['wip_count'])
        self.assertEqual(1, result['chassi_count'])
        self.assertEqual(1, result['shortage_lines'])
        self.assertEqual('9TESTCHASSI000001', result['rows'][0]['chassi'])
        self.assertEqual('4100', result['rows'][0]['numero_os'])
        self.assertEqual('2026-11-05', result['rows'][0]['data_entrega'])

    def test_partial_own_commitment_only_reserves_residual(self):
        prior = self.add_order(1, amount=3)
        self.movement(prior, 1)
        row = self.item()
        self.assertEqual(4, row['saldo_disponivel'])
        self.assertEqual(2, row['demanda_anterior'])
        self.assertEqual(2, row['reserva'])
        self.assertEqual(2, row['saldo_disponivel_sequencia'])

    def test_later_committed_order_is_protected_even_before_its_turn(self):
        later = self.add_order(20)
        self.movement(later, 4)
        self.add_order(1)
        row = self.item()
        self.assertEqual(1, row['reserva'])
        self.assertEqual(0, row['saldo_disponivel_sequencia'])
        self.assertEqual(1, row['faltante'])

    def test_reordering_changes_reservation_without_stock_changes(self):
        self.add_order(1, amount=5)
        self.assertEqual(5, self.item()['reserva'])
        with self.engine.begin() as conn:
            conn.execute(text('update erp_work_order_sequences set sequencia=0 where work_order_id=:id'), {'id': WORK})
        self.assertEqual(0, self.item()['reserva'])
        self.assertEqual(5, self.item()['saldo_disponivel_sequencia'])

    def test_without_rank_uses_same_delivery_then_item_fallback_as_cards(self):
        prior = self.add_order(1, amount=2)
        with self.engine.begin() as conn:
            conn.execute(text('update erp_work_order_sequences set sequencia=null'))
            conn.execute(text("update erp_work_orders set data_comercial_prevista='2026-10-25'"))
            conn.execute(text("update erp_work_orders set data_comercial_prevista='2026-10-20' where id=:id"), {'id': prior})
        self.assertEqual(2, self.item()['reserva'])

    def test_card_outside_wip_does_not_borrow_the_queue(self):
        self.add_order(1, amount=5)
        with self.engine.begin() as conn:
            conn.execute(text("update erp_work_orders set status='FINALIZADA' where id=:id"), {'id': WORK})
        result = self.cockpit()
        self.assertFalse(result['reserva_sequencia']['aplicavel'])
        self.assertEqual(0, result['items'][0]['reserva'])

    def test_closed_noncurrent_and_technically_closed_orders_do_not_reserve(self):
        for state in ('FINALIZADA', 'ENTREGUE', 'CANCELADA', 'RETIRADA', 'ARQUIVADA'):
            self.add_order(1, amount=100, status=state)
        self.add_order(2, amount=100, technical='CONCLUIDA')
        self.add_order(3, amount=100, current=False)
        self.add_order(4, amount=2)
        self.assertEqual(2, self.item()['reserva'])

    def test_later_demand_does_not_reserve_for_current_card(self):
        self.add_order(10, amount=50)
        self.assertEqual(0, self.item()['reserva'])

    def test_bom_parent_and_rendered_children_are_not_counted_twice(self):
        with self.engine.begin() as conn:
            conn.execute(text("insert into skus values(5,'CJ-100','Conjunto','CJ',1),(6,'PP-100','Subconjunto','CJ',1)"))
            conn.execute(text('insert into bom_components values(1,5,6,2),(2,6,1,1.5)'))
        prior = self.add_order(1, composition=[
            {"codigo": "CJ-100", "qtd": 1}, {"codigo": "PP-100", "qtd": 2},
            {"codigo": "1001", "qtd": 3},
        ])
        self.assertEqual(3, self.item()['reserva'])
        self.movement(prior, 0.5, sku_id=5)
        row = self.item()
        self.assertEqual(1.5, row['demanda_anterior'])
        self.assertEqual(1.5, row['reserva'])
        self.assertEqual(3.5, row['saldo_disponivel'])

    def test_direct_baixa_covers_prior_demand_and_current_card(self):
        prior = self.add_order(1)
        self.movement(prior, 1, 'BAIXA')
        self.assertEqual(0, self.item()['reserva'])
        self.movement(WORK, 1, 'BAIXA')
        row = self.item()
        self.assertEqual(1, row['baixado_direto_na_os'])
        self.assertEqual('ATENDIDO', row['status'])
        self.assertEqual(0, row['faltante'])

    def test_linked_baixa_is_not_added_to_gross_commitment_coverage(self):
        prior = self.add_order(1, amount=2)
        commitment = self.movement(prior, 1)
        self.movement(prior, 1, 'BAIXA', related=commitment)
        row = self.item()
        self.assertEqual(0, row['saldo_empenhado'])
        self.assertEqual(1, row['reserva'])
        self.assertEqual(1, row['demanda_anterior'])

    def test_negative_global_stock_does_not_create_extra_demand_on_protected_card(self):
        self.movement(WORK, 1)
        with self.engine.begin() as conn:
            conn.execute(text('update stock_balances set saldo_atual=0 where sku_id=1'))
        row = self.item()
        self.assertEqual(-1, row['saldo_disponivel'])
        self.assertEqual(0, row['saldo_disponivel_sequencia'])
        self.assertEqual(0, row['faltante'])
        self.assertEqual('ATENDIDO', row['status'])

    def test_appropriated_baixa_of_shared_commitment_covers_only_its_os(self):
        prior = self.add_order(1)
        shared = self.movement(None, 2)
        self.movement(prior, 1, 'BAIXA', related=shared)
        row = self.item()
        self.assertEqual(1, row['saldo_empenhado'])
        self.assertEqual(0, row['reserva'])

    def test_cancelled_movements_do_not_cover_or_reduce_stock(self):
        prior = self.add_order(1, amount=2)
        self.movement(prior, 1, status='CANCELADA')
        self.movement(prior, 1, 'BAIXA', status='CANCELADA')
        row = self.item()
        self.assertEqual(0, row['saldo_empenhado'])
        self.assertEqual(2, row['reserva'])

    def test_unknown_preceding_composition_does_not_claim_availability(self):
        prior = self.add_order(1)
        with self.engine.begin() as conn:
            conn.execute(text('delete from suprimentos_documentos where erp_work_order_id=:id'), {'id': prior})
        result = self.cockpit()
        self.assertFalse(result['reserva_sequencia']['completa'])
        self.assertEqual(1, len(result['reserva_sequencia']['ordens_sem_composicao']))
        self.assertEqual('RESERVA_INCOMPLETA', result['items'][0]['status'])
        self.movement(WORK, 1)
        self.assertEqual('ATENDIDO', self.item()['status'])

    def test_invalid_or_empty_preceding_composition_is_reported(self):
        prior = self.add_order(1)
        for value in ('not json', '[]', '{}', '[{}]', '[{"codigo":"1001","qtd":0}]'):
            with self.subTest(value=value), self.engine.begin() as conn:
                conn.execute(text('update suprimentos_documentos set composicao=:value where erp_work_order_id=:id'),
                             {'value': value, 'id': prior})
            self.assertEqual('RESERVA_INCOMPLETA', self.item()['status'])

    def test_newest_direct_document_wins_over_legacy_document(self):
        prior = self.add_order(1)
        with self.engine.begin() as conn:
            number = conn.execute(text('select numero_os from erp_work_orders where id=:id'), {'id': prior}).scalar()
            conn.execute(text("""
                insert into suprimentos_documentos(tipo,numero,erp_work_order_id,composicao,status,updated_at)
                values('os',:number,:work,:value,'emitido',:date)
            """), [
                {'number': number, 'work': prior, 'value': '[{"codigo":"1001","qtd":2}]', 'date': '2026-10-21'},
                {'number': number, 'work': None, 'value': '[{"codigo":"1001","qtd":100}]', 'date': '2026-10-22'},
            ])
        self.assertEqual(2, self.item()['reserva'])

    def test_partial_available_stock_is_not_labelled_as_fully_available(self):
        self.add_order(1, amount=4.5)
        row = self.item()
        self.assertEqual(0.5, row['saldo_disponivel_sequencia'])
        self.assertEqual(0.5, row['faltante'])
        self.assertEqual('SALDO_PARCIAL', row['status'])

    def test_incoming_does_not_become_free_stock(self):
        self.add_order(1, amount=5)
        with self.engine.begin() as conn:
            conn.execute(text("update erp_purchase_order_lines set sku_id=1,sku_codigo='1001' where id=1"))
        row = self.item()
        self.assertEqual(5, row['em_chegada'])
        self.assertEqual(0, row['saldo_disponivel_sequencia'])
        self.assertEqual(1, row['faltante'])

    def test_read_only_repeatable_and_prior_documents_are_loaded_once(self):
        for i in range(1, 5):
            self.add_order(i)
        statements = []
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(self.engine, 'before_cursor_execute', record)
        first = self.cockpit()
        self.assertEqual(1, sum('document_rank' in sql for sql in statements))
        self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements))
        second = self.cockpit()
        self.assertEqual(first, second)
        event.remove(self.engine, 'before_cursor_execute', record)


if __name__ == '__main__':
    unittest.main()
