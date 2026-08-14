-- Rollback protegido: so restaura a unicidade antiga se nenhuma revisao extra
-- tiver sido criada. Nunca apaga O.S. para viabilizar a reversao.

begin;

do $$
begin
    if exists (
        select 1
        from public.erp_work_orders
        group by vehicle_entry_id
        having count(*) > 1
    ) or exists (
        select 1
        from public.erp_work_orders
        group by numero_os
        having count(*) > 1
    ) then
        raise exception 'Rollback bloqueado: existem revisoes de O.S. que precisam ser preservadas.';
    end if;
end $$;

drop index if exists public.ix_erp_work_orders_supersedes;
drop index if exists public.ux_erp_work_orders_number_revision;
drop index if exists public.ux_erp_work_orders_entry_revision;
drop index if exists public.ux_erp_work_orders_current_number;
drop index if exists public.ux_erp_work_orders_current_entry;

alter table public.erp_work_orders
    add constraint erp_work_orders_vehicle_entry_id_key unique(vehicle_entry_id),
    add constraint erp_work_orders_numero_os_key unique(numero_os);

alter table public.erp_work_orders
    drop constraint if exists erp_work_orders_supersedes_work_order_id_fkey,
    drop constraint if exists ck_erp_work_orders_revision_number_positive,
    drop column if exists supersedes_work_order_id,
    drop column if exists is_current,
    drop column if exists revision_number;

commit;
