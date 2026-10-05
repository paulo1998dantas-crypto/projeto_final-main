-- Canonical execution names and equal per-crew shares. Audit actors are untouched.
-- No hours, counters, timestamps, status or observations are changed.
begin;
set local lock_timeout='5s';
set local statement_timeout='45s';

create temporary table operator_original_stage_values on commit drop as
select id,to_jsonb(s)-'responsavel' as data from public.erp_work_order_stages s
union all select id,to_jsonb(s)-'responsavel' from public.erp_vehicle_entry_stages s;
create temporary table operator_original_totals on commit drop as
select sum(setup_time_hours) as setup,sum(production_time_hours) as production,
    sum(total_stopped_time_hours) as stopped from bi.fato_fechamento_operador;

create or replace function public.mes_operator_names(raw_name text) returns text[]
language plpgsql immutable parallel safe as $$
declare
  normalized text;
  chunk text;
  part text;
  matches text[];
  result text[] := array[]::text[];
  known text := '\m(EBER FAVACHO|ALMOXARIFADO|GRUPO EURO|CLEILTON|CLEILRON|GEOVANNY|AMARILDO|VINICIUS|GEOVANE|EVERTON|GEOVANY|CLEITON|RODRIGO|CARLOS|FELIPE|JUAREZ|RENATO|ROBERT|SAMUEL|SIDNEY|THIAGO|VICTOR|EVRTON|PAULO|VITOR|LUCAS|IGOR|JEAN|IAGO|JOSE|LUIZ|FRED|CM|JEN|WILIAN)\M';
  aliases jsonb := '{"CLEILTON":"CLEITON","CLEILRON":"CLEITON","GEOVANE":"GEOVANY","GEOVANNY":"GEOVANY","EVRTON":"EVERTON","JEN":"JEAN","WILIAN":"WILLIAN"}';
begin
  normalized := btrim(regexp_replace(translate(upper(coalesce(raw_name,'')),
      'ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ','AAAAAEEEEIIIIOOOOOUUUUC'),'\s+',' ','g'));
  if normalized='NAO INFORMADO' then return array['NÃO INFORMADO']; end if;
  foreach chunk in array regexp_split_to_array(normalized,'\s*[/,;+&|]\s*|\s+E\s+') loop
    if chunk='' then continue; end if;
    if btrim(regexp_replace(chunk,known,'','g'))='' then
      select array_agg(m[1]) into matches from regexp_matches(chunk,known,'g') m;
    else matches := array[chunk];
    end if;
    foreach part in array coalesce(matches,array[]::text[]) loop
      part := coalesce(aliases->>part,part);
      if not part=any(result) then result:=array_append(result,part); end if;
    end loop;
  end loop;
  return result;
end $$;

create or replace function public.mes_normalize_operators(raw_name text) returns text
language sql immutable parallel safe as $$
  select array_to_string(public.mes_operator_names(raw_name),' / ')
$$;

-- Preserve every old value in the existing audit table, including timer crews.
do $$
declare tbl text; name_field text; entity text;
begin
  foreach tbl in array array['erp_work_order_stages','erp_vehicle_entry_stages',
      'erp_stage_time_sessions','erp_stage_setup_sessions','erp_stage_time_pauses'] loop
    name_field:=case when tbl in ('erp_work_order_stages','erp_vehicle_entry_stages') then 'responsavel' else 'execution_operator' end;
    entity:=upper(tbl);
    execute format($q$
      with previous as materialized (
        select id,%I as old_name,public.mes_normalize_operators(%I) as new_name
        from public.%I where %I is not null and %I is distinct from public.mes_normalize_operators(%I)
        order by id for update
      ), changed as (
        update public.%I t set %I=p.new_name from previous p where t.id=p.id
        returning t.id,p.old_name,p.new_name
      )
      insert into public.erp_audit_events(entity_type,entity_id,action,actor,origin,before_data,after_data,reason)
      select %L,id,'NORMALIZACAO_OPERADORES','PAULO','MES',
        jsonb_build_object(%L,old_name),jsonb_build_object(%L,new_name),
        'Padronização solicitada por PAULO: referências de operadores, maiúsculas e separação por /. Horas e usuários registradores preservados.'
      from changed
    $q$,name_field,name_field,tbl,name_field,name_field,name_field,tbl,name_field,entity,name_field,name_field);
  end loop;
end $$;

create or replace view bi.mes_operator_allocations with (security_barrier=true) as
select i.interval_id,i.stage_id,i.work_order_id,i.numero_os,i.item_number,i.chassi,i.modelo,i.stage_code,i.status,i.localizacao,
    n.name as execution_operator,i.registered_by,i.phase,i.started_at,i.ended_at,i.seconds,
    (round(i.hours*n.ordinality/cardinality(crew.names),2)-
     round(i.hours*(n.ordinality-1)/cardinality(crew.names),2))::numeric(10,2) as hours,
    i.execution_operator as execution_crew,cardinality(crew.names) as operator_count,
    md5(i.interval_id::text||':'||n.name)::uuid as allocation_id
from bi.mes_execution_intervals i
cross join lateral (select public.mes_operator_names(i.execution_operator) as names) crew
cross join lateral unnest(crew.names) with ordinality n(name,ordinality);

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
select case when cardinality(crew.names)=1 then r.apontamento_id
            else md5(r.apontamento_id::text||':'||n.name)::uuid end as apontamento_id,
    r.work_order_id,r.numero_os,r.item,r.chassi_exibicao,n.name as responsavel,r.etapa,
    r.data_fechamento,r.inicio,r.termino,
    (round(r.setup_time_hours*n.ordinality/cardinality(crew.names),2)-round(r.setup_time_hours*(n.ordinality-1)/cardinality(crew.names),2))::numeric(10,2) as setup_time_hours,
    (round(r.production_time_hours*n.ordinality/cardinality(crew.names),2)-round(r.production_time_hours*(n.ordinality-1)/cardinality(crew.names),2))::numeric(10,2) as production_time_hours,
    (round(r.total_stopped_time_hours*n.ordinality/cardinality(crew.names),2)-round(r.total_stopped_time_hours*(n.ordinality-1)/cardinality(crew.names),2))::numeric(10,2) as total_stopped_time_hours,
    r.etapa_id,r.status_etapa,r.fase,r.registrado_por
from rows r
cross join lateral (select public.mes_operator_names(r.responsavel) as names) crew
cross join lateral unnest(crew.names) with ordinality n(name,ordinality);


comment on view bi.fato_fechamento_operador is
 'Horas por pessoa: rateio igual da equipe de cada intervalo e do residual manual. Somar horas; contar etapas distintas por etapa_id. Auditoria e total da etapa preservados.';
revoke all on function public.mes_operator_names(text),public.mes_normalize_operators(text) from public,anon,authenticated;
grant execute on function public.mes_operator_names(text),public.mes_normalize_operators(text) to service_role,powerbi_reader;
revoke all on bi.mes_operator_allocations from public,anon,authenticated;
grant select on bi.mes_operator_allocations,bi.fato_fechamento_operador to service_role,powerbi_reader;

-- Abort the entire migration if the normalization changes any production data
-- other than execution names, or if allocating crews changes the BI sum.
do $$
begin
  if exists(select 1 from operator_original_stage_values o join (
      select id,to_jsonb(s)-'responsavel' as data from public.erp_work_order_stages s
      union all select id,to_jsonb(s)-'responsavel' from public.erp_vehicle_entry_stages s
    ) n using(id) where o.data is distinct from n.data) then
    raise exception 'Normalização alterou dados de produção: transação abortada.';
  end if;
  if exists(select 1 from operator_original_totals o where row(o.setup,o.production,o.stopped)
      is distinct from (select row(sum(setup_time_hours),sum(production_time_hours),sum(total_stopped_time_hours))
                       from bi.fato_fechamento_operador)) then
    raise exception 'Rateio alterou total de horas do BI: transação abortada.';
  end if;
end $$;
commit;
