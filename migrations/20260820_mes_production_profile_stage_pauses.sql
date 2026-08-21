-- Perfil operacional de Producao e registro de sessoes/paradas de etapa.
-- Migration aditiva e idempotente: nao altera apontamentos, O.S. ou saldos existentes.
begin;

insert into public.erp_roles (
    code, name, description, active, created_at, updated_at
)
values (
    'PRODUCAO',
    'Producao',
    'Apontamento simplificado das etapas produtivas no MES.',
    true,
    now(),
    now()
)
on conflict (code) do update
set name = excluded.name,
    description = excluded.description,
    active = true,
    updated_at = now();

insert into public.erp_permissions (code, module, description, created_at)
values
    (
        'mes.dashboard.read',
        'MES',
        'Consultar paineis e cards produtivos do MES.',
        now()
    ),
    (
        'mes.stage.write',
        'MES',
        'Registrar apontamentos em etapas produtivas do MES.',
        now()
    )
on conflict (code) do nothing;

insert into public.erp_role_permissions (role_code, permission_code)
values
    ('PRODUCAO', 'mes.dashboard.read'),
    ('PRODUCAO', 'mes.stage.write')
on conflict do nothing;

create table if not exists public.erp_stage_time_pauses (
    id uuid primary key default gen_random_uuid(),
    work_order_stage_id uuid
        references public.erp_work_order_stages(id) on delete restrict,
    vehicle_entry_stage_id uuid
        references public.erp_vehicle_entry_stages(id) on delete restrict,
    pause_type text not null
        check (pause_type in ('PARADA', 'INTERRUPCAO')),
    started_at timestamptz not null,
    ended_at timestamptz,
    duration_seconds bigint,
    started_by text not null,
    ended_by text,
    reason text not null default '',
    idempotency_key text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint erp_stage_time_pauses_single_stage_ck check (
        (work_order_stage_id is not null and vehicle_entry_stage_id is null)
        or
        (work_order_stage_id is null and vehicle_entry_stage_id is not null)
    ),
    constraint erp_stage_time_pauses_duration_ck check (
        duration_seconds is null or duration_seconds >= 0
    )
);

create index if not exists erp_stage_time_pauses_work_stage_idx
    on public.erp_stage_time_pauses(work_order_stage_id, started_at desc)
    where work_order_stage_id is not null;

create index if not exists erp_stage_time_pauses_entry_stage_idx
    on public.erp_stage_time_pauses(vehicle_entry_stage_id, started_at desc)
    where vehicle_entry_stage_id is not null;

create unique index if not exists erp_stage_time_pauses_idempotency_idx
    on public.erp_stage_time_pauses(idempotency_key)
    where idempotency_key is not null;

create unique index if not exists erp_stage_time_pauses_one_open_work_idx
    on public.erp_stage_time_pauses(work_order_stage_id)
    where work_order_stage_id is not null and ended_at is null;

create unique index if not exists erp_stage_time_pauses_one_open_entry_idx
    on public.erp_stage_time_pauses(vehicle_entry_stage_id)
    where vehicle_entry_stage_id is not null and ended_at is null;

alter table public.erp_stage_time_pauses enable row level security;
do $$
begin
    if exists(select 1 from pg_roles where rolname='anon') then
        execute 'revoke all on table public.erp_stage_time_pauses from anon';
    end if;
    if exists(select 1 from pg_roles where rolname='authenticated') then
        execute 'revoke all on table public.erp_stage_time_pauses from authenticated';
    end if;
    if exists(select 1 from pg_roles where rolname='service_role') then
        execute 'grant select, insert, update on table public.erp_stage_time_pauses to service_role';
    end if;
end
$$;

comment on table public.erp_stage_time_pauses is
    'Intervalos de parada ou interrupcao de etapas produtivas; preservados ao promover apontamentos pre-O.S.';

create table if not exists public.erp_stage_time_sessions (
    id uuid primary key default gen_random_uuid(),
    work_order_stage_id uuid
        references public.erp_work_order_stages(id) on delete restrict,
    vehicle_entry_stage_id uuid
        references public.erp_vehicle_entry_stages(id) on delete restrict,
    started_at timestamptz not null,
    ended_at timestamptz,
    productive_seconds bigint,
    started_by text not null,
    ended_by text,
    observation text not null default '',
    idempotency_key text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint erp_stage_time_sessions_single_stage_ck check (
        (work_order_stage_id is not null and vehicle_entry_stage_id is null)
        or
        (work_order_stage_id is null and vehicle_entry_stage_id is not null)
    ),
    constraint erp_stage_time_sessions_duration_ck check (
        productive_seconds is null or productive_seconds >= 0
    )
);

create index if not exists erp_stage_time_sessions_work_stage_idx
    on public.erp_stage_time_sessions(work_order_stage_id, started_at)
    where work_order_stage_id is not null;

create index if not exists erp_stage_time_sessions_entry_stage_idx
    on public.erp_stage_time_sessions(vehicle_entry_stage_id, started_at)
    where vehicle_entry_stage_id is not null;

create unique index if not exists erp_stage_time_sessions_idempotency_idx
    on public.erp_stage_time_sessions(idempotency_key)
    where idempotency_key is not null;

create unique index if not exists erp_stage_time_sessions_one_open_work_idx
    on public.erp_stage_time_sessions(work_order_stage_id)
    where work_order_stage_id is not null and ended_at is null;

create unique index if not exists erp_stage_time_sessions_one_open_entry_idx
    on public.erp_stage_time_sessions(vehicle_entry_stage_id)
    where vehicle_entry_stage_id is not null and ended_at is null;

alter table public.erp_stage_time_sessions enable row level security;
do $$
begin
    if exists(select 1 from pg_roles where rolname='anon') then
        execute 'revoke all on table public.erp_stage_time_sessions from anon';
    end if;
    if exists(select 1 from pg_roles where rolname='authenticated') then
        execute 'revoke all on table public.erp_stage_time_sessions from authenticated';
    end if;
    if exists(select 1 from pg_roles where rolname='service_role') then
        execute 'grant select, insert, update on table public.erp_stage_time_sessions to service_role';
    end if;
end
$$;

comment on table public.erp_stage_time_sessions is
    'Sessoes produtivas aditivas por ITEM/O.S./etapa. O total produtivo e a soma de productive_seconds, sem incluir pausas.';

commit;
