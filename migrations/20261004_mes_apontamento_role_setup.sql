-- Perfil aditivo de apontamento; preserva todos os papeis e sessoes existentes.
begin;

insert into public.erp_roles (code,name,description,active,created_at,updated_at)
values ('APONTAMENTO','Apontamento','Apontamento operacional por setor no MES.',true,now(),now())
on conflict (code) do update set name=excluded.name,description=excluded.description,
    active=true,updated_at=now();

insert into public.erp_permissions (code,module,description,created_at)
values ('mes.dashboard.read','MES','Consultar cards de apontamento.',now()),
       ('mes.stage.write','MES','Registrar etapas produtivas.',now())
on conflict (code) do nothing;

insert into public.erp_role_permissions (role_code,permission_code)
values ('APONTAMENTO','mes.dashboard.read'),('APONTAMENTO','mes.stage.write')
on conflict do nothing;

create table if not exists public.erp_stage_setup_sessions (
    id uuid primary key default gen_random_uuid(),
    work_order_stage_id uuid references public.erp_work_order_stages(id) on delete restrict,
    vehicle_entry_stage_id uuid references public.erp_vehicle_entry_stages(id) on delete restrict,
    started_at timestamptz not null,
    ended_at timestamptz,
    duration_seconds bigint check (duration_seconds is null or duration_seconds >= 0),
    started_by text not null,
    ended_by text,
    idempotency_key text unique,
    created_at timestamptz not null default now(),
    constraint erp_stage_setup_sessions_single_stage_ck check (
        (work_order_stage_id is not null and vehicle_entry_stage_id is null) or
        (work_order_stage_id is null and vehicle_entry_stage_id is not null)
    )
);

create unique index if not exists erp_stage_setup_sessions_one_open_work_idx
    on public.erp_stage_setup_sessions(work_order_stage_id)
    where work_order_stage_id is not null and ended_at is null;
create unique index if not exists erp_stage_setup_sessions_one_open_entry_idx
    on public.erp_stage_setup_sessions(vehicle_entry_stage_id)
    where vehicle_entry_stage_id is not null and ended_at is null;

alter table public.erp_stage_setup_sessions enable row level security;

-- Guarda segundos exatos; o campo em horas do MES recebe somente a diferença
-- entre os totais arredondados, evitando deriva em muitas sessões curtas.
create table if not exists public.erp_stage_auto_time_counters (
    id uuid primary key default gen_random_uuid(),
    work_order_stage_id uuid unique references public.erp_work_order_stages(id) on delete restrict,
    vehicle_entry_stage_id uuid unique references public.erp_vehicle_entry_stages(id) on delete restrict,
    production_seconds bigint not null default 0 check (production_seconds >= 0),
    setup_seconds bigint not null default 0 check (setup_seconds >= 0),
    stopped_seconds bigint not null default 0 check (stopped_seconds >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint erp_stage_auto_time_counters_single_stage_ck check (
        (work_order_stage_id is not null and vehicle_entry_stage_id is null) or
        (work_order_stage_id is null and vehicle_entry_stage_id is not null)
    )
);
alter table public.erp_stage_auto_time_counters enable row level security;
do $$
begin
    if exists(select 1 from pg_roles where rolname='anon') then
        execute 'revoke all on table public.erp_stage_setup_sessions from anon';
        execute 'revoke all on table public.erp_stage_auto_time_counters from anon';
    end if;
    if exists(select 1 from pg_roles where rolname='authenticated') then
        execute 'revoke all on table public.erp_stage_setup_sessions from authenticated';
        execute 'revoke all on table public.erp_stage_auto_time_counters from authenticated';
    end if;
    if exists(select 1 from pg_roles where rolname='service_role') then
        execute 'grant select,insert,update on table public.erp_stage_setup_sessions to service_role';
        execute 'grant select,insert,update on table public.erp_stage_auto_time_counters to service_role';
    end if;
end
$$;

commit;
