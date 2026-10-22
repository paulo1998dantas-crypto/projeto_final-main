-- Additive quality module. No updates to production O.S., stages or pointing history.
begin;
set local lock_timeout='5s';
create table public.erp_quality_defects (
 id varchar(36) primary key, description varchar(200) not null,
 normalized varchar(200) not null unique, created_by varchar(100) not null,
 created_at timestamptz not null default now()
);
create table public.erp_quality_reports (
 id varchar(36) primary key,
 work_order_id uuid not null references public.erp_work_orders(id) on delete restrict,
 observed_on date not null check(observed_on >= date '2026-10-26'),
 actor_id integer not null references public.users(id) on delete restrict,
 actor varchar(100) not null, created_at timestamptz not null default now()
);
create table public.erp_quality_issues (
 id varchar(36) primary key, report_id varchar(36) not null references public.erp_quality_reports(id) on delete restrict,
 defect_id varchar(36) not null references public.erp_quality_defects(id) on delete restrict,
 description varchar(200) not null, stage_code varchar(40) not null,
 disposition varchar(12) not null check(disposition in ('DEFEITO','REFUGO')),
 unique(report_id,defect_id,stage_code)
);
create index erp_quality_reports_work_date on public.erp_quality_reports(work_order_id,observed_on);
create index erp_quality_issues_report on public.erp_quality_issues(report_id);
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000001','Água no assoalho traseiro','AGUA NO ASSOALHO TRASEIRO','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000002','Conector do painel solto / mau contato','CONECTOR DO PAINEL SOLTO / MAU CONTATO','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000003','Guarnição das portas com entrada de água','GUARNICAO DAS PORTAS COM ENTRADA DE AGUA','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000004','Bolhas de ar na colagem do para-brisa','BOLHAS DE AR NA COLAGEM DO PARA-BRISA','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000005','Ruído interno no chicote elétrico da porta','RUIDO INTERNO NO CHICOTE ELETRICO DA PORTA','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000006','Tecido do banco enrugado ou desalinhado','TECIDO DO BANCO ENRUGADO OU DESALINHADO','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000007','Falta de estanqueidade / Vazamento de gás','FALTA DE ESTANQUEIDADE / VAZAMENTO DE GAS','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000008','Vazamento de água no vidro lateral','VAZAMENTO DE AGUA NO VIDRO LATERAL','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000009','Trilho do banco dianteiro travando','TRILHO DO BANCO DIANTEIRO TRAVANDO','SISTEMA',now());
insert into public.erp_quality_defects values ('00000000-0000-4000-8000-000000000010','Display do ar condicionado sem iluminação','DISPLAY DO AR CONDICIONADO SEM ILUMINACAO','SISTEMA',now());

insert into public.erp_roles(code,name,description,active,created_at,updated_at)
 values ('QUALIDADE','Qualidade','Apontamentos e indicadores FTT do MES.',true,now(),now())
 on conflict(code) do update set name=excluded.name,description=excluded.description,active=true,updated_at=now();
insert into public.erp_permissions(code,module,description,created_at) values
 ('mes.quality.read','MES','Consultar FTT e apontamentos de qualidade.',now()),
 ('mes.quality.write','MES','Registrar defeitos e apontamentos de qualidade.',now())
 on conflict(code) do nothing;
insert into public.erp_role_permissions(role_code,permission_code)
 select 'QUALIDADE',code from public.erp_permissions
 where code in ('mes.dashboard.read','mes.quality.read','mes.quality.write') on conflict do nothing;
insert into public.erp_role_permissions(role_code,permission_code)
 select r.code,p.code from public.erp_roles r cross join public.erp_permissions p
 where r.code in ('ADMIN','ADM') and p.code in ('mes.quality.read','mes.quality.write') on conflict do nothing;
alter table public.erp_quality_defects enable row level security;
alter table public.erp_quality_reports enable row level security;
alter table public.erp_quality_issues enable row level security;
revoke all on public.erp_quality_defects,public.erp_quality_reports,public.erp_quality_issues from public;
do $$
declare r text;
begin
 foreach r in array array['anon','authenticated'] loop
  if exists(select 1 from pg_roles where rolname=r) then
   execute format('revoke all on public.erp_quality_defects,public.erp_quality_reports,public.erp_quality_issues from %I',r);
  end if;
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant select,insert on public.erp_quality_defects,public.erp_quality_reports,public.erp_quality_issues to service_role;
 end if;
end $$;
commit;

