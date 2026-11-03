/* Isolated PostgreSQL/WASM migration test; no operational connection. */
const {PGlite}=require("@electric-sql/pglite");
const fs=require("node:fs"),path=require("node:path"),assert=require("node:assert/strict");
(async()=>{
 const db=new PGlite();
 try{
 await db.exec(`
 create role anon;create role authenticated;create role service_role bypassrls;
 create table users(id integer primary key);
 create table erp_work_orders(id uuid primary key,status text);
 create table erp_roles(code text primary key,name text,description text,active boolean,created_at timestamptz,updated_at timestamptz);
 create table erp_permissions(code text primary key,module text,description text,created_at timestamptz);
 create table erp_role_permissions(role_code text references erp_roles(code),permission_code text references erp_permissions(code),primary key(role_code,permission_code));
 insert into erp_roles(code) values ('ADMIN');
 insert into erp_permissions(code) values ('mes.dashboard.read');
 insert into users values(9);
 insert into erp_work_orders values ('00000000-0000-4000-8000-000000001234','ATIVA');
 `);
 await db.exec(fs.readFileSync(path.join(__dirname,"../migrations/20261022_mes_quality_ftt.sql"),"utf8"));
 assert.equal((await db.query("select count(*)::int n from erp_quality_defects")).rows[0].n,10);
 const perms=(await db.query("select permission_code from erp_role_permissions where role_code='QUALIDADE' order by permission_code")).rows.map(r=>r.permission_code);
 assert.deepEqual(perms,["mes.dashboard.read","mes.quality.read","mes.quality.write"]);
 for(const table of ["erp_quality_defects","erp_quality_reports","erp_quality_issues"]){
  assert.equal((await db.query("select relrowsecurity from pg_class where oid=$1::regclass",[table])).rows[0].relrowsecurity,true);
  for(const role of ["anon","authenticated"]){
   assert.equal((await db.query("select has_table_privilege($1,$2,'SELECT,INSERT,UPDATE,DELETE') allowed",[role,table])).rows[0].allowed,false);
  }
 }
 await db.exec(`insert into erp_quality_reports values('report','00000000-0000-4000-8000-000000001234','2026-10-26',9,'PAULO',now());
 insert into erp_quality_issues values('issue','report','00000000-0000-4000-8000-000000000001','Falha','BCO','DEFEITO');`);
 await assert.rejects(db.exec("delete from erp_work_orders"));
 await assert.rejects(db.exec("insert into erp_quality_issues values('duplicate','report','00000000-0000-4000-8000-000000000001','Falha','BCO','DEFEITO')"));
 await assert.rejects(db.exec("insert into erp_quality_reports values('old','00000000-0000-4000-8000-000000001234','2026-10-25',9,'PAULO',now())"));
 assert.equal((await db.query("select status from erp_work_orders")).rows[0].status,"ATIVA");
 await db.exec(fs.readFileSync(path.join(__dirname,"../migrations/20261103195306_mes_quality_audits.sql"),"utf8"));
 const legacy=(await db.query("select audit_complete,version from erp_quality_reports where id='report'")).rows[0];
 assert.deepEqual(legacy,{audit_complete:false,version:1});
 assert.equal((await db.query("select count(*)::int n from erp_quality_report_areas")).rows[0].n,0);
 for(const table of ["erp_quality_report_areas","erp_quality_corrections"]){
  assert.equal((await db.query("select relrowsecurity from pg_class where oid=$1::regclass",[table])).rows[0].relrowsecurity,true);
  for(const role of ["anon","authenticated"]){
   assert.equal((await db.query("select has_table_privilege($1,$2,'SELECT,INSERT,UPDATE,DELETE') allowed",[role,table])).rows[0].allowed,false);
  }
  assert.equal((await db.query("select has_table_privilege('service_role',$1,'SELECT,INSERT') allowed",[table])).rows[0].allowed,true);
  assert.equal((await db.query("select has_table_privilege('service_role',$1,'UPDATE,DELETE') allowed",[table])).rows[0].allowed,false);
 }
 await db.exec(`
 insert into erp_quality_reports(id,work_order_id,observed_on,actor_id,actor,audit_complete)
 values('approved','00000000-0000-4000-8000-000000001234','2026-10-27',9,'PAULO',true);
 insert into erp_quality_report_areas values('approved','BCO');
 update erp_quality_reports set version=2 where id='report';
 update erp_quality_issues set stage_code='SERRA' where id='issue';
 insert into erp_quality_corrections values('correction','report','issue','BCO','SERRA','Corrigir responsabilidade',9,'PAULO',now(),2);
 `);
 await assert.rejects(db.exec("insert into erp_quality_report_areas values('approved','LIBERAÇÃO')"));
 await assert.rejects(db.exec("insert into erp_quality_report_areas values('approved','BCO')"));
 await assert.rejects(db.exec("delete from erp_quality_issues where id='issue'"));
 await assert.rejects(db.exec("insert into erp_quality_corrections values('another','report','issue','BCO','SERRA','Motivo',9,'PAULO',now(),2)"));
 await db.exec("set role service_role;update erp_quality_reports set version=3 where id='report';update erp_quality_issues set stage_code='BCO' where id='issue';");
 await assert.rejects(db.exec("delete from erp_quality_corrections"));
 await assert.rejects(db.exec("update erp_quality_reports set actor='OTHER'"));
 await assert.rejects(db.exec("update erp_quality_issues set description='Changed'"));
 await db.exec("reset role");
 await db.exec(fs.readFileSync(path.join(__dirname,"../migrations/20261103200911_mes_quality_report_editing.sql"),"utf8"));
 assert.equal((await db.query("select count(*)::int n from erp_quality_reports where voided")).rows[0].n,0);
 assert.equal((await db.query("select relrowsecurity from pg_class where oid='erp_quality_report_events'::regclass")).rows[0].relrowsecurity,true);
 for(const role of ['anon','authenticated'])assert.equal((await db.query("select has_table_privilege($1,'erp_quality_report_events','SELECT,INSERT,UPDATE,DELETE') allowed",[role])).rows[0].allowed,false);
 await db.exec(`update erp_quality_issues set active=false where id='issue';
 insert into erp_quality_issues(id,report_id,defect_id,description,stage_code,disposition)
 values('replacement','report','00000000-0000-4000-8000-000000000001','Falha','BCO','DEFEITO');`);
 await assert.rejects(db.exec("insert into erp_quality_issues(id,report_id,defect_id,description,stage_code,disposition) values('dup','report','00000000-0000-4000-8000-000000000001','Falha','BCO','DEFEITO')"));
 await db.exec(`set role service_role;
 update erp_quality_reports set voided=true,version=4 where id='report';
 insert into erp_quality_report_events values('event','report','DELETE','{}','{}','Teste de exclusão auditada',9,'PAULO',now(),4);`);
 await assert.rejects(db.exec("delete from erp_quality_report_events"));
 await assert.rejects(db.exec("update erp_quality_report_events set reason='Alterado'"));
 await assert.rejects(db.exec("update erp_quality_reports set work_order_id=null"));
 await db.exec("reset role");
 assert.equal((await db.query("select status from erp_work_orders")).rows[0].status,"ATIVA");
 console.log("FTT PostgreSQL: audits + editing + exclusion, RLS/grants, partial uniqueness, append-only evidence, legacy/production preserved OK");
 }finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

