"""Quality audit tests on isolated SQLite, never the operational database."""
import unittest
from datetime import date
from uuid import uuid4
from io import BytesIO
from sqlalchemy import text, select, func
from openpyxl import load_workbook
import authz
import quality_ftt as q
import test_quality_ftt as legacy


class AuditTests(unittest.TestCase):
    setUp = legacy.FTTTests.setUp
    tearDown = legacy.FTTTests.tearDown
    payload = legacy.FTTTests.payload
    save = legacy.FTTTests.save
    client = legacy.FTTTests.client
    def audit(self, clean=False, when="2026-10-27"):
        p = self.payload(audit_complete=True)
        p["observed_on"] = when
        if clean:
            p["issues"] = []
        return p

    def quality(self, start="2026-10-26", end="2026-10-31"):
        with self.engine.connect() as c:
            return q.quality_dashboard(c,["BCO","ELÉTRICA","LIBERAÇÃO"],start,end)

    def change(self, issue_id, **kwargs):
        data = dict(idempotency_key=str(uuid4()),version=1,stage_code="ELÉTRICA",reason="Área incorreta na digitação")
        data.update(kwargs)
        with self.engine.begin() as c:
            return q.correct_issue(c,issue_id,data,self.user,["BCO","ELÉTRICA"])

    def issue(self):
        with self.engine.connect() as c:
            return dict(c.execute(select(q.issues)).mappings().first())

    def test_production_alone_does_not_invent_an_audit(self):
        r=self.quality()
        self.assertEqual(1,r["totals"]["produced"])
        self.assertEqual(0,r["totals"]["audited"])
        self.assertIsNone(r["vehicles"]["ftt"])
        self.assertEqual(["BCO","ELÉTRICA"],[a["area"] for a in r["areas"]])

    def test_approved_audit_is_persisted_and_counts_pending_areas(self):
        p=self.audit(clean=True);saved=self.save(p);self.assertTrue(self.save(p)["replayed"])
        r=self.quality()
        self.assertEqual(dict(audited=1,defective=0,first_pass=1,ftt=100),r["vehicles"])
        self.assertEqual((2,0,2,100),(r["totals"]["audited"],r["totals"]["defective"],r["totals"]["first_pass"],r["totals"]["ftt"]))
        with self.engine.connect() as c:
            rows=q.history(c,observed_on="2026-10-27")["reports"]
        self.assertEqual(saved["id"],rows[0]["id"])
        self.assertEqual([],rows[0]["issues"])
        self.assertEqual(["BCO","ELÉTRICA"],sorted(rows[0]["areas"]))

    def test_failure_does_not_wait_for_production_completion(self):
        p=self.audit();p["issues"][0]["stage_code"]="ELÉTRICA";self.save(p)
        r=self.quality()
        self.assertEqual((1,1,1,0),(r["areas"][1]["produced"],r["areas"][1]["audited"],r["areas"][1]["defective"],r["areas"][1]["ftt"]))
        self.assertEqual(0,r["vehicles"]["ftt"])
        self.assertEqual(50,r["totals"]["ftt"])

    def test_defect_registration_establishes_passage_for_applicable_areas(self):
        self.save(self.payload());r=self.quality()
        self.assertEqual(1,r["total_failures"])
        self.assertEqual(1,r["vehicles"]["audited"])
        self.assertEqual(0,r["pending"])
        self.assertEqual((2,2),(r["totals"]["produced"],r["totals"]["audited"]))
        # A subsequent explicit full audit uses existing known failures too.
        self.save(self.audit(clean=True,when="2026-10-28"))
        self.assertEqual(1,self.quality()["vehicles"]["defective"])

    def test_reinspection_never_duplicates_unit_or_erases_failed_first_pass(self):
        self.save(self.audit());self.save(self.audit(clean=True,when="2026-10-28"))
        r=self.quality();self.assertEqual(1,r["vehicles"]["audited"])
        self.assertEqual(1,r["vehicles"]["defective"])
        self.assertEqual(0,self.quality("2026-10-28")["vehicles"]["audited"])
        with self.engine.connect() as c:
            self.assertEqual(2,len(q.history(c,work_id=self.work)["reports"]))

    def test_late_failure_respects_as_of_date(self):
        self.save(self.audit(clean=True));self.save(self.audit(when="2026-10-29"))
        self.assertEqual(100,self.quality(end="2026-10-28")["vehicles"]["ftt"])
        self.assertEqual(0,self.quality(end="2026-10-29")["vehicles"]["ftt"])

    def test_revisions_keep_identity_but_repeated_chassis_do_not_merge_entries(self):
        self.save(self.audit())
        with self.engine.begin() as c:
            c.execute(text("update erp_work_orders set is_current=false"))
            new=str(uuid4())
            c.execute(text("insert into erp_work_orders values(:id,'3185.1','e','ATIVA',true)"),{"id":new})
            c.execute(text("insert into erp_work_order_stages values(:id,'BCO','PENDENTE',true,null)"),{"id":new})
        self.work=new;self.save(self.audit(clean=True,when="2026-10-28"))
        self.assertEqual(1,self.quality()["vehicles"]["audited"])
        with self.engine.begin() as c:
            c.execute(text("insert into erp_vehicle_entries values('e2','v',9999)"))
            other=str(uuid4())
            c.execute(text("insert into erp_work_orders values(:id,'9999','e2','ATIVA',true)"),{"id":other})
            c.execute(text("insert into erp_work_order_stages values(:id,'BCO','PENDENTE',true,null)"),{"id":other})
        self.work=other;self.save(self.audit(clean=True))
        self.assertEqual(2,self.quality()["vehicles"]["audited"])

    def test_areas_are_frozen_at_audit_time(self):
        self.save(self.audit(clean=True))
        with self.engine.begin() as c:
            c.execute(text("update erp_work_order_stages set aplicavel=false,status='PENDENTE',termino=null"))
        self.assertEqual(2,self.quality()["totals"]["audited"])
        self.assertEqual(2,self.quality()["totals"]["produced"])

    def test_historical_defect_only_report_uses_its_mes_route_without_rewriting(self):
        saved=self.save(self.payload())
        with self.engine.begin() as c:
            c.execute(q.audit_areas.delete())
            c.execute(q.reports.update().values(audit_complete=False))
            before=list(c.execute(text("select * from erp_work_order_stages")))
        result=self.quality()
        self.assertEqual((2,2,1),(result['totals']['produced'],result['totals']['audited'],result['vehicles']['defective']))
        with self.engine.connect() as c:
            report=q.history(c,work_id=self.work)['reports'][0]
            self.assertTrue(report['audit_complete'])
            self.assertEqual({'BCO','ELÉTRICA'},set(report['areas']))
            self.assertEqual(before,list(c.execute(text("select * from erp_work_order_stages"))))
            self.assertFalse(c.execute(select(q.reports.c.audit_complete)).scalar_one())
            self.assertEqual(1,c.execute(select(q.reports.c.version)).scalar_one())

    def test_only_applicable_stages_and_never_release_are_inferred(self):
        with self.engine.begin() as c:
            c.execute(text("insert into erp_work_order_stages values(:id,'VIDROS','PENDENTE',false,null),(:id,'LIBERAÇÃO','PENDENTE',true,null)"),{'id':self.work})
            q.create_report(c,self.audit(clean=True),self.user,['BCO','ELÉTRICA','VIDROS','LIBERAÇÃO'])
        with self.engine.connect() as c:
            r=q.quality_dashboard(c,['BCO','ELÉTRICA','VIDROS','LIBERAÇÃO'],'2026-10-26','2026-10-31')
        self.assertEqual((0,0),[(a['produced'],a['audited']) for a in r['areas'] if a['area']=='VIDROS'][0])
        self.assertNotIn('LIBERAÇÃO',[a['area'] for a in r['areas']])

    def test_mes_completion_and_reaudit_do_not_duplicate_quality_passage(self):
        self.save(self.audit());self.save(self.audit(clean=True,when='2026-10-28'))
        with self.engine.begin() as c:
            c.execute(text("update erp_work_order_stages set status='CONCLUÍDA',termino='2026-10-29T15:00:00+00:00'"))
        self.assertEqual((2,2),(self.quality()['totals']['produced'],self.quality()['totals']['audited']))
        self.assertEqual(0,self.quality('2026-10-28','2026-10-28')['totals']['produced'])

    def test_audit_infers_pending_route_without_changing_mes_and_respects_period(self):
        with self.engine.begin() as c:
            c.execute(text("update erp_work_order_stages set status='PENDENTE',termino=null"))
        self.save(self.audit(clean=True,when='2026-10-28'))
        self.assertEqual(0,self.quality(end='2026-10-27')['totals']['produced'])
        self.assertEqual(2,self.quality('2026-10-28','2026-10-28')['totals']['produced'])
        with self.engine.connect() as c:
            self.assertEqual(2,c.execute(text("select count(*) from erp_work_order_stages where status='PENDENTE' and termino is null")).scalar_one())

    def test_correction_moves_one_failure_and_preserves_audit_evidence(self):
        p=self.audit();self.save(p);i=self.issue()
        key=str(uuid4());self.change(i["id"],idempotency_key=key)
        self.assertTrue(self.change(i["id"],idempotency_key=key)["replayed"])
        with self.engine.connect() as c:
            r=q.history(c,work_id=self.work)["reports"][0]
        self.assertEqual((2,"PAULO","ELÉTRICA"),(r["version"],r["actor"],r["issues"][0]["stage_code"]))
        self.assertEqual("BCO",r["corrections"][0]["from_stage"])
        self.assertEqual(1,len(r["corrections"]))
        areas=self.quality()["areas"]
        self.assertEqual((0,1),(areas[0]["defective"],areas[1]["defective"]))
        self.assertEqual(1,self.quality()["total_failures"])
        with self.assertRaisesRegex(ValueError,"já foi corrigido"):
            self.change(i["id"],stage_code="BCO")
        with self.assertRaisesRegex(ValueError,"outro lançamento|outra correção"):
            self.change(i["id"],idempotency_key=key,reason="Outro motivo informado")

    def test_correction_requires_reason_allowed_area_and_prevents_duplicate(self):
        p=self.audit();p["issues"].append(dict(defect_id=self.defect,stage_code="ELÉTRICA"));self.save(p)
        i=self.issue()
        for kw in [dict(reason="x"),dict(stage_code="LIBERAÇÃO"),dict(version=True),dict(stage_code="PREP")]:
            with self.assertRaises(ValueError):self.change(i["id"],**kw)
        with self.assertRaisesRegex(ValueError,"já está registrado"):
            self.change(i["id"],stage_code="ELÉTRICA" if i["stage_code"]=="BCO" else "BCO")
        with self.engine.connect() as c:
            self.assertEqual(0,c.execute(select(func.count()).select_from(q.corrections)).scalar())
            self.assertEqual(1,c.execute(select(q.reports.c.version)).scalar())

    def test_http_day_history_correction_and_approved_audit(self):
        client=self.client();headers={"X-FTT-Request":"1","Origin":"http://testserver"}
        res=client.post("/api/ftt/reports",json=self.audit(clean=True),headers=headers)
        self.assertEqual(200,res.status_code,res.text)
        data=client.get("/api/ftt/history?day=2026-10-27").json()
        self.assertEqual(1,len(data["reports"]))
        self.assertEqual([],client.get("/api/ftt/history?day=2026-10-28").json()["reports"])
        self.assertEqual(400,client.get("/api/ftt/history?day=invalid").status_code)
        self.assertEqual(100,client.get("/api/ftt/quality?from=2026-10-26&to=2026-10-31").json()["vehicles"]["ftt"])
        self.save(self.audit());issue=self.issue()
        payload=dict(idempotency_key=str(uuid4()),version=1,stage_code="ELÉTRICA",reason="Correção de digitação")
        self.assertEqual(403,client.post(f'/api/ftt/issues/{issue["id"]}/area',json=payload).status_code)
        res=client.post(f'/api/ftt/issues/{issue["id"]}/area',json=payload,headers=headers)
        self.assertEqual(200,res.status_code,res.text)

    def test_migration_missing_returns_explicit_error_without_partial_write(self):
        with self.engine.begin() as c:
            q.audit_areas.drop(c)
        response=self.client().get("/api/ftt/quality")
        self.assertEqual(503,response.status_code)
        self.assertIn(q.AUDIT_MIGRATION,response.json()["error"])
        with self.assertRaises(ValueError):self.save(self.audit(clean=True))

    def test_read_only_profile_can_query_history_but_not_correct_or_register(self):
        self.save(self.audit());issue=self.issue()
        self.user=authz.Principal(8,"CONSULTA","CONSULTA",True,1,frozenset({"CONSULTA"}),frozenset({"mes.quality.read"}))
        client=self.client()
        self.assertNotIn('id="quality-form"',client.get("/ftt").text)
        self.assertEqual(200,client.get("/api/ftt/history?day=2026-10-27").status_code)
        payload=dict(idempotency_key=str(uuid4()),version=1,stage_code="ELÉTRICA",reason="Correção de área")
        self.assertEqual(403,client.post(f'/api/ftt/issues/{issue["id"]}/area',json=payload,headers={"X-FTT-Request":"1"}).status_code)
        self.assertEqual(403,client.post("/api/ftt/reports",json=self.audit(clean=True),headers={"X-FTT-Request":"1"}).status_code)

    def test_export_keeps_approved_audits_and_all_eleven_empty_areas(self):
        self.save(self.audit(clean=True))
        client=self.client();response=client.get("/api/ftt/export.xlsx?from=2026-10-26&to=2026-10-31")
        self.assertEqual(200,response.status_code)
        book=load_workbook(BytesIO(response.content))
        self.assertEqual("Auditadas pela Qualidade",book["FTT por área"].cell(1,3).value)
        self.assertEqual(0,book["Auditorias e histórico"].cell(2,6).value)
        self.assertEqual("Auditoria completa",book["Auditorias e histórico"].cell(2,5).value)
        with self.engine.connect() as c:
            r=q.quality_dashboard(c,list(q.AREA_NAMES),"2026-10-26","2026-10-31")
        self.assertEqual(11,len(r["areas"]))
        self.assertEqual(0,r["areas"][0]["audited"])
        self.assertIsNone(r["areas"][0]["ftt"])

    def test_history_can_find_delivered_and_old_revision_work_orders(self):
        self.save(self.audit())
        with self.engine.begin() as c:
            c.execute(text("update erp_work_orders set is_current=false,status='RETIRADA'"))
            self.assertEqual([],q.work_options(c,"3185"))
            self.assertEqual(self.work,q.work_options(c,"3185",include_history=True)[0]["id"])


if __name__=="__main__":unittest.main()
