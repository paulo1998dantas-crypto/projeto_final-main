-- Tempos complementares informados manualmente no card do MES.
-- Nao substituem nem alteram as sessoes/paradas automaticas, status,
-- inicio, termino ou observacoes existentes.
begin;

alter table public.erp_work_order_stages
    add column if not exists setup_time_hours numeric(10,2)
        check (setup_time_hours is null or setup_time_hours >= 0),
    add column if not exists total_stopped_time_hours numeric(10,2)
        check (total_stopped_time_hours is null or total_stopped_time_hours >= 0);

alter table public.erp_work_order_stage_events
    add column if not exists setup_time_hours numeric(10,2)
        check (setup_time_hours is null or setup_time_hours >= 0),
    add column if not exists total_stopped_time_hours numeric(10,2)
        check (total_stopped_time_hours is null or total_stopped_time_hours >= 0);

comment on column public.erp_work_order_stages.setup_time_hours is
    'Tempo de setup digitado manualmente em horas decimais; nao participa dos calculos de apontamento.';
comment on column public.erp_work_order_stages.total_stopped_time_hours is
    'Tempo parado total digitado manualmente em horas decimais; independente das pausas calculadas.';
comment on column public.erp_work_order_stage_events.setup_time_hours is
    'Snapshot do tempo de setup informado manualmente no evento, em horas decimais.';
comment on column public.erp_work_order_stage_events.total_stopped_time_hours is
    'Snapshot do tempo parado total informado manualmente no evento, em horas decimais.';

commit;
