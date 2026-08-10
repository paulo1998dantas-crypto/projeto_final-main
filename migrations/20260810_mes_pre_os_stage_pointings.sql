-- Apontamentos produtivos realizados depois da entrada do veículo e antes da O.S.
-- A estrutura é aditiva: não altera etapas, eventos ou saldos já existentes.

create table if not exists public.erp_vehicle_entry_stages (
    id uuid primary key default gen_random_uuid(),
    vehicle_entry_id uuid not null references public.erp_vehicle_entries(id) on delete restrict,
    stage_code text not null,
    aplicavel boolean not null default true,
    status text not null default 'PENDENTE'
        check (status in ('PENDENTE', 'EM_ANDAMENTO', 'CONCLUÍDA', 'NÃO_APLICÁVEL')),
    ordem integer not null,
    responsavel text,
    localizacao text,
    inicio timestamptz,
    termino timestamptz,
    observacoes text not null default '',
    parametrizado boolean not null default false,
    version integer not null default 1,
    transferred_to_work_order_stage_id uuid
        references public.erp_work_order_stages(id) on delete set null,
    transferred_at timestamptz,
    transferred_by text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (vehicle_entry_id, stage_code)
);

create index if not exists erp_vehicle_entry_stages_entry_idx
    on public.erp_vehicle_entry_stages(vehicle_entry_id, ordem);

create table if not exists public.erp_vehicle_entry_stage_events (
    id uuid primary key default gen_random_uuid(),
    vehicle_entry_stage_id uuid not null
        references public.erp_vehicle_entry_stages(id) on delete restrict,
    action text not null,
    status_anterior text,
    novo_status text not null,
    operador text not null,
    inicio timestamptz,
    termino timestamptz,
    localizacao text,
    observacao text,
    idempotency_key text,
    transferred_to_event_id uuid
        references public.erp_work_order_stage_events(id) on delete set null,
    transferred_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists erp_vehicle_entry_stage_events_stage_idx
    on public.erp_vehicle_entry_stage_events(vehicle_entry_stage_id, created_at);

create unique index if not exists erp_vehicle_entry_stage_events_idempotency_idx
    on public.erp_vehicle_entry_stage_events(idempotency_key)
    where idempotency_key is not null;

alter table public.erp_vehicle_entry_stages enable row level security;
alter table public.erp_vehicle_entry_stage_events enable row level security;

revoke all on table public.erp_vehicle_entry_stages from anon, authenticated;
revoke all on table public.erp_vehicle_entry_stage_events from anon, authenticated;
grant select, insert, update on table public.erp_vehicle_entry_stages to service_role;
grant select, insert, update on table public.erp_vehicle_entry_stage_events to service_role;

comment on table public.erp_vehicle_entry_stages is
    'Estado produtivo preliminar do ITEM antes da abertura da O.S.; promovido transacionalmente para erp_work_order_stages.';
comment on table public.erp_vehicle_entry_stage_events is
    'Histórico imutável dos apontamentos preliminares feitos contra a entrada do veículo.';
