-- Align both pointing consoles without changing manual historical totals.
-- Additive/idempotent: retain intervals, stage status, observations and dates.
begin;
set local lock_timeout = '5s';
set local statement_timeout = '30s';

create temporary table shared_sync_stages on commit drop as
select 'work'::text as kind,s.id,to_jsonb(s) as before_data
from public.erp_work_order_stages s
where exists(select 1 from public.erp_stage_time_sessions t where t.work_order_stage_id=s.id)
   or exists(select 1 from public.erp_stage_setup_sessions t where t.work_order_stage_id=s.id)
   or exists(select 1 from public.erp_stage_time_pauses t where t.work_order_stage_id=s.id)
union all
select 'entry',s.id,to_jsonb(s) from public.erp_vehicle_entry_stages s
where exists(select 1 from public.erp_stage_time_sessions t where t.vehicle_entry_stage_id=s.id)
   or exists(select 1 from public.erp_stage_setup_sessions t where t.vehicle_entry_stage_id=s.id)
   or exists(select 1 from public.erp_stage_time_pauses t where t.vehicle_entry_stage_id=s.id);

-- Application commands lock the canonical stage first as well.
select s.id from public.erp_work_order_stages s join shared_sync_stages t on t.id=s.id and t.kind='work' order by s.id for update of s;
select s.id from public.erp_vehicle_entry_stages s join shared_sync_stages t on t.id=s.id and t.kind='entry' order by s.id for update of s;
update shared_sync_stages t set before_data=to_jsonb(s) from public.erp_work_order_stages s where t.id=s.id and t.kind='work';
update shared_sync_stages t set before_data=to_jsonb(s) from public.erp_vehicle_entry_stages s where t.id=s.id and t.kind='entry';

do $$
declare tbl text; duration_field text;
begin
    foreach tbl in array array['erp_stage_time_sessions','erp_stage_setup_sessions','erp_stage_time_pauses'] loop
        execute format('update public.%I set auto_time_fields=true where not auto_time_fields',tbl);
        -- A manually concluded/cancelled stage wins over an old open timer.
        -- Do not invent elapsed historical hours; keep the timer as evidence.
        duration_field := case when tbl='erp_stage_time_sessions' then 'productive_seconds' else 'duration_seconds' end;
        execute format($sql$
            update public.%I i set ended_at=greatest(i.started_at,coalesce((t.before_data->>'termino')::timestamptz,now())),
                ended_by='SINCRONIZACAO MES',%I=0,superseded_at=now()
            from shared_sync_stages t
            where t.id=coalesce(i.work_order_stage_id,i.vehicle_entry_stage_id)
              and i.ended_at is null and t.before_data->>'status'<>'EM_ANDAMENTO'
        $sql$,tbl,duration_field);
    end loop;
end $$;

create temporary table shared_sync_intervals on commit drop as
select work_order_stage_id,vehicle_entry_stage_id,started_at,execution_operator,
    coalesce(productive_seconds,0) as production_seconds,0::bigint as setup_seconds,0::bigint as stopped_seconds
from public.erp_stage_time_sessions where superseded_at is null
union all
select work_order_stage_id,vehicle_entry_stage_id,started_at,execution_operator,0,coalesce(duration_seconds,0),0
from public.erp_stage_setup_sessions where superseded_at is null
union all
select work_order_stage_id,vehicle_entry_stage_id,started_at,execution_operator,0,0,coalesce(duration_seconds,0)
from public.erp_stage_time_pauses where superseded_at is null;

create temporary table shared_sync_totals on commit drop as
select t.kind,t.id,t.before_data,
    coalesce(sum(i.production_seconds),0)::bigint as production_seconds,
    coalesce(sum(i.setup_seconds),0)::bigint as setup_seconds,
    coalesce(sum(i.stopped_seconds),0)::bigint as stopped_seconds,
    (select string_agg(name,' / ' order by priority,first_seen,name) from (
        select distinct on (lower(trim(name))) trim(name) as name,priority,first_seen from (
            select name,0 as priority,to_timestamp(ordinality::double precision) as first_seen
            from regexp_split_to_table(coalesce(t.before_data->>'responsavel',''),'/') with ordinality as n(name,ordinality)
            union all
            select execution_operator,1,started_at from shared_sync_intervals x
            where coalesce(x.work_order_stage_id,x.vehicle_entry_stage_id)=t.id
        ) n where nullif(trim(name),'') is not null
        order by lower(trim(name)),priority,first_seen
    ) names) as operators
from shared_sync_stages t left join shared_sync_intervals i on coalesce(i.work_order_stage_id,i.vehicle_entry_stage_id)=t.id
group by t.kind,t.id,t.before_data;

-- Seed exact counters, but never add historical seconds twice to manual hours.
insert into public.erp_stage_auto_time_counters(work_order_stage_id,production_seconds,setup_seconds,stopped_seconds)
select id,production_seconds,setup_seconds,stopped_seconds from shared_sync_totals where kind='work'
on conflict(work_order_stage_id) do update set production_seconds=excluded.production_seconds,
    setup_seconds=excluded.setup_seconds,stopped_seconds=excluded.stopped_seconds,updated_at=now();
insert into public.erp_stage_auto_time_counters(vehicle_entry_stage_id,production_seconds,setup_seconds,stopped_seconds)
select id,production_seconds,setup_seconds,stopped_seconds from shared_sync_totals where kind='entry'
on conflict(vehicle_entry_stage_id) do update set production_seconds=excluded.production_seconds,
    setup_seconds=excluded.setup_seconds,stopped_seconds=excluded.stopped_seconds,updated_at=now();

do $$
declare tbl text; kind_name text; event_tbl text; fk text;
begin
    foreach kind_name in array array['work','entry'] loop
        tbl := case when kind_name='work' then 'erp_work_order_stages' else 'erp_vehicle_entry_stages' end;
        event_tbl := case when kind_name='work' then 'erp_work_order_stage_events' else 'erp_vehicle_entry_stage_events' end;
        fk := case when kind_name='work' then 'work_order_stage_id' else 'vehicle_entry_stage_id' end;
        execute format($sql$
            update public.%I s set responsavel=coalesce(t.operators,s.responsavel),
                production_time_hours=coalesce(s.production_time_hours,case when t.production_seconds>0 then round(t.production_seconds/3600.0,2) end),
                setup_time_hours=coalesce(s.setup_time_hours,case when t.setup_seconds>0 then round(t.setup_seconds/3600.0,2) end),
                total_stopped_time_hours=coalesce(s.total_stopped_time_hours,case when t.stopped_seconds>0 then round(t.stopped_seconds/3600.0,2) end)
            from shared_sync_totals t where t.id=s.id and t.kind=%L
        $sql$,tbl,kind_name);
        execute format($sql$
            insert into public.%I(%I,action,status_anterior,novo_status,operador,inicio,termino,localizacao,
                observacao,setup_time_hours,production_time_hours,total_stopped_time_hours,idempotency_key)
            select s.id,'SINCRONIZACAO_APONTAMENTOS',s.status,s.status,'SINCRONIZACAO MES',s.inicio,s.termino,s.localizacao,
                'Consolidação entre telas MES. Valores anteriores: '||t.before_data::text,
                s.setup_time_hours,s.production_time_hours,s.total_stopped_time_hours,'shared-sync-20261005:'||s.id::text
            from public.%I s join shared_sync_totals t on t.id=s.id and t.kind=%L
            where to_jsonb(s) is distinct from t.before_data
              and not exists(select 1 from public.%I ev where ev.idempotency_key='shared-sync-20261005:'||s.id::text)
        $sql$,event_tbl,fk,tbl,kind_name,event_tbl);
    end loop;
end $$;

select count(*) as etapas_sincronizadas from shared_sync_totals;
commit;
