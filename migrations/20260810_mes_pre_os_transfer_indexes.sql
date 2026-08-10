-- Índices das referências usadas na promoção idempotente do apontamento preliminar.
-- A migration é aditiva e não altera registros operacionais existentes.

create index if not exists erp_vehicle_entry_stages_transferred_stage_idx
    on public.erp_vehicle_entry_stages(transferred_to_work_order_stage_id)
    where transferred_to_work_order_stage_id is not null;

create index if not exists erp_vehicle_entry_stage_events_transferred_event_idx
    on public.erp_vehicle_entry_stage_events(transferred_to_event_id)
    where transferred_to_event_id is not null;
