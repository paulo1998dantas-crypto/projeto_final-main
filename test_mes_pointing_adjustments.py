from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

import erp_service
import main


class PointingAdjustmentTests(TestCase):
    def setUp(self):
        self.conn = Mock()
        self.start = datetime(2026, 10, 1, 11, tzinfo=timezone.utc)
        self.stage = {
            'id': 'stage-1', 'status': 'EM_ANDAMENTO', 'parametrizado': True,
            'aplicavel': True, 'inicio': self.start, 'responsavel': 'Operador A',
            'observacoes': 'Observação existente',
        }
        self.session = {'id': 'session-1', 'started_at': self.start}
        self.kind = 'work'

    def execute(self, payload, *, setup=None, current='P', session=True):
        stage = {**self.stage}
        if current == 'N':
            stage.update(status='PENDENTE', inicio=None)
        timing = {'open_session': dict(self.session) if session else None, 'open_pause': None,
                  'total_productive_seconds': 0, 'total_paused_seconds': 0}
        with ExitStack() as stack:
            for name, value in (
                ('_stage_pause_schema_ready', True), ('_setup_schema_ready', True),
                ('_production_locked_stage', (self.kind, {'status': 'ATIVA'}, stage)),
                ('_production_event_replay', False), ('_pause_summary', timing),
                ('_open_stage_setup', dict(setup) if setup else None),
                ('_close_stage_session', 1800), ('_close_stage_setup', 1800),
                ('update_stage', {'input_code': 'S'}), ('update_vehicle_entry_stage', {'input_code': 'S'}),
            ):
                stack.enter_context(patch.object(erp_service, name, return_value=value))
            self.add_hours = stack.enter_context(patch.object(erp_service, '_add_auto_stage_hours'))
            self.open_session = stack.enter_context(patch.object(erp_service, '_open_stage_session'))
            self.update = erp_service.update_stage if self.kind == 'work' else erp_service.update_vehicle_entry_stage
            self.close_session = erp_service._close_stage_session
            result = erp_service.execute_production_stage_command(
                self.conn, self.kind, 'target-1', 'REVEST', {
                    'expected_status': current, 'idempotency_key': 'point-1', 'auto_time_fields': True,
                    **payload,
                }, 'Usuário logado',
            )
            return result, self.update.call_args.args[3]

    def test_start_can_report_another_operator_without_impersonating_audit_actor(self):
        self.conn.execute.return_value.scalar_one.return_value = None
        _, payload = self.execute({
            'action': 'INICIAR', 'responsavel': 'Operador B', 'inicio': '2026-10-01T08:00:00-03:00',
        }, current='N', session=False)
        self.assertEqual(payload['responsavel'], 'Operador B')
        self.assertEqual(payload['inicio'], self.start)
        self.assertEqual(payload['observacoes'], 'Observação existente')
        self.assertIn('Registrado por: Usuário logado', payload['pointing_audit_note'])
        self.assertEqual(self.open_session.call_args.args[4], 'Usuário logado')
        self.assertEqual(self.update.call_args.args[4], 'Usuário logado')

    def test_retroactive_start_cannot_overlap_previous_closed_session(self):
        self.conn.execute.return_value.scalar_one.return_value = self.start + timedelta(minutes=10)
        with self.assertRaisesRegex(ValueError, 'sobrepor'):
            self.execute({'action': 'INICIAR', 'inicio': self.start.isoformat()}, current='N', session=False)
        self.open_session.assert_not_called()

    def test_direct_finish_preserves_assigned_operator_and_automatic_timestamps(self):
        _, payload = self.execute({'action': 'FINALIZAR', 'expected_interval_id': 'session-1'})
        self.assertEqual(payload['responsavel'], 'Operador A')
        self.assertIsNone(payload['inicio'])
        self.assertEqual(payload['termino'].tzinfo, timezone.utc)
        self.conn.execute.assert_not_called()

    def test_adjusted_finish_recalculates_only_open_session_and_keeps_actor(self):
        self.conn.execute.return_value.scalar_one.return_value = None
        _, payload = self.execute({
            'action': 'FINALIZAR', 'expected_interval_id': 'session-1', 'ajustar_horarios': True,
            'inicio_sessao': '2026-10-01T08:30:00-03:00', 'termino': '2026-10-01T09:00:00-03:00',
        })
        statements = [str(call.args[0]) for call in self.conn.execute.call_args_list]
        self.assertIn('update erp_stage_time_sessions set started_at', statements[1])
        self.assertIn('update erp_work_order_stages set inicio', statements[2])
        self.assertEqual(payload['termino'], self.start + timedelta(hours=1))
        self.assertIn('Início da sessão ajustado', payload['pointing_audit_note'])
        self.add_hours.assert_called_once_with(
            self.conn, 'work', 'target-1', 'REVEST', 'production_time_hours', 1800, 'Usuário logado', 'point-1',
        )

    def test_adjustment_after_pause_preserves_first_start_and_closed_intervals(self):
        self.stage['inicio'] = self.start - timedelta(hours=2)
        self.conn.execute.return_value.scalar_one.return_value = self.start - timedelta(minutes=10)
        self.execute({
            'action': 'FINALIZAR', 'expected_interval_id': 'session-1', 'ajustar_horarios': True,
            'inicio_sessao': '2026-10-01T08:30:00-03:00', 'termino': '2026-10-01T09:00:00-03:00',
        })
        self.assertEqual(len(self.conn.execute.call_args_list), 2)
        self.assertEqual(self.stage['inicio'], self.start - timedelta(hours=2))

    def test_setup_and_pre_os_use_same_adjustment_and_setup_counter(self):
        self.kind = 'entry'
        self.conn.execute.return_value.scalar_one.return_value = None
        self.execute({
            'action': 'FINALIZAR', 'expected_interval_id': 'setup-1', 'ajustar_horarios': True,
            'inicio_sessao': '2026-10-01T08:30:00-03:00', 'termino': '2026-10-01T09:00:00-03:00',
        }, session=False, setup={'id': 'setup-1', 'started_at': self.start})
        sql = [str(call.args[0]) for call in self.conn.execute.call_args_list]
        self.assertIn('update erp_stage_setup_sessions set started_at', sql[1])
        self.assertIn('update erp_vehicle_entry_stages set inicio', sql[2])
        self.add_hours.assert_called_once_with(
            self.conn, 'entry', 'target-1', 'REVEST', 'setup_time_hours', 1800, 'Usuário logado', 'point-1',
        )

    def test_overlap_is_rejected_before_writing_or_closing(self):
        self.conn.execute.return_value.scalar_one.return_value = self.start - timedelta(minutes=5)
        with self.assertRaisesRegex(ValueError, 'sobrepor'):
            self.execute({
                'action': 'FINALIZAR', 'expected_interval_id': 'session-1', 'ajustar_horarios': True,
                'inicio_sessao': '2026-10-01T07:00:00-03:00', 'termino': '2026-10-01T09:00:00-03:00',
            })
        self.assertEqual(len(self.conn.execute.call_args_list), 1)

    def test_invalid_range_future_and_stale_session_do_not_write(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        cases = [
            ({'action': 'INICIAR', 'inicio': future}, ValueError),
            ({'action': 'FINALIZAR', 'expected_interval_id': 'other'}, erp_service.StageConflictError),
            ({'action': 'FINALIZAR', 'expected_interval_id': 'session-1', 'ajustar_horarios': True, 'termino': future}, ValueError),
            ({'action': 'FINALIZAR', 'expected_interval_id': 'session-1', 'ajustar_horarios': True,
              'inicio_sessao': '2026-10-01T10:00:00-03:00', 'termino': '2026-10-01T09:00:00-03:00'}, ValueError),
            ({'action': 'FINALIZAR', 'responsavel': ' '}, ValueError),
        ]
        for payload, error in cases:
            with self.subTest(payload=payload), self.assertRaises(error):
                self.execute(payload)
        self.conn.execute.assert_not_called()

    def test_prepared_ui_preserves_seconds_and_converts_to_brasilia(self):
        detail = main.prepare_production_detail({'stages': [{
            'open_session': {'id': 'session-1', 'started_at': self.start + timedelta(seconds=27)},
        }]})
        self.assertEqual(detail['stages'][0]['active_interval_start_input'], '2026-10-01T08:00:27')

    def test_retry_returns_before_corrections_and_double_counting(self):
        with (
            patch.object(erp_service, '_stage_pause_schema_ready', return_value=True),
            patch.object(erp_service, '_setup_schema_ready', return_value=True),
            patch.object(erp_service, '_production_locked_stage', return_value=('work', {}, self.stage)),
            patch.object(erp_service, '_production_event_replay', return_value=True),
            patch.object(erp_service, '_pause_summary', return_value={}),
        ):
            result = erp_service.execute_production_stage_command(self.conn, 'work', 'target-1', 'REVEST', {
                'action': 'FINALIZAR', 'auto_time_fields': True, 'idempotency_key': 'point-1',
                'ajustar_horarios': True, 'inicio_sessao': self.start.isoformat(),
            }, 'Usuário logado')
        self.assertTrue(result['replayed'])
        self.conn.execute.assert_not_called()
