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
 console.log("FTT PostgreSQL: schema, 10 defaults, limited role, RLS/grants, foreign keys, cutoff and duplicate constraints OK");
 }finally{await db.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

