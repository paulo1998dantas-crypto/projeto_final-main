-- Tempo parado total digitado manualmente no MES.
-- Mantem esse valor separado das pausas calculadas por sessao e nao altera
-- status, inicio, termino, observacoes ou os calculos operacionais existentes.
begin;

alter table public.erp_work_order_stages
    add column if not exists total_stopped_time_hours numeric(10,2)
        check (total_stopped_time_hours is null or total_stopped_time_hours >= 0);

alter table public.erp_work_order_stage_events
    add column if not exists total_stopped_time_hours numeric(10,2)
        check (total_stopped_time_hours is null or total_stopped_time_hours >= 0);

alter table public.erp_vehicle_entry_stages
    add column if not exists total_stopped_time_hours numeric(10,2)
        check (total_stopped_time_hours is null or total_stopped_time_hours >= 0);

alter table public.erp_vehicle_entry_stage_events
    add column if not exists total_stopped_time_hours numeric(10,2)
        check (total_stopped_time_hours is null or total_stopped_time_hours >= 0);

comment on column public.erp_work_order_stages.total_stopped_time_hours is
    'Tempo parado total digitado manualmente em horas decimais; separado das pausas calculadas por sessão.';
comment on column public.erp_work_order_stage_events.total_stopped_time_hours is
    'Snapshot do tempo parado total informado manualmente no evento, em horas decimais.';
comment on column public.erp_vehicle_entry_stages.total_stopped_time_hours is
    'Tempo parado total digitado manualmente antes da abertura da O.S., em horas decimais.';
comment on column public.erp_vehicle_entry_stage_events.total_stopped_time_hours is
    'Snapshot do tempo parado total informado manualmente no evento pré-O.S., em horas decimais.';

commit;
