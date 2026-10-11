import asyncio
import unittest
from unittest.mock import patch
from pathlib import Path
import erp_commercial_proposals as service


class CommercialProposalTests(unittest.TestCase):
    def test_render_tls_proxy_and_host_comparison(self):
        self.assertTrue(service.same_origin('https://mes.example','http://mes.example/'))
        self.assertFalse(service.same_origin('https://mes.example.evil','http://mes.example/'))
        self.assertFalse(service.same_origin('https://other.example','http://mes.example/'))

    def test_picker_uses_ids_and_versions_not_free_text(self):
        source=Path('templates/mes_proposal_picker.html').read_text(encoding='utf-8')
        for token in ('program_id:row.id','program_version:row.version','expected_proposal:work.proposta_numero','Confirmar vínculo','ticket!==proposalPicker.ticket'):
            self.assertIn(token,source)
        self.assertIn('name="proposta_numero" maxlength="80" readonly',Path('templates/gestao_os.html').read_text(encoding='utf-8'))

    def test_incomplete_or_forged_selection_is_rejected(self):
        for payload in ({},{'program_id':'p','program_version':True},{'program_id':'p','program_version':1}):
            with patch.object(service,'available',return_value=True),self.assertRaises(ValueError):
                service.select_proposal(object(),'w',payload,'PAULO')

    def test_route_requires_manage_permission(self):
        import main
        with patch.object(main,'erp_feature_enabled',return_value=True),patch.object(main,'require_login',return_value=object()),patch.object(main,'has_permission',return_value=False):
            response=asyncio.run(main.erp_select_commercial_proposal('w',object(),{},object()))
        self.assertEqual(response.status_code,403)

    def test_posting_from_another_origin_is_rejected(self):
        import main
        request=type('Request',(),{'headers':{'origin':'https://other.example'},'base_url':'https://mes.example/'})()
        with patch.object(main,'erp_feature_enabled',return_value=True),patch.object(main,'require_login',return_value=object()),patch.object(main,'has_permission',return_value=True):
            response=asyncio.run(main.erp_select_commercial_proposal('w',request,{},object()))
        self.assertEqual(response.status_code,403)

    def test_open_and_edit_link_inside_transaction(self):
        source=Path('erp_service.py').read_text(encoding='utf-8')
        for method in ('create_work_order','update_work_order'):
            start=source.index('def '+method+'(')
            end=source.find('\ndef ',start+1)
            block=source[start:end]
            self.assertIn('apply_selection(conn, work_id, payload, actor)',block)

    def test_internal_entry_options_require_backend_identity(self):
        import main
        with patch.object(main,'erp_feature_enabled',return_value=True),patch.object(main,'erp_backend_actor',return_value=None):
            response=asyncio.run(main.erp_internal_entry_commercial_proposals('e',object()))
        self.assertEqual(response.status_code,401)
