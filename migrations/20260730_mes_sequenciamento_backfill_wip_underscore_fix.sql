-- O status operacional canônico contém sublinhado: EM_PRODUÇÃO.
-- Reexecutável e restrita às novas tabelas de sequenciamento.
begin;

update public.erp_work_order_sequences
   set ativo=false,updated_by='MIGRATION_FIX',updated_at=now()
 where ativo=true;

with active_profile as (
    select id from public.erp_sequence_profiles where ativo=true limit 1
), ranked as (
    select w.id as work_order_id,
           w.data_comercial_prevista as data_entrega_vigente,
           row_number() over (
             order by w.data_comercial_prevista nulls last,e.item_number,w.id
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
       ranked.sequencia,true,'MIGRATION_FIX',now()
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
