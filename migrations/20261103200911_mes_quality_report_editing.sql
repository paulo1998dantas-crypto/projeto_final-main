-- Quality-only editing and logical exclusion with immutable before/after evidence.
begin;
set local lock_timeout='5s';
alter table public.erp_quality_reports add column voided boolean not null default false;
alter table public.erp_quality_issues add column active boolean not null default true;
do $$
declare c record;
begin
 for c in select conname from pg_constraint
  where conrelid='public.erp_quality_issues'::regclass and contype='u'
   and conkey=(select array_agg(attnum::smallint order by array_position(array['report_id','defect_id','stage_code'],attname::text))
               from pg_attribute where attrelid='public.erp_quality_issues'::regclass and attname in ('report_id','defect_id','stage_code'))
 loop
  execute format('alter table public.erp_quality_issues drop constraint %I',c.conname);
 end loop;
end $$;
create unique index erp_quality_issues_active_unique on public.erp_quality_issues(report_id,defect_id,stage_code) where active;
create table public.erp_quality_report_events (
 id varchar(36) primary key,
 report_id varchar(36) not null references public.erp_quality_reports(id) on delete restrict,
 action varchar(12) not null check(action in ('EDIT','DELETE')),
 before_data jsonb not null, after_data jsonb not null,
 reason varchar(500) not null check(length(trim(reason)) between 5 and 500),
 actor_id integer not null references public.users(id) on delete restrict,
 actor varchar(100) not null, created_at timestamptz not null default now(),
 version integer not null check(version>1), unique(report_id,version)
);
create index erp_quality_report_events_report on public.erp_quality_report_events(report_id,created_at);
alter table public.erp_quality_report_events enable row level security;
revoke all on public.erp_quality_report_events from public;
do $$
declare r text;
begin
 foreach r in array array['anon','authenticated','service_role'] loop
  if exists(select 1 from pg_roles where rolname=r) then
   execute format('revoke all on public.erp_quality_report_events from %I',r);
  end if;
 end loop;
 if exists(select 1 from pg_roles where rolname='service_role') then
  grant select,insert on public.erp_quality_report_events to service_role;
  grant update(observed_on,audit_complete,voided) on public.erp_quality_reports to service_role;
  grant update(active,defect_id,description,disposition) on public.erp_quality_issues to service_role;
 end if;
end $$;
commit;
