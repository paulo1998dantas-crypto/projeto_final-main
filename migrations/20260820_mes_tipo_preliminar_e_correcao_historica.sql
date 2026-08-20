alter table public.erp_vehicle_entries
    add column if not exists tipo_preliminar text;

update public.erp_vehicle_entries entry
   set tipo_preliminar = coalesce(
       (
           select work_order.tipo_servico
             from public.erp_work_orders work_order
            where work_order.vehicle_entry_id = entry.id
            order by work_order.is_current desc,
                     work_order.revision_number desc,
                     work_order.created_at desc
            limit 1
       ),
       'TRANSFORMAÇÃO'
   )
 where tipo_preliminar is null
    or btrim(tipo_preliminar) = '';

alter table public.erp_vehicle_entries
    alter column tipo_preliminar set default 'TRANSFORMAÇÃO',
    alter column tipo_preliminar set not null;

comment on column public.erp_vehicle_entries.tipo_preliminar is
    'Tipo operacional informado na entrada. Ao emitir uma O.S., o valor inicializa erp_work_orders.tipo_servico; correções posteriores da O.S. são explícitas e auditadas.';

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conrelid = 'public.erp_vehicle_entries'::regclass
           and conname = 'erp_vehicle_entries_tipo_preliminar_check'
    ) then
        alter table public.erp_vehicle_entries
            add constraint erp_vehicle_entries_tipo_preliminar_check
            check (
                tipo_preliminar in (
                    'TRANSFORMAÇÃO',
                    'PÓS-VENDA',
                    'INSTALAÇÃO_DE_ACESSÓRIO',
                    'RETORNO',
                    'OUTRO'
                )
            ) not valid;
    end if;
end
$$;

alter table public.erp_vehicle_entries
    validate constraint erp_vehicle_entries_tipo_preliminar_check;
