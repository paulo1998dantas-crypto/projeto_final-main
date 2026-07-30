-- Sequenciamento operacional persistente do MES.
-- Aditiva: não altera status, datas, saldos, histórico, etapas concluídas ou
-- os campos *_legacy trazidos das planilhas. Rollback seguro: remover somente
-- as duas tabelas e a coluna sequencia_planejada depois de desligar o código.
begin;

create table if not exists public.erp_sequence_profiles (
    id uuid primary key default gen_random_uuid(),
    nome text not null,
    criterios jsonb not null,
    ativo boolean not null default false,
    created_by text not null default '',
    updated_by text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint erp_sequence_profiles_nome_unique unique (nome),
    constraint erp_sequence_profiles_criterios_array check (jsonb_typeof(criterios) = 'array')
);

create unique index if not exists erp_sequence_profiles_one_active_idx
    on public.erp_sequence_profiles ((ativo)) where ativo;

create table if not exists public.erp_work_order_sequences (
    work_order_id uuid primary key references public.erp_work_orders(id) on delete restrict,
    profile_id uuid null references public.erp_sequence_profiles(id) on delete set null,
    data_entrega_vigente date null,
    semana_planejada text null,
    sequencia integer null check (sequencia is null or sequencia > 0),
    prioridade_manual integer null check (prioridade_manual between 0 and 999999),
    ativo boolean not null default true,
    updated_by text not null default '',
    updated_at timestamptz not null default now()
);

create index if not exists erp_work_order_sequences_wip_idx
    on public.erp_work_order_sequences (ativo, sequencia, data_entrega_vigente);

alter table public.erp_work_order_stages
    add column if not exists sequencia_planejada integer null;

-- O backend acessa essas tabelas pela conexão privada do serviço. Elas não são
-- uma API pública; RLS e revogação explícita evitam exposição acidental no Data API.
alter table public.erp_sequence_profiles enable row level security;
alter table public.erp_work_order_sequences enable row level security;
revoke all on table public.erp_sequence_profiles from anon, authenticated;
revoke all on table public.erp_work_order_sequences from anon, authenticated;

insert into public.erp_sequence_profiles (nome,criterios,ativo,created_by,updated_by)
values (
    'Prazo de entrega',
    '[
      {"field":"delivery_date","direction":"ASC"},
      {"field":"manual_priority","direction":"ASC"},
      {"field":"line","direction":"ASC"},
      {"field":"item_number","direction":"ASC"}
    ]'::jsonb,
    false,'MIGRATION','MIGRATION'
)
on conflict (nome) do nothing;

update public.erp_sequence_profiles
   set ativo=true,updated_by='MIGRATION',updated_at=now()
 where nome='Prazo de entrega'
   and not exists (select 1 from public.erp_sequence_profiles where ativo=true);

-- Backfill somente do WIP atual. Registros fechados não são reclassificados.
with active_profile as (
    select id from public.erp_sequence_profiles where ativo=true limit 1
), ranked as (
    select w.id as work_order_id,
           w.data_comercial_prevista as data_entrega_vigente,
           row_number() over (
             order by w.data_comercial_prevista nulls last,
                      e.item_number,
                      w.id
           )::integer as sequencia
      from public.erp_work_orders w
      join public.erp_vehicle_entries e on e.id=w.vehicle_entry_id
     where w.status in ('ATIVA','EM_PRODUÇÃO')
)
insert into public.erp_work_order_sequences (
    work_order_id,profile_id,data_entrega_vigente,semana_planejada,
    sequencia,ativo,updated_by,updated_at
)
select ranked.work_order_id,active_profile.id,ranked.data_entrega_vigente,
       case when ranked.data_entrega_vigente is null then null
            else to_char(ranked.data_entrega_vigente,'IW') end,
       ranked.sequencia,true,'MIGRATION',now()
  from ranked cross join active_profile
on conflict (work_order_id) do update set
    profile_id=excluded.profile_id,
    data_entrega_vigente=excluded.data_entrega_vigente,
    semana_planejada=excluded.semana_planejada,
    sequencia=excluded.sequencia,
    ativo=true,
    updated_by=excluded.updated_by,
    updated_at=excluded.updated_at;

update public.erp_work_order_stages stage
   set data_planejada=seq.data_entrega_vigente,
       semana_planejada=seq.semana_planejada,
       sequencia_planejada=seq.sequencia
  from public.erp_work_order_sequences seq
 where stage.work_order_id=seq.work_order_id
   and seq.ativo=true;

commit;
