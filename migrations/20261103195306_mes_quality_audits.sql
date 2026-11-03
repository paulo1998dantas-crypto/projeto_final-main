-- MES only: no production pointing, stock, BI view or past quality verdict is changed.
begin;
set local lock_timeout='5s';
alter table public.erp_quality_reports add column audit_complete boolean not null default false;
alter table public.erp_quality_reports add column version integer not null default 1 check(version>0);
create table public.erp_quality_report_areas (
 report_id varchar(36) not null references public.erp_quality_reports(id) on delete restrict,
 stage_code varchar(40) not null check(stage_code<>'LIBERAÇÃO'),
 primary key(report_id,stage_code)
);
create table public.erp_quality_corrections (
 id varchar(36) primary key,
 report_id varchar(36) not null references public.erp_quality_reports(id) on delete restrict,
 issue_id varchar(36) not null references public.erp_quality_issues(id) on delete restrict,
 from_stage varchar(40) not null, to_stage varchar(40) not null,
 reason varchar(500) not null check(length(trim(reason)) between 5 and 500),
 actor_id integer not null references public.users(id) on delete restrict,
 actor varchar(100) not null, created_at timestamptz not null default now(),
 version integer not null check(version>1),
 unique(report_id,version), check(from_stage<>to_stage)
);
create index erp_quality_reports_day on public.erp_quality_reports(observed_on,created_at);
create index erp_quality_corrections_report on public.erp_quality_corrections(report_id,created_at);
create index erp_quality_corrections_issue on public.erp_quality_corrections(issue_id);
alter table public.erp_quality_report_areas enable row level security;
alter table public.erp_quality_corrections enable row level security;
revoke all on public.erp_quality_report_areas,public.erp_quality_corrections from public;
do $$
declare r text;
begin
 foreach r in array array['anon','authenticated','service_role'] loop
  if exists(select 1 from pg_roles where rolname=r) then
   execute format('revoke all on public.erp_quality_report_areas,public.erp_quality_corrections from %I',r);
  end if;
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant select,insert on public.erp_quality_report_areas,public.erp_quality_corrections to service_role;
  grant update(version) on public.erp_quality_reports to service_role;
  grant update(stage_code) on public.erp_quality_issues to service_role;
 end if;
end $$;
-- Existing records stay defect-only. No inferred approvals or backfilled audit dates.
commit;
