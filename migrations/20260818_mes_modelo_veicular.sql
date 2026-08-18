alter table public.erp_vehicle_entries
    add column if not exists modelo_veicular text;

comment on column public.erp_vehicle_entries.modelo_veicular is
    'Classificação operacional por ITEM: PACK, STANDART ou ORIGINAL. Não substitui o modelo físico de erp_vehicles.';

do $$
begin
    if not exists (
        select 1
          from pg_constraint
         where conrelid = 'public.erp_vehicle_entries'::regclass
           and conname = 'erp_vehicle_entries_modelo_veicular_check'
    ) then
        alter table public.erp_vehicle_entries
            add constraint erp_vehicle_entries_modelo_veicular_check
            check (modelo_veicular is null or modelo_veicular in ('PACK', 'STANDART', 'ORIGINAL'))
            not valid;
    end if;
end
$$;

alter table public.erp_vehicle_entries
    validate constraint erp_vehicle_entries_modelo_veicular_check;
