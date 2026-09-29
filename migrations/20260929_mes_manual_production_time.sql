-- Tempos informados manualmente no controle de produção do MES.
-- Aditivo: não altera status, sessões, início/fim, observações ou cálculos.
begin;

alter table public.erp_work_order_stages
    add column if not exists setup_time_hours numeric(10,2)
        check (setup_time_hours is null or setup_time_hours >= 0),
    add column if not exists production_time_hours numeric(10,2)
        check (production_time_hours is null or production_time_hours >= 0);

alter table public.erp_work_order_stage_events
    add column if not exists setup_time_hours numeric(10,2)
        check (setup_time_hours is null or setup_time_hours >= 0),
    add column if not exists production_time_hours numeric(10,2)
        check (production_time_hours is null or production_time_hours >= 0);

alter table public.erp_vehicle_entry_stages
    add column if not exists setup_time_hours numeric(10,2)
        check (setup_time_hours is null or setup_time_hours >= 0),
    add column if not exists production_time_hours numeric(10,2)
        check (production_time_hours is null or production_time_hours >= 0);

alter table public.erp_vehicle_entry_stage_events
    add column if not exists setup_time_hours numeric(10,2)
        check (setup_time_hours is null or setup_time_hours >= 0),
    add column if not exists production_time_hours numeric(10,2)
        check (production_time_hours is null or production_time_hours >= 0);

comment on column public.erp_work_order_stages.setup_time_hours is
    'Tempo de setup informado manualmente em horas decimais.';
comment on column public.erp_work_order_stages.production_time_hours is
    'Tempo de produção informado manualmente em horas decimais; não altera o cálculo de sessões.';
comment on column public.erp_work_order_stage_events.setup_time_hours is
    'Snapshot do tempo de setup manual informado no evento, em horas decimais.';
comment on column public.erp_work_order_stage_events.production_time_hours is
    'Snapshot do tempo de produção manual informado no evento, em horas decimais.';
comment on column public.erp_vehicle_entry_stages.setup_time_hours is
    'Tempo de setup informado manualmente antes da abertura da O.S., em horas decimais.';
comment on column public.erp_vehicle_entry_stages.production_time_hours is
    'Tempo de produção informado manualmente antes da abertura da O.S., em horas decimais.';
comment on column public.erp_vehicle_entry_stage_events.setup_time_hours is
    'Snapshot do tempo de setup manual informado no evento pré-O.S., em horas decimais.';
comment on column public.erp_vehicle_entry_stage_events.production_time_hours is
    'Snapshot do tempo de produção manual informado no evento pré-O.S., em horas decimais.';
comment on column public.erp_work_order_stages.total_stopped_time_hours is
    'Campo legado mantido por compatibilidade; a interface atual usa setup e tempo de produção.';
comment on column public.erp_work_order_stage_events.total_stopped_time_hours is
    'Campo legado mantido por compatibilidade; a interface atual usa setup e tempo de produção.';

commit;
