"""Transactional tests using a real SQLite ledger; no production database."""
from decimal import Decimal
import json
import re
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event, text

import erp_service
from erp_stock_closure import NegativeStockConfirmationRequired


WORK = uuid4().hex
OTHER = uuid4().hex
SCHEMA = [
    """create table users(id integer primary key,username text,active boolean)""",
    """create table skus(id integer primary key,sku text,descricao text,active boolean)""",
    """create table stock_balances(id integer primary key,sku_id integer unique,saldo_atual numeric(14,3),updated_at timestamp)""",
    """create table bom_components(id integer primary key,item_sku_id integer,component_sku_id integer,quantidade numeric(14,3))""",
    """create table suprimentos_documentos(id integer primary key,tipo text,numero text,erp_work_order_id text,composicao text,status text,updated_at timestamp)""",
    """create table erp_work_orders(id text primary key,numero_os text,status text,technical_status text,
       technical_previous_status text,technical_closed_at timestamp,technical_closed_by text,
       technical_close_reason text,updated_at timestamp,version integer default 1)""",
    """create table movements(id integer primary key,sku_id integer,tipo text,quantidade numeric(14,3),
       saldo_anterior numeric(14,3),saldo_posterior numeric(14,3),usuario_id integer,related_movement_id integer,
       documento text,observacao text,source_type text,source_id text,idempotency_key text unique,
       operation_id text,work_order_id text,context_kind text,setor text,reference_text text,
       link_updated_at timestamp,link_updated_by integer,movement_status text,created_at timestamp not null)""",
    """create table dashboard_movement_cache(id integer primary key,movement_id integer,created_at timestamp,
       cached_at timestamp,usuario_id integer,usuario_nome text,sku_id integer,sku_codigo text,descricao text,
       tipo text,quantidade numeric(14,3),saldo_anterior numeric(14,3),saldo_posterior numeric(14,3),documento text,observacao text)""",
    """create table erp_work_order_status_history(id integer primary key,work_order_id text,status_anterior text,novo_status text,usuario text,observacao text)""",
    """create table erp_audit_events(id integer primary key,entity_type text,entity_id text,action text,
       actor text,origin text,before_data text,after_data text,reason text,created_at timestamp default CURRENT_TIMESTAMP)""",
]


class TechnicalCloseCommitmentTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        @event.listens_for(self.engine, "connect")
        def functions(connection, record):
            connection.create_function("now", 0, lambda: "2026-09-07 12:00:00")
            connection.create_function("jsonb_build_object", -1,
                                       lambda *args: json.dumps(dict(zip(args[::2], args[1::2]))))
        @event.listens_for(self.engine, "before_cursor_execute", retval=True)
        def postgres_sql(connection, cursor, sql, parameters, context, executemany):
            # Only PostgreSQL syntax unsupported by SQLite is adapted. All
            # reads/writes, constraints, commits and rollbacks hit the real DB.
            sql = re.sub(r"for (no key )?update", "", sql, flags=re.I)
            sql = re.sub(r"cast\((\?) as jsonb\)", r"\1", sql, flags=re.I)
            return sql, parameters
        with self.engine.begin() as conn:
            for sql in SCHEMA:
                conn.execute(text(sql))
            conn.execute(text("insert into users values (1,'pcp',1),(2,'almoxarife',1)"))
            conn.execute(text("insert into erp_work_orders(id,numero_os,status,technical_status) values(:id,'3100','ATIVA','ABERTA')"), {"id": WORK})
            for sku_id, sku in [(1, "CJ"), (2, "PP"), (3, "MP"), (4, "OUTRO")]:
                conn.execute(text("insert into skus values(:id,:sku,:sku,1)"), {"id": sku_id, "sku": sku})
                conn.execute(text("insert into stock_balances(sku_id,saldo_atual) values(:id,100)"), {"id": sku_id})
            conn.execute(text("insert into bom_components values(1,1,2,2),(2,2,3,3)"))
            conn.execute(text("insert into suprimentos_documentos values(1,'os','3100',:id,'[]','emitido','2026-09-07')"), {"id": WORK})
        self.recalculate = patch.object(erp_service, "recalculate_work_order_sequences", return_value={})
        self.recalculate.start()
        self.addCleanup(self.recalculate.stop)
        self.addCleanup(self.engine.dispose)

    def sql(self, sql, **params):
        with self.engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return result.mappings().all() if result.returns_rows else None

    def composition(self, **amounts):
        self.sql("update suprimentos_documentos set composicao=:data where id=1",
                 data=json.dumps([{"codigo": k, "qtd": v} for k, v in amounts.items()]))

    def movement(self, amount, *, sku=3, work=WORK, kind="EMPENHO", parent=None, active=True, created="2026-09-01"):
        return self.sql("""insert into movements(sku_id,tipo,quantidade,saldo_anterior,saldo_posterior,usuario_id,
            related_movement_id,work_order_id,movement_status,created_at)
            values(:sku,:kind,:qty,100,100,2,:parent,:work,:status,:created) returning id""",
            sku=sku, kind=kind, qty=amount, parent=parent, work=work,
            status="ATIVA" if active else "CANCELADA", created=created)[0]["id"]

    def close(self, **kwargs):
        with self.engine.begin() as conn:
            return erp_service.technical_close_work_order(conn, WORK, "pcp", actor_user_id=1, **kwargs)

    def automatic(self):
        return self.sql("select * from movements where source_type='TECHNICAL_CLOSE_AUTO_BAIXA' order by id")

    def test_linked_only_remaining_and_ignores_cancelled_other_orders(self):
        parent = self.movement(5)
        self.movement(2, kind="BAIXA", parent=parent)
        self.movement(10, active=False)
        self.movement(10, work=OTHER)
        self.close()
        rows = self.automatic()
        self.assertEqual([(parent, 3)], [(r["related_movement_id"], r["quantidade"]) for r in rows])
        self.assertEqual(1, rows[0]["usuario_id"])
        self.assertEqual(97, self.sql("select saldo_atual from stock_balances where sku_id=3")[0]["saldo_atual"])

    def test_shared_fifo_proportional_preserves_parent_and_other_orders(self):
        self.composition(MP=5)
        older = self.movement(2, work=None)
        newer = self.movement(10, work=None, created="2026-09-02")
        self.movement(50, work=OTHER)
        result = self.close()["auto_baixas"]
        rows = self.automatic()
        self.assertEqual([(older, 2), (newer, 3)], [(r["related_movement_id"], r["quantidade"]) for r in rows])
        self.assertEqual(2, result["candidatos_consumidos"])
        self.assertTrue(all(row["work_order_id"] == WORK for row in rows))
        self.assertIsNone(self.sql("select work_order_id from movements where id=:id", id=newer)[0]["work_order_id"])

    def test_partial_previous_shared_consumption_counts(self):
        self.composition(MP=5)
        shared = self.movement(10, work=None)
        self.movement(2, kind="BAIXA", parent=shared)
        self.movement(6, kind="BAIXA", parent=shared, work=OTHER)
        result = self.close()["auto_baixas"]
        self.assertEqual(2, self.automatic()[0]["quantidade"])
        self.assertEqual("1.000", result["pendencias_encerradas"][0]["quantidade_pendente"])

    def test_kit_covers_children_once_before_loose_child_pool(self):
        self.composition(CJ=1, PP=2, MP=6)
        leaf = self.movement(10, work=None)
        parent = self.movement(3, sku=1, work=None, created="2026-09-02")
        result = self.close()["auto_baixas"]
        rows = self.automatic()
        self.assertEqual(1, len(rows))
        self.assertEqual(leaf, rows[0]["related_movement_id"])
        self.assertEqual(6, rows[0]["quantidade"])
        self.assertEqual(1, result["candidatos_consumidos"])
        self.assertEqual([], result["pendencias_encerradas"])

    def test_linked_kit_covers_bom_and_does_not_consume_shared_pool_again(self):
        self.composition(CJ=1, PP=2, MP=6)
        parent = self.movement(1, sku=1)
        self.movement(20, work=None)
        self.close()
        self.assertEqual([], self.automatic())

    def test_stock_alone_is_not_a_candidate_and_residual_is_audited(self):
        self.composition(MP=6, OUTRO=4)
        result = self.close()
        self.assertEqual([], self.automatic())
        self.assertEqual(2, len(result["auto_baixas"]["pendencias_encerradas"]))
        work = self.sql("select * from erp_work_orders")[0]
        self.assertEqual(("CONCLUIDA", "CONCLUIDA"), (work["status"], work["technical_status"]))
        self.assertEqual("concluido", self.sql("select status from suprimentos_documentos")[0]["status"])
        audit = json.loads(self.sql("select after_data from erp_audit_events")[0]["after_data"])
        self.assertEqual(result["auto_baixas"], audit["auto_baixas"])

    def test_negative_requires_exact_confirmation_and_no_first_write(self):
        self.composition(MP=4)
        self.movement(4, work=None)
        self.sql("update stock_balances set saldo_atual=1 where sku_id=3")
        with self.assertRaises(NegativeStockConfirmationRequired) as error:
            self.close()
        self.assertEqual([], self.automatic())
        self.assertEqual("ATIVA", self.sql("select status from erp_work_orders")[0]["status"])
        self.assertEqual("emitido", self.sql("select status from suprimentos_documentos")[0]["status"])
        self.assertEqual([], self.sql("select * from erp_audit_events"))
        with self.assertRaises(NegativeStockConfirmationRequired):
            self.close(confirm_negative_stock="true", confirmation_token=error.exception.payload["confirmation_token"])
        result = self.close(confirm_negative_stock=True, confirmation_token=error.exception.payload["confirmation_token"])
        self.assertEqual(-3, self.sql("select saldo_atual from stock_balances where sku_id=3")[0]["saldo_atual"])
        self.assertTrue(result["auto_baixas"]["saldo_negativo_confirmado"])

    def test_confirmation_rechecks_changed_balance(self):
        self.movement(5)
        self.sql("update stock_balances set saldo_atual=1 where sku_id=3")
        with self.assertRaises(NegativeStockConfirmationRequired) as first:
            self.close()
        self.sql("update stock_balances set saldo_atual=0 where sku_id=3")
        with self.assertRaises(NegativeStockConfirmationRequired) as second:
            self.close(confirm_negative_stock=True, confirmation_token=first.exception.payload["confirmation_token"])
        self.assertNotEqual(first.exception.payload["confirmation_token"], second.exception.payload["confirmation_token"])
        self.assertEqual([], self.automatic())

    def test_repeat_after_success_cannot_consume_new_pool(self):
        self.composition(MP=5)
        self.movement(2, work=None)
        self.close()
        before = self.automatic()
        self.movement(10, work=None)
        result = self.close()
        self.assertTrue(result["replayed"])
        self.assertEqual(before, self.automatic())
        self.assertEqual(1, len(self.sql("select * from erp_work_order_status_history")))

    def test_failure_after_material_writes_rolls_everything_back(self):
        self.movement(5)
        with patch.object(erp_service, "recalculate_work_order_sequences", side_effect=RuntimeError("fault")):
            with self.assertRaisesRegex(RuntimeError, "fault"):
                self.close()
        self.assertEqual([], self.automatic())
        self.assertEqual(100, self.sql("select saldo_atual from stock_balances where sku_id=3")[0]["saldo_atual"])
        self.assertEqual("ATIVA", self.sql("select status from erp_work_orders")[0]["status"])
        self.assertEqual("emitido", self.sql("select status from suprimentos_documentos")[0]["status"])

    def test_fractional_shared_kit_never_exceeds_demand(self):
        self.composition(MP=1)
        self.movement(1, sku=1, work=None)
        result = self.close()["auto_baixas"]
        self.assertEqual([], self.automatic())
        self.assertEqual(Decimal("1"), Decimal(result["pendencias_encerradas"][0]["quantidade_pendente"]))

    def test_a_kit_cannot_reconsume_an_already_covered_child(self):
        self.composition(PP=2, MP=6)
        self.movement(6)
        self.movement(1, sku=1, work=None)
        result = self.close()["auto_baixas"]
        self.assertEqual(0, result["candidatos_consumidos"])

    def test_document_linked_to_another_revision_is_not_used(self):
        self.composition(MP=20)
        self.sql("update suprimentos_documentos set erp_work_order_id=:other", other=OTHER)
        self.movement(10, work=None)
        result = self.close()["auto_baixas"]
        self.assertEqual(0, result["candidatos_consumidos"])
        self.assertIsNone(result["document_id"])

    def test_invalid_composition_aborts_instead_of_silently_waiving(self):
        self.sql("update suprimentos_documentos set composicao='oops'")
        with self.assertRaisesRegex(ValueError, "Composição"):
            self.close()
        self.assertEqual("ATIVA", self.sql("select status from erp_work_orders")[0]["status"])

    def test_cancelled_order_cannot_consume(self):
        self.movement(5)
        self.sql("update erp_work_orders set status='CANCELADA'")
        with self.assertRaisesRegex(ValueError, "cancelada"):
            self.close()
        self.assertEqual([], self.automatic())

    def test_entry_backflush_is_never_repeated_at_closure(self):
        self.composition(CJ=1, PP=2, MP=6)
        entry = self.movement(1, sku=1, kind="ENTRADA", work=None)
        self.sql("update movements set source_type='MANUAL_ENTRY_BACKFLUSH' where id=:id", id=entry)
        for sku_id, amount in [(2, 2), (3, 6)]:
            debit = self.movement(amount, sku=sku_id, kind="BAIXA", work=None)
            self.sql("update movements set source_type='BACKFLUSH_CONSUMPTION' where id=:id", id=debit)
            self.sql("update stock_balances set saldo_atual=saldo_atual-:qty where sku_id=:id", qty=amount, id=sku_id)
        parent = self.movement(1, sku=1)
        children_before = self.sql("select * from stock_balances where sku_id in (2,3) order by sku_id")
        history_before = self.sql("select * from movements where source_type='BACKFLUSH_CONSUMPTION' order by id")
        self.close()
        self.assertEqual([], self.automatic())
        self.assertEqual(children_before, self.sql("select * from stock_balances where sku_id in (2,3) order by sku_id"))
        self.assertEqual(history_before, self.sql("select * from movements where source_type='BACKFLUSH_CONSUMPTION' order by id"))

    def test_previous_direct_debits_cover_need_before_shared_pool(self):
        self.composition(MP=10)
        prior = self.movement(4, kind="BAIXA", created="2026-08-20")
        self.movement(50, kind="BAIXA", active=False)
        parent = self.movement(20, work=None)
        self.close()
        self.assertEqual([(parent, 6)], [(r["related_movement_id"], r["quantidade"]) for r in self.automatic()])
        self.assertEqual(4, self.sql("select quantidade from movements where id=:id", id=prior)[0]["quantidade"])

    def test_previously_debited_kit_covers_kit_pp_and_mp(self):
        self.composition(CJ=1, PP=2, MP=6)
        self.movement(1, sku=1, kind="BAIXA")
        self.movement(10, sku=1, work=None)
        self.movement(20, sku=2, work=None)
        self.movement(60, sku=3, work=None)
        result = self.close()["auto_baixas"]
        self.assertEqual([], self.automatic())
        self.assertEqual([], result["pendencias_encerradas"])

    def test_linked_commitment_fully_consumed_over_several_days_has_no_new_debit(self):
        self.composition(MP=5)
        parent = self.movement(5)
        self.movement(2, kind="BAIXA", parent=parent, created="2026-08-20")
        self.movement(3, kind="BAIXA", parent=parent, created="2026-09-01")
        self.movement(10, work=None)
        self.close()
        self.assertEqual([], self.automatic())

    def test_ambiguous_direct_debit_and_open_commitment_blocks_without_rewriting_history(self):
        self.composition(MP=5)
        self.movement(5)
        self.movement(2, kind="BAIXA")
        before = self.sql("select * from movements order by id")
        with self.assertRaisesRegex(ValueError, "Há baixa direta.*mesmo SKU"):
            self.close()
        self.assertEqual(before, self.sql("select * from movements order by id"))
        self.assertEqual("ATIVA", self.sql("select status from erp_work_orders")[0]["status"])

    def test_reserved_internal_op_inputs_are_not_shared_pool(self):
        self.composition(MP=5)
        protected = self.movement(10, work=None)
        self.sql("update movements set source_type='PRODUCTION_ORDER' where id=:id", id=protected)
        result = self.close()["auto_baixas"]
        self.assertEqual([], self.automatic())
        self.assertEqual(Decimal(5), Decimal(result["pendencias_encerradas"][0]["quantidade_pendente"]))

    def test_reopen_preserves_baixas_and_reclose_does_not_repeat_them(self):
        self.composition(MP=5)
        self.movement(5)
        self.close()
        before = self.sql("select * from movements order by id")
        with self.engine.begin() as conn:
            erp_service.technical_reopen_work_order(conn, WORK, "pcp", "ajuste")
        self.assertEqual("emitido", self.sql("select status from suprimentos_documentos")[0]["status"])
        self.assertEqual(before, self.sql("select * from movements order by id"))
        self.close()
        self.assertEqual(before, self.sql("select * from movements order by id"))


if __name__ == "__main__":
    unittest.main()
