import asyncio
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text

from erp_catalogs import payload, transformation_options
from erp_service import _invalid_controlled_work_order_fields


class TransformationCatalogTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://')
        self.conn = self.engine.connect()
        self.conn.execute(text('''
            create table cadastro_registros (
                sku text primary key, descricao_primaria text, ativo boolean
            )
        '''))
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.conn.close)

    def register(self, code, description, active=True):
        self.conn.execute(text('''
            insert into cadastro_registros values (:code,:description,:active)
        '''), {'code': code, 'description': description, 'active': active})

    def test_executive_uses_current_cadastro_description(self):
        description = 'JI EXECUTIVE MASTER L3H2 VITRE PLUS MASTER'
        self.register('40340041', description)
        options = payload(self.conn)['transformacoes']
        self.assertEqual([
            {'codigo': 'N/A', 'descricao': 'N/A'},
            {'codigo': '40340041', 'descricao': description},
        ], options)
        self.assertEqual([], _invalid_controlled_work_order_fields({
            'transformacao_codigo': '40340041', 'transformacao': description,
        }, self.conn))

    def test_new_transformation_is_available_without_restart(self):
        self.assertEqual(1, len(transformation_options(self.conn)))
        self.register('40340099', 'NOVA TRANSFORMACAO')
        self.assertIn({'codigo': '40340099', 'descricao': 'NOVA TRANSFORMACAO'},
                      transformation_options(self.conn))
        self.assertEqual([], _invalid_controlled_work_order_fields({
            'transformacao_codigo': '40340099', 'transformacao': 'NOVA TRANSFORMACAO',
        }, self.conn))

    def test_rename_and_deactivation_take_effect_on_next_read(self):
        self.register('40340041', 'DESCRICAO ANTERIOR')
        payload(self.conn)
        self.conn.execute(text("update cadastro_registros set descricao_primaria='NOVA DESCRICAO'"))
        self.assertEqual('NOVA DESCRICAO', transformation_options(self.conn)[1]['descricao'])
        self.conn.execute(text('update cadastro_registros set ativo=false'))
        self.assertEqual([{'codigo': 'N/A', 'descricao': 'N/A'}], transformation_options(self.conn))
        self.assertEqual(['transformacao'], _invalid_controlled_work_order_fields({
            'transformacao_codigo': '40340041', 'transformacao': 'NOVA DESCRICAO',
        }, self.conn))

    def test_non_transformations_inactive_and_incomplete_records_are_excluded(self):
        self.register('30200372', 'CJ BANCOS')
        self.register('10200069', 'BCO CARONA')
        self.register('40340099', 'INATIVA', False)
        self.register('40340098', ' ')
        self.register('40INVALID', 'INVALIDA')
        self.assertEqual([{'codigo': 'N/A', 'descricao': 'N/A'}], transformation_options(self.conn))

    def test_mismatched_or_unknown_transformation_is_rejected(self):
        self.register('40340041', 'JI EXECUTIVE MASTER')
        for code, description in [('40340041', 'DESCRICAO INCORRETA'),
                                  ('40340099', 'JI EXECUTIVE MASTER')]:
            self.assertEqual(['transformacao'], _invalid_controlled_work_order_fields({
                'transformacao_codigo': code, 'transformacao': description,
            }, self.conn))

    def test_blank_and_not_applicable_remain_valid(self):
        for work in [{}, {'transformacao_codigo': 'N/A', 'transformacao': 'N/A'}]:
            self.assertEqual([], _invalid_controlled_work_order_fields(work, self.conn))

    def test_mes_catalog_endpoint_reads_registrations(self):
        import main

        self.register('40340099', 'NOVA TRANSFORMACAO')
        with (patch.object(main, 'erp_feature_enabled', return_value=True),
              patch.object(main, 'require_login', return_value=object()),
              patch.object(main, 'has_permission', return_value=True),
              patch.object(main.erp_service, 'recent_entry_clients', return_value=[])):
            result = asyncio.run(main.erp_catalogs_api(object(), self.conn))
        self.assertEqual('40340099', result['transformacoes'][1]['codigo'])

    def test_suprimentos_internal_catalog_endpoint_reads_registrations(self):
        import main

        self.register('40340099', 'NOVA TRANSFORMACAO')
        with (patch.object(main, 'erp_feature_enabled', return_value=True),
              patch.object(main, 'erp_backend_actor', return_value='PCP'),
              patch.object(main.database.engine, 'connect', return_value=nullcontext(self.conn))):
            result = asyncio.run(main.erp_internal_catalogs(object()))
        self.assertEqual('40340099', result['transformacoes'][1]['codigo'])


if __name__ == '__main__':
    unittest.main()
