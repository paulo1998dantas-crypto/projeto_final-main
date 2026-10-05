"""Execute actual time-write SQL against the two distinct stage schemas.

SQLite checks columns and persistence without requiring production credentials.
Only PostgreSQL FOR UPDATE and Decimal adaptation are bridged for this fixture.
"""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

import erp_service
import main


class StagePersistenceTests(unittest.TestCase):
    def test_actual_manual_and_auto_time_writes_work_with_production_stage_columns(self):
        for kind in ('work', 'entry'):
            with self.subTest(kind=kind):
                engine = create_engine('sqlite://')
                stage_table = 'erp_work_order_stages' if kind == 'work' else 'erp_vehicle_entry_stages'
                event_table = 'erp_work_order_stage_events' if kind == 'work' else 'erp_vehicle_entry_stage_events'
                stage_fk = 'work_order_stage_id' if kind == 'work' else 'vehicle_entry_stage_id'
                with engine.begin() as sql:
                    sql.connection.driver_connection.create_function('now', 0, lambda: '2026-10-05T12:00:00')
                    timestamp_column = ', updated_at text' if kind == 'entry' else ''
                    sql.execute(text(f'''create table {stage_table}(
                        id text primary key, status text, inicio text, termino text,
                        observacoes text, setup_time_hours numeric, production_time_hours numeric,
                        total_stopped_time_hours numeric{timestamp_column})'''))
                    sql.execute(text(f'''create table {event_table}(
                        {stage_fk} text, action text, status_anterior text, novo_status text,
                        operador text, observacao text, setup_time_hours numeric, production_time_hours numeric,
                        total_stopped_time_hours numeric, idempotency_key text unique,
                        created_at text default current_timestamp)'''))
                    sql.execute(text(f'''create table erp_stage_auto_time_counters(
                        id text primary key default 'counter', {stage_fk} text unique,
                        production_seconds integer default 0, setup_seconds integer default 0,
                        stopped_seconds integer default 0, updated_at text)'''))
                    sql.execute(text(f'''insert into {stage_table}(id,status,inicio,observacoes)
                        values('stage','EM_ANDAMENTO','2026-10-05T08:00:00','PRESERVAR')'''))

                    class Connection:
                        def execute(self, statement, params=None):
                            params = {key: float(value) if isinstance(value, Decimal) else value
                                      for key, value in (params or {}).items()}
                            return sql.execute(text(str(statement).replace('for update', '')), params)

                    conn = Connection()

                    def locked(*args):
                        stage = dict(sql.execute(text(f'select * from {stage_table}')).mappings().one())
                        return kind, {'status': 'ATIVA'}, stage

                    with patch.object(erp_service, '_production_locked_stage', side_effect=locked):
                        erp_service.update_production_manual_times(conn, kind, 'target', 'REVEST', {
                            'setup_time_hours': '1,25', 'production_time_hours': '2,50',
                            'total_stopped_time_hours': '0,50', 'idempotency_key': 'manual',
                        }, 'OPERADOR')
                        for field in ('production_time_hours', 'setup_time_hours', 'total_stopped_time_hours'):
                            erp_service._add_auto_stage_hours(conn, kind, 'target', 'REVEST', field, 3600, 'OPERADOR', 'auto')
                        replay = erp_service.update_production_manual_times(conn, kind, 'target', 'REVEST', {
                            'production_time_hours': '999', 'idempotency_key': 'manual',
                        }, 'OPERADOR')

                    row = sql.execute(text(f'select * from {stage_table}')).mappings().one()
                    self.assertEqual((row['setup_time_hours'], row['production_time_hours'], row['total_stopped_time_hours']), (2.25, 3.5, 1.5))
                    self.assertEqual((row['status'], row['inicio'], row['termino'], row['observacoes']),
                                     ('EM_ANDAMENTO', '2026-10-05T08:00:00', None, 'PRESERVAR'))
                    self.assertTrue(replay['replayed'])
                    events = sql.execute(text(f'select * from {event_table}')).mappings().all()
                    self.assertEqual(len(events), 4)
                    self.assertTrue(all(event['operador'] == 'OPERADOR' and event['created_at'] for event in events))
                    if kind == 'entry':
                        self.assertEqual(row['updated_at'], '2026-10-05T12:00:00')
                engine.dispose()


class PointingApiFailureTests(unittest.TestCase):
    def call_command(self, *, error=None, result=None, encoding_error=None):
        transitions = []

        @contextmanager
        def begin():
            try:
                yield object()
                transitions.append('commit')
            except Exception:
                transitions.append('rollback')
                raise

        with (
            patch.object(main, 'erp_feature_enabled', return_value=True),
            patch.object(main, 'require_login', return_value=SimpleNamespace(nome='OPERADOR')),
            patch.object(main, 'can_access_production_console', return_value=True),
            patch.object(main, 'has_permission', return_value=True),
            patch.object(main, 'is_pointing_profile', return_value=True),
            patch.object(main, 'is_production_only', return_value=True),
            patch.object(main.database, 'engine', SimpleNamespace(begin=begin)),
            patch.object(erp_service, 'execute_production_stage_command', side_effect=error, return_value=result),
            patch.object(main, 'jsonable_encoder', wraps=main.jsonable_encoder, side_effect=encoding_error),
            patch.object(main.logger, 'exception') as log,
        ):
            response = asyncio.run(main.production_stage_command('work', 'target', object(),
                {'stage_code': 'REVEST', 'action': 'FINALIZAR'}, object()))
        return response, transitions, log

    def test_database_error_rolls_back_and_returns_safe_json(self):
        response, transitions, log = self.call_command(error=ProgrammingError('SQL', {}, Exception('private database detail')))
        self.assertEqual(transitions, ['rollback'])
        self.assertEqual(response.status_code, 500)
        data = json.loads(response.body)
        self.assertFalse(data['ok'])
        self.assertIn('Confira o estado da etapa', data['error'])
        self.assertNotIn('private database detail', data['error'])
        log.assert_called_once()

    def test_success_serializes_uuids_datetimes_and_decimals_before_commit(self):
        response, transitions, log = self.call_command(result={
            'open_session': {'id': UUID(int=1), 'started_at': datetime(2026, 10, 5, tzinfo=timezone.utc)},
            'production_time_hours': Decimal('1.50'),
        })
        self.assertEqual(transitions, ['commit'])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(json.loads(response.body)['ok'])
        log.assert_not_called()

    def test_serialization_error_rolls_back_instead_of_saving_and_reporting_failure(self):
        response, transitions, _ = self.call_command(result={'ok': True}, encoding_error=TypeError('Cannot serialize'))
        self.assertEqual(transitions, ['rollback'])
        self.assertEqual(response.status_code, 500)

    def test_validation_and_conflict_keep_existing_status_codes(self):
        for error, status in ((ValueError('Dados inválidos'), 400), (erp_service.StageConflictError('Etapa alterada'), 409)):
            response, transitions, log = self.call_command(error=error)
            self.assertEqual(response.status_code, status)
            self.assertEqual(transitions, ['rollback'])
            log.assert_not_called()


if __name__ == '__main__':
    unittest.main()
