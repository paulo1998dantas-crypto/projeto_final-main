"""Edits and logical deletion use isolated databases, never operational records."""
import unittest
from uuid import uuid4
from sqlalchemy import select,func,text
import quality_ftt as q
import test_quality_ftt as legacy
import test_quality_audits as audits


class EditingTests(unittest.TestCase):
    setUp=legacy.FTTTests.setUp
    tearDown=legacy.FTTTests.tearDown
    payload=legacy.FTTTests.payload
    save=legacy.FTTTests.save
    client=legacy.FTTTests.client
    audit=audits.AuditTests.audit
    quality=audits.AuditTests.quality

    def setup_report(self,two=True):
        p=self.audit()
        if two:p['issues'].append(dict(defect_id=self.defect,stage_code='ELÉTRICA'))
        self.identifier=self.save(p)['id']
        return self.record()

    def record(self,include_voided=True):
        with self.engine.connect() as c:
            return q.history(c,work_id=self.work,include_voided=include_voided)['reports'][0]

    def data(self,r,**kwargs):
        d=dict(idempotency_key=str(uuid4()),version=r['version'],reason='Corrigir lançamento da Qualidade',
               observed_on=str(r['observed_on']),audit_complete=r['audit_complete'],
               issues=[{k:i[k] for k in ('id','defect_id','stage_code')} for i in r['issues']])
        d.update(kwargs);return d

    def edit(self,data,delete=False):
        with self.engine.begin() as c:
            return q.change_report(c,self.identifier,data,self.user,['BCO','ELÉTRICA'],delete=delete)

    def test_remove_one_of_two_responsible_areas_preserves_old_rows_and_evidence(self):
        r=self.setup_report();data=self.data(r,issues=[self.data(r)['issues'][0]])
        result=self.edit(data);self.assertTrue(self.edit(data)['replayed'])
        r=self.record();self.assertEqual(1,len(r['issues']));self.assertEqual(2,len(r['events'][0]['before_data']['issues']))
        self.assertEqual(1,len(r['events'][0]['after_data']['issues']))
        self.assertEqual(result['id'],r['events'][0]['id'])
        self.assertEqual(1,self.quality()['totals']['defective'])
        with self.engine.connect() as c:
            self.assertEqual(2,c.execute(select(func.count()).select_from(q.issues)).scalar())
            self.assertEqual(1,c.execute(select(func.count()).select_from(q.issues).where(q.issues.c.active.is_(True))).scalar())

    def test_edit_date_moves_audit_without_changing_production(self):
        r=self.setup_report();self.edit(self.data(r,observed_on='2026-11-01'))
        self.assertEqual(0,self.quality()['vehicles']['audited'])
        self.assertEqual(1,self.quality(end='2026-11-01')['vehicles']['audited'])
        self.assertEqual(1,self.quality()['totals']['produced'])
        with self.engine.connect() as c:self.assertEqual(0,len(q.history(c,observed_on='2026-10-27')['reports']))

    def test_edit_to_clean_audit_only_after_explicit_complete_flag(self):
        r=self.setup_report();self.edit(self.data(r,issues=[]))
        self.assertEqual(100,self.quality()['vehicles']['ftt'])
        self.assertEqual(0,self.quality()['total_failures'])
        self.assertEqual(2,len(self.record()['events'][0]['before_data']['issues']))

    def test_delete_excludes_every_indicator_but_preserves_history_and_production(self):
        r=self.setup_report();data=self.data(r,confirm=True)
        self.edit(data,delete=True);self.assertTrue(self.edit(data,delete=True)['replayed'])
        result=self.quality();self.assertEqual(0,result['vehicles']['audited']);self.assertEqual(0,result['total_failures'])
        self.assertEqual(1,result['totals']['produced'])
        with self.engine.connect() as c:
            self.assertEqual([],q.history(c,work_id=self.work)['reports'])
            self.assertEqual(1,c.execute(select(func.count()).select_from(q.reports)).scalar())
            self.assertEqual('ATIVA',c.execute(text('select status from erp_work_orders')).scalar())
            self.assertEqual(0,q.dashboard(c,['BCO','ELÉTRICA'],'2026-10-26','2026-10-31')['total_failures'])
        removed=self.record();self.assertTrue(removed['voided']);self.assertEqual('DELETE',removed['events'][0]['action'])
        self.assertEqual(2,len(removed['issues']))
        with self.assertRaises(ValueError):self.edit(self.data(removed,observed_on='2026-10-28'))

    def test_rejection_of_unconfirmed_delete_stale_version_and_duplicate_rows_is_atomic(self):
        r=self.setup_report()
        for data,delete in [(self.data(r),True),(self.data(r,version=2),False),
                            (self.data(r,reason='x'),False),(self.data(r,issues=[r['issues'][0],r['issues'][0]]),False),
                            (self.data(r,observed_on='2026-11-02'),False),
                            (self.data(r,issues=[],audit_complete=False),False)]:
            with self.assertRaises(ValueError):self.edit(data,delete)
        self.assertEqual(1,self.record()['version']);self.assertEqual([],self.record()['events'])

    def test_foreign_issue_ids_and_idempotency_key_reuse_are_rejected(self):
        r=self.setup_report();d=self.data(r);d['issues'][0]['id']=str(uuid4())
        with self.assertRaises(ValueError):self.edit(d)
        d=self.data(r,observed_on='2026-10-28');self.edit(d)
        d['observed_on']='2026-10-29'
        with self.assertRaises(ValueError):self.edit(d)

    def test_removed_sector_can_be_added_again_without_duplicate_or_losing_previous_events(self):
        r=self.setup_report();original=self.data(r)['issues'];self.edit(self.data(r,issues=original[:1]))
        r=self.record();added=dict(original[1]);added.pop('id')
        self.edit(self.data(r,issues=self.data(r)['issues']+[added]))
        self.assertEqual(2,len(self.record()['issues']));self.assertEqual(2,len(self.record()['events']))
        self.assertEqual(2,self.quality()['total_failures'])

    def test_http_edit_delete_csrf_and_excluded_history(self):
        r=self.setup_report();client=self.client();data=self.data(r,issues=self.data(r)['issues'][:1])
        url=f'/api/ftt/reports/{self.identifier}'
        self.assertEqual(403,client.post(url+'/edit',json=data).status_code)
        headers={'X-FTT-Request':'1','Origin':'http://testserver'}
        res=client.post(url+'/edit',json=data,headers=headers);self.assertEqual(200,res.status_code,res.text)
        r=self.record();data=self.data(r,confirm=True)
        self.assertEqual(403,client.post(url+'/delete',json=data,headers={**headers,'Origin':'https://other.test'}).status_code)
        res=client.post(url+'/delete',json=data,headers=headers);self.assertEqual(200,res.status_code,res.text)
        self.assertEqual([],client.get('/api/ftt/history?day=2026-10-27').json()['reports'])
        self.assertTrue(client.get('/api/ftt/history?day=2026-10-27&include_voided=1').json()['reports'][0]['voided'])


if __name__=='__main__':unittest.main()
