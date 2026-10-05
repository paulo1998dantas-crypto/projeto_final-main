-- Preserve operators per interval; cumulative stage fields remain authoritative.
-- No historical stage status, time, observation or operator is overwritten.
begin;

alter table public.erp_stage_time_sessions
    add column if not exists execution_operator text,
    add column if not exists auto_time_fields boolean not null default false,
    add column if not exists superseded_at timestamptz;
alter table public.erp_stage_setup_sessions
    add column if not exists execution_operator text,
    add column if not exists auto_time_fields boolean not null default true,
    add column if not exists superseded_at timestamptz;
alter table public.erp_stage_time_pauses
    add column if not exists execution_operator text,
    add column if not exists auto_time_fields boolean not null default false,
    add column if not exists superseded_at timestamptz,
    add column if not exists resume_phase text not null default 'PRODUCAO'
        check (resume_phase in ('SETUP','PRODUCAO'));

-- Recover the explicitly informed operator from the original command event.
-- Unknown historic execution names stay unknown, never replaced by today's user.
do $$
declare tbl text;
begin
    foreach tbl in array array['erp_stage_time_sessions','erp_stage_setup_sessions','erp_stage_time_pauses'] loop
        execute format($sql$
            update public.%I t set execution_operator=recovered.name,
                auto_time_fields=true
            from (
                select t.id,substring(ev.observacao from 'Operador informado: (.*)\. Registrado por:') as name
                from public.%I t
                join (
                    select work_order_stage_id as stage_id,idempotency_key,observacao from public.erp_work_order_stage_events
                    union all
                    select vehicle_entry_stage_id,idempotency_key,observacao from public.erp_vehicle_entry_stage_events
                ) ev on ev.stage_id=coalesce(t.work_order_stage_id,t.vehicle_entry_stage_id)
                    and ev.idempotency_key=regexp_replace(t.idempotency_key,':(session|setup|pause)$','')
                where ev.observacao like '%%Operador informado:%%'
            ) recovered where t.id=recovered.id and t.execution_operator is null
                and recovered.name is not null
        $sql$,tbl,tbl);
    end loop;
end $$;

update public.erp_stage_time_pauses p set resume_phase='SETUP'
where exists(select 1 from public.erp_stage_setup_sessions t
    where coalesce(t.work_order_stage_id,t.vehicle_entry_stage_id)=coalesce(p.work_order_stage_id,p.vehicle_entry_stage_id)
      and t.ended_at=p.started_at);

create or replace view bi.mes_execution_intervals with (security_barrier=true) as
with intervals as (
    select id,work_order_stage_id,vehicle_entry_stage_id,execution_operator,started_by,
        started_at,ended_at,productive_seconds as seconds,'PRODUCAO'::text as phase,auto_time_fields,superseded_at
    from public.erp_stage_time_sessions
    union all
    select id,work_order_stage_id,vehicle_entry_stage_id,execution_operator,started_by,
        started_at,ended_at,duration_seconds,'SETUP',auto_time_fields,superseded_at
    from public.erp_stage_setup_sessions
    union all
    select id,work_order_stage_id,vehicle_entry_stage_id,execution_operator,started_by,
        started_at,ended_at,duration_seconds,'PARADA',auto_time_fields,superseded_at
    from public.erp_stage_time_pauses
), stages as (
    select s.id as stage_id,s.work_order_id,w.numero_os,e.item_number,v.chassi,
        concat_ws(' ',v.marca,v.modelo,v.versao) as modelo,s.stage_code,s.status,
        s.inicio,s.termino,s.responsavel,s.localizacao,s.setup_time_hours,s.production_time_hours,s.total_stopped_time_hours
    from public.erp_work_order_stages s join public.erp_work_orders w on w.id=s.work_order_id
    join public.erp_vehicle_entries e on e.id=w.vehicle_entry_id join public.erp_vehicles v on v.id=e.vehicle_id
    where s.aplicavel
    union all
    select s.id,null::uuid,null,e.item_number,v.chassi,concat_ws(' ',v.marca,v.modelo,v.versao),
        s.stage_code,s.status,s.inicio,s.termino,s.responsavel,s.localizacao,
        s.setup_time_hours,s.production_time_hours,s.total_stopped_time_hours
    from public.erp_vehicle_entry_stages s join public.erp_vehicle_entries e on e.id=s.vehicle_entry_id
    join public.erp_vehicles v on v.id=e.vehicle_id
    where s.aplicavel and s.transferred_to_work_order_stage_id is null
), measured as (
    select t.*,s.stage_id,s.work_order_id,s.numero_os,s.item_number,s.chassi,s.modelo,s.stage_code,
        s.status,s.localizacao,s.termino as canonical_end,
        case t.phase when 'PRODUCAO' then s.production_time_hours when 'SETUP' then s.setup_time_hours
            else s.total_stopped_time_hours end as canonical_hours,
        sum(t.seconds) over(partition by s.stage_id,t.phase) as phase_seconds,
        sum(t.seconds) over(partition by s.stage_id,t.phase order by t.started_at,t.id) as cumulative_seconds,
        row_number() over(partition by s.stage_id order by t.ended_at desc,t.id desc) as latest_interval
    from intervals t join stages s on s.stage_id=coalesce(t.work_order_stage_id,t.vehicle_entry_stage_id)
    where t.auto_time_fields and t.superseded_at is null and t.ended_at is not null
), allocated as (
    select *,greatest(0,least(coalesce(canonical_hours,round(phase_seconds/3600.0,2)),round(phase_seconds/3600.0,2))) as allocated_hours
    from measured
)
select id as interval_id,stage_id,work_order_id,numero_os,item_number,chassi,modelo,stage_code,status,localizacao,
    coalesce(nullif(trim(execution_operator),''),'NÃO INFORMADO') as execution_operator,started_by as registered_by,
    phase,started_at,
    case when status='CONCLUÍDA' and latest_interval=1 and canonical_end is not null then canonical_end else ended_at end as ended_at,
    seconds,
    (case when phase_seconds=0 then 0 else
        round(allocated_hours*cumulative_seconds/phase_seconds,2)-
        round(allocated_hours*(cumulative_seconds-seconds)/phase_seconds,2) end)::numeric(10,2) as hours
from allocated;

-- Same BI view/name/first columns. Session rows + manual residual, never
-- repeated cumulative snapshots. Adjusted canonical totals always reconcile.
create or replace view bi.fato_fechamento_operador with (security_barrier=true) as
with automatic as (
    select interval_id as apontamento_id,work_order_id,numero_os,item_number as item,
        right(chassi,8) as chassi_exibicao,execution_operator as responsavel,stage_code as etapa,
        (ended_at at time zone 'America/Sao_Paulo')::date as data_fechamento,
        started_at as inicio,ended_at as termino,
        (case when phase='SETUP' then hours else 0 end)::numeric(10,2) as setup_time_hours,
        (case when phase='PRODUCAO' then hours else 0 end)::numeric(10,2) as production_time_hours,
        (case when phase='PARADA' then hours else 0 end)::numeric(10,2) as total_stopped_time_hours,
        stage_id as etapa_id,status as status_etapa,phase as fase,registered_by as registrado_por
    from bi.mes_execution_intervals
), totals as (
    select etapa_id,sum(setup_time_hours) as setup,sum(production_time_hours) as production,sum(total_stopped_time_hours) as stopped
    from automatic group by etapa_id
), manual as (
    select s.id as apontamento_id,s.work_order_id,w.numero_os,e.item_number as item,right(v.chassi,8) as chassi_exibicao,
        coalesce(nullif(trim(s.responsavel),''),'NÃO INFORMADO') as responsavel,s.stage_code as etapa,
        (s.termino at time zone 'America/Sao_Paulo')::date as data_fechamento,s.inicio,s.termino,
        greatest(0,coalesce(s.setup_time_hours,0)-coalesce(t.setup,0))::numeric(10,2) as setup_time_hours,
        greatest(0,coalesce(s.production_time_hours,0)-coalesce(t.production,0))::numeric(10,2) as production_time_hours,
        greatest(0,coalesce(s.total_stopped_time_hours,0)-coalesce(t.stopped,0))::numeric(10,2) as total_stopped_time_hours,
        s.id as etapa_id,s.status as status_etapa,'MANUAL / HISTÓRICO'::text as fase,null::text as registrado_por
    from public.erp_work_order_stages s join public.erp_work_orders w on w.id=s.work_order_id
    join public.erp_vehicle_entries e on e.id=w.vehicle_entry_id join public.erp_vehicles v on v.id=e.vehicle_id
    left join totals t on t.etapa_id=s.id
    where s.aplicavel and s.status='CONCLUÍDA' and s.termino is not null
), rows as (
    select * from automatic
    union all
    select * from manual m where m.setup_time_hours+m.production_time_hours+m.total_stopped_time_hours>0
        or not exists(select 1 from automatic a where a.etapa_id=m.etapa_id)
)
select * from rows;

comment on view bi.fato_fechamento_operador is
    'Sessões somáveis por operador e fase + saldo manual/histórico. Totais reconciliam com a etapa vigente; datas em Brasília. Não somar snapshots de eventos.';
-- Same existing BI audience; no access for anon/authenticated/public.
revoke all on bi.mes_execution_intervals from public,anon,authenticated;
grant select on bi.fato_fechamento_operador,bi.mes_execution_intervals to powerbi_reader;
grant select on bi.mes_execution_intervals to service_role;
commit;
