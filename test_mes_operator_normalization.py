from decimal import Decimal
from pathlib import Path
from unittest import TestCase
from jinja2 import Environment, FileSystemLoader
import erp_operators as operators
import test_mes_pointing_adjustments as fixtures


class OperatorNormalizationTests(TestCase):
    def test_observed_aliases_and_known_unseparated_crews(self):
        for raw,expected in {
            'Carlos e Evrton':'CARLOS / EVERTON',
            'Cleilton / Cleilron / Cleiton':'CLEITON',
            'Geovane, Geovanny e Geovany':'GEOVANY',
            'Paulo jen':'PAULO / JEAN',
            'Paulo Iago Everton':'PAULO / IAGO / EVERTON',
            'Carlos /Geovane':'CARLOS / GEOVANY',
            'Vítor e Victor':'VITOR / VICTOR',
            'Iago Igor':'IAGO / IGOR',
            'VINÍCIUS; vinicius':'VINICIUS',
        }.items():
            with self.subTest(raw=raw):self.assertEqual(operators.normalize_operators(raw),expected)

    def test_unknown_full_names_and_known_compound_names_are_preserved(self):
        self.assertEqual(operators.operator_names('João Pedro / Eber Favacho / Grupo Euro / Francisco'),
                         ['JOAO PEDRO','EBER FAVACHO','GRUPO EURO','FRANCISCO'])
        self.assertEqual(operators.normalize_operators('Terceirizado PT A'),'TERCEIRIZADO PT A')
        self.assertEqual(operators.normalize_operators('não informado'),'NÃO INFORMADO')

    def test_explicit_crew_deduplicates_before_allocation(self):
        self.assertEqual(operators.execution_crew({'operadores':['carlos','Carlos','Everton']},'LOGIN'), 'CARLOS / EVERTON')
        for raw in ('CARLOS',None,{},[3],[],[' ']):
            with self.subTest(raw=raw),self.assertRaises(ValueError):operators.execution_crew({'operadores':raw},'LOGIN')
        self.assertEqual(operators.execution_crew({},'Novo colaborador'),'NOVO COLABORADOR')

    def test_equal_shares_preserve_total_with_rounding(self):
        self.assertEqual(operators.distribute_hours(2,2),[Decimal('1'),Decimal('1')])
        self.assertEqual(operators.distribute_hours(1,3),[Decimal('.33'),Decimal('.34'),Decimal('.33')])
        for hours in ('0','.01','1.25','9999.99'):
            for count in range(1,21):self.assertEqual(sum(operators.distribute_hours(hours,count)),Decimal(hours))

    def test_both_consoles_keep_free_entry_and_separate_fields(self):
        env=Environment(loader=FileSystemLoader(Path(__file__).parent/'templates'))
        for name in ('producao_apontamento.html','producao_operador.html'):
            html=env.get_template(name).render(stage={'stage_code':'BCO','input_code':'N','active_execution_operators':[]},
                detail={'operator_references':operators.OPERATOR_REFERENCES,'target_kind':'work','target_id':'id'},current_user={'nome':'Paulo'})
            self.assertIn('Operadores desta execução',html)
            self.assertIn('readExecutionOperators()',html)
            self.assertIn('value="EBER FAVACHO"',html)
            self.assertIn('input.maxLength=160',html)
            self.assertNotIn('<select id="responsavel"',html)

    def test_actual_command_keeps_crew_separate_from_login_identity(self):
        fixture=fixtures.PointingAdjustmentTests();fixture.setUp()
        fixture.conn.execute.return_value.scalar_one.return_value=None
        _,payload=fixture.execute({'action':'INICIAR','operadores':['carlos','Evrton','Carlos']},current='N',session=False)
        self.assertEqual(payload['responsavel'],'CARLOS / EVERTON')
        self.assertEqual(fixture.start_setup.call_args.args[4:6],('Usuário logado','CARLOS / EVERTON'))
        self.assertEqual(fixture.update.call_args.args[4],'Usuário logado')

    def test_finish_cannot_reassign_already_running_crew(self):
        fixture=fixtures.PointingAdjustmentTests();fixture.setUp()
        fixture.session['execution_operator']='CARLOS / EVERTON'
        _,payload=fixture.execute({'action':'FINALIZAR','operadores':['PAULO'],'expected_interval_id':'session-1'})
        self.assertIn('CARLOS / EVERTON',payload['responsavel'])
        self.assertNotIn('PAULO',payload['responsavel'])

    def test_migration_never_changes_canonical_hours_or_registered_users(self):
        sql=(Path(__file__).parent/'migrations/20261005_mes_operator_normalization.sql').read_text(encoding='utf-8')
        self.assertIn('before_data,after_data',sql)
        self.assertNotIn('set production_time_hours',sql)
        self.assertNotIn('set started_by',sql)
        self.assertNotIn('drop view',sql.lower())
        self.assertIn('n.name as responsavel',sql)
        self.assertIn('cardinality(crew.names)',sql)
