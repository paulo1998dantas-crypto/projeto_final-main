"""Real interval/counter/event writes for multi-operator production cycles.

SQLite fixture bridges only PostgreSQL locks/date arithmetic; the production
command and all interval/counter writes execute unmocked, with both stage kinds.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import re
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import create_engine, text
from jinja2 import Environment, FileSystemLoader

import erp_service


class ExecutionCycleTests(TestCase):
    def test_setup_pause_resume_and_finish_accumulate_all_operators_exactly_once(self):
        for kind in ('work', 'entry'):
            with self.subTest(kind=kind):
                engine = create_engine('sqlite://')
                with engine.begin() as sql:
                    raw = sql.connection.driver_connection
                    raw.create_function('now', 0, lambda: '2026-10-05T12:00:00+00:00')
                    raw.create_function('new_id', 0, lambda: str(uuid4()))
                    raw.create_function('greatest', -1, lambda *values: max(values))
                    raw.create_function('seconds_between', 2, lambda end, start: int((datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds()))
                    stage_table = 'erp_work_order_stages' if kind == 'work' else 'erp_vehicle_entry_stages'
                    event_table = 'erp_work_order_stage_events' if kind == 'work' else 'erp_vehicle_entry_stage_events'
                    fk = erp_service._pause_stage_column(kind)
                    sql.execute(text(f'''create table {stage_table}(id text primary key,status text,parametrizado boolean,
                        aplicavel boolean,inicio text,termino text,responsavel text,observacoes text,localizacao text,bloqueio_motivo text,
                        setup_time_hours numeric,production_time_hours numeric,total_stopped_time_hours numeric,updated_at text)'''))
                    sql.execute(text(f'''insert into {stage_table}(id,status,parametrizado,aplicavel,observacoes)
                        values('stage','PENDENTE',true,true,'OBSERVAÇÃO ORIGINAL')'''))
                    sql.execute(text(f'''create table {event_table}(id text default (new_id()),{fk} text,action text,status_anterior text,novo_status text,
                        operador text,observacao text,setup_time_hours numeric,production_time_hours numeric,
                        total_stopped_time_hours numeric,idempotency_key text unique,inicio text,termino text,localizacao text)'''))
                    sql.execute(text(f'''create table erp_stage_auto_time_counters(id text default 'counter',{fk} text unique,
                        production_seconds integer default 0,setup_seconds integer default 0,stopped_seconds integer default 0,updated_at text)'''))
                    for table, seconds, extra in (
                        ('erp_stage_time_sessions','productive_seconds','observation text,'),
                        ('erp_stage_setup_sessions','duration_seconds',''),
                        ('erp_stage_time_pauses','duration_seconds','pause_type text,reason text,resume_phase text,'),
                    ):
                        sql.execute(text(f'''create table {table}(id text default (new_id()),{fk} text,started_at text,
                            ended_at text,started_by text,ended_by text,{seconds} integer,updated_at text,
                            execution_operator text,auto_time_fields boolean default false,superseded_at text,
                            {extra} idempotency_key text unique)'''))

                    class Connection:
                        def execute(self, statement, params=None):
                            query = str(statement).replace('for update', '')
                            query = query.replace('cast(:finish as timestamptz)', ':finish')
                            query = query.replace('cast(:ended as timestamptz) < cast(:started as timestamptz)',
                                                  'julianday(:ended) < julianday(:started)')
                            query = re.sub(r'greatest\(\s*0,\s*floor\(extract\(epoch from\s*\(cast\(:ended as timestamptz\)-started_at\)\)\)::bigint\s*\)',
                                           'max(0,seconds_between(:ended,started_at))', query)
                            params = {key: float(value) if isinstance(value, Decimal) else value.isoformat() if isinstance(value, datetime) else value
                                      for key, value in (params or {}).items()}
                            return sql.execute(text(query), params)

                    conn = Connection()

                    def locked(*args):
                        stage = dict(sql.execute(text(f'select * from {stage_table}')).mappings().one())
                        for field in ('inicio','termino'):
                            if stage[field]: stage[field] = datetime.fromisoformat(stage[field])
                        return kind, {'status': 'ATIVA'}, stage

                    def save_stage(connection, target, code, payload, actor, **kwargs):
                        # Only parent-order lifecycle/locks are outside this fixture.
                        # Canonical payload and all actual timing writes are exercised.
                        sql.execute(text(f'''update {stage_table} set status=:status,responsavel=:responsavel,
                            inicio=coalesce(inicio,:inicio),termino=:termino,observacoes=:observacoes'''), {
                            **payload,'status': 'CONCLUÍDA' if payload['input_code']=='S' else 'EM_ANDAMENTO',
                            'inicio': payload.get('inicio').isoformat() if payload.get('inicio') else None,
                            'termino': payload.get('termino').isoformat() if payload.get('termino') else None,
                        })
                        sql.execute(text(f'''insert into {event_table}(idempotency_key,action,{fk},novo_status)
                            values(:key,'APONTAMENTO','stage',:status)'''), {'key':payload.get('idempotency_key'),'status':payload['input_code']})
                        return {'input_code':payload['input_code']}

                    with (
                        patch.object(erp_service,'_stage_pause_schema_ready',return_value=True),
                        patch.object(erp_service,'_setup_schema_ready',return_value=True),
                        patch.object(erp_service,'_production_locked_stage',side_effect=locked),
                        patch.object(erp_service,'_locked_work_and_stage',side_effect=lambda *args: locked()[1:]),
                        patch.object(erp_service,'update_stage',side_effect=save_stage),
                        patch.object(erp_service,'update_vehicle_entry_stage',side_effect=save_stage),
                    ):
                        commands = [('INICIAR','08:00','A'),('INICIAR','08:10','A'),('INTERROMPER','08:40','A'),
                            ('INICIAR','08:55','B'),('PARAR','09:15','B'),('INICIAR','09:20','C'),
                            ('SETUP','09:35','C'),('PARAR','09:40','C'),('INICIAR','09:45','D'),
                            ('INICIAR','09:50','D'),('FINALIZAR','10:00','D')]
                        for index,(action,clock,operator) in enumerate(commands):
                            # The normal and APONTAMENTO consoles cannot diverge
                            # even when legacy callers send auto_time_fields=False.
                            payload={'action':action,'auto_time_fields':index%2==0,'responsavel':operator,
                                'expected_status':'N' if index==0 else 'P','idempotency_key':str(index),
                                'observacoes':'OBSERVAÇÃO ORIGINAL',
                                ('inicio' if action=='INICIAR' else 'termino' if action=='FINALIZAR' else 'momento'):f'2026-10-03T{clock}:00-03:00'}
                            erp_service.execute_production_stage_command(conn,kind,'target','REVEST',payload,'USUÁRIO COMPARTILHADO')
                            # Duplicate delivery must not increment any interval/counter.
                            replay=erp_service.execute_production_stage_command(conn,kind,'target','REVEST',payload,'USUÁRIO COMPARTILHADO')
                            self.assertTrue(replay['replayed'])
                        row=locked()[2]
                        self.assertEqual((row['production_time_hours'],row['setup_time_hours'],row['total_stopped_time_hours']),(1.25,0.33,0.42))
                        self.assertEqual(row['status'],'CONCLUÍDA')
                        self.assertEqual(row['inicio'].isoformat(),'2026-10-03T11:00:00+00:00')
                        self.assertEqual(row['observacoes'],'OBSERVAÇÃO ORIGINAL')
                        self.assertEqual(row['responsavel'],'A / B / C / D')
                        self.assertEqual(erp_service._production_execution_operators(conn,kind,'stage'),['A','B','C','D'])
                        counter=sql.execute(text('select * from erp_stage_auto_time_counters')).mappings().one()
                        self.assertEqual((counter['production_seconds'],counter['setup_seconds'],counter['stopped_seconds']),(4500,1200,1500))
                        pauses=sql.execute(text('select resume_phase,execution_operator from erp_stage_time_pauses order by started_at')).all()
                        self.assertEqual(pauses,[('PRODUCAO','A'),('PRODUCAO','B'),('SETUP','C')])
                        for table in ('erp_stage_time_sessions','erp_stage_setup_sessions','erp_stage_time_pauses'):
                            self.assertEqual(sql.execute(text(f'select count(*) from {table} where ended_at is null')).scalar_one(),0)
                        events=sql.execute(text(f"select * from {event_table} where action='TEMPOS_AUTOMATICOS'")).mappings().all()
                        self.assertAlmostEqual(sum(row['production_time_hours'] or 0 for row in events),1.25)
                        self.assertAlmostEqual(sum(row['setup_time_hours'] or 0 for row in events),0.33)
                        self.assertAlmostEqual(sum(row['total_stopped_time_hours'] or 0 for row in events),0.42)
                        token=erp_service._stage_sync_token(row,erp_service._execution_intervals(conn,kind,'stage'))
                        correction={'input_code':'S','expected_status':'S','idempotency_key':'manual-correction',
                            'expected_sync_token':token,'responsavel':'D',
                            'observacoes':'OBSERVAÇÃO ORIGINAL','inicio':'2026-10-03T07:50:00-03:00',
                            'termino':'2026-10-03T10:30:00-03:00'}
                        erp_service.update_synchronized_stage(conn,kind,'target','REVEST',correction,'PCP',metadata_only=kind=='work')
                        corrected=locked()[2]
                        self.assertEqual((corrected['production_time_hours'],corrected['setup_time_hours'],corrected['total_stopped_time_hours']),(1.75,0.5,0.42))
                        self.assertEqual(corrected['inicio'].isoformat(),'2026-10-03T10:50:00+00:00')
                        self.assertEqual(corrected['termino'].isoformat(),'2026-10-03T13:30:00+00:00')
                        self.assertEqual(corrected['responsavel'],'A / B / C / D')
                        self.assertTrue(erp_service.update_synchronized_stage(conn,kind,'target','REVEST',correction,'PCP',metadata_only=kind=='work')['replayed'])
                        # A stale manual page cannot replace the new automatic
                        # totals after another console has changed the stage.
                        with self.assertRaises(erp_service.StageConflictError):
                            erp_service.update_synchronized_stage(conn,kind,'target','REVEST',{
                                **correction,'idempotency_key':'stale-write'},'PCP',metadata_only=kind=='work')
                        erp_service.update_production_manual_times(conn,kind,'target','REVEST',{'production_time_hours':2,'idempotency_key':'manual-total'},'PCP')
                        # Subsequent adjustments add to the explicit baseline.
                        for action,clock in [('INICIAR','11:00'),('INICIAR','11:10'),('FINALIZAR','11:40')]:
                            payload={'action':action,'responsavel':'E','observacoes':'Novo ciclo',
                                'expected_status':'S' if clock=='11:00' else 'P','idempotency_key':clock,
                                ('termino' if action=='FINALIZAR' else 'inicio'):f'2026-10-03T{clock}:00-03:00'}
                            erp_service.execute_production_stage_command(conn,kind,'target','REVEST',payload,'OUTRO LOGIN')
                        final=locked()[2]
                        self.assertEqual(final['production_time_hours'],2.5)
                        self.assertEqual(final['setup_time_hours'],0.67)
                        self.assertEqual(final['responsavel'],'A / B / C / D / E')
                        self.assertEqual(final['inicio'],corrected['inicio'])
                engine.dispose()

    def test_completed_status_wins_over_stale_timer_in_operator_template(self):
        env=Environment(loader=FileSystemLoader(Path(__file__).parent/'templates'))
        html=env.get_template('producao_operador.html').render(stage={
            'stage_code':'ELÉTRICA','input_code':'S','open_session':{'id':'old'},'execution_operators':[],
        },detail={'target_kind':'work','target_id':'test','item_number':3202},current_user={'nome':'Operador'})
        self.assertIn('S — OK / ETAPA CONCLUÍDA',html)
        self.assertNotIn('Produção iniciada em',html)
