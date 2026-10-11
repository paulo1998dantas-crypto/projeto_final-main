"""Selection by persisted proposal/vehicle ID, never fuzzy proposal numbers."""
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from uuid import UUID
from urllib.parse import urlsplit


def same_origin(origin,base_url):
    # TLS is terminated by Render: the backend scheme can be HTTP while the
    # browser origin is HTTPS. Compare the actual authority, never a prefix.
    left,right=urlsplit(origin),urlsplit(base_url)
    return left.scheme in {'http','https'} and left.netloc.lower()==right.netloc.lower()


def available(conn):
    return bool(conn.execute(text("SELECT to_regprocedure('public.comercial_selecionar_proposta_mes(uuid,text,integer,text,text,text)')")).scalar())


def options(conn,work_id=None,search='',limit=100,entry_id=None):
    try:UUID(str(entry_id or work_id))
    except (ValueError,TypeError):raise ValueError('O.S. inválida.')
    if not available(conn):raise ValueError('Seletor comercial indisponível. Aplique a atualização do Comercial.')
    query=text("""
      select c.id,c.version,c.proposal_id,c.sequence,c.chassi,p.client_name,p.destination,p.status,
       coalesce(nullif(p.configuration->>'original_proposal_number',''),p.number)
         ||case when p.revision>1 then '.'||(p.revision-1)::text else '' end as proposal_number,
       (c.work_order_id=o.id::text) as linked
      from erp_vehicle_entries e left join erp_work_orders o on o.vehicle_entry_id=e.id and o.is_current
      join erp_vehicles v on v.id=e.vehicle_id cross join comercial_programacao c
      join comercial_propostas p on p.id=c.proposal_id
      where {identity} and c.included and c.status<>'CANCELADO'
        and p.kind<>'COMPLEMENTAR' and p.status not in ('CANCELADA','SUBSTITUÍDA')
        and (nullif(trim(c.chassi),'') is null or upper(trim(c.chassi))=upper(trim(v.chassi)))
        and (c.vehicle_entry_id is null or c.vehicle_entry_id=e.id::text)
        and (c.work_order_id is null or c.work_order_id=o.id::text)
        and (:q='' or p.number ilike :like or p.configuration->>'original_proposal_number' ilike :like
          or p.client_name ilike :like or p.destination ilike :like)
      order by linked desc nulls last, (c.chassi is not null) desc, p.updated_at desc,c.sequence
      limit :limit
    """.format(identity="e.id=cast(:identity as uuid)" if entry_id else "o.id=cast(:identity as uuid)"))
    return [dict(r) for r in conn.execute(query,{'identity':entry_id or work_id,'q':search.strip(),'like':'%'+search.strip()+'%','limit':max(1,min(int(limit),100))}).mappings()]


def apply_selection(conn,work_id,payload,actor):
    """Called inside the same transaction that opens/edits this exact MES card."""
    if not payload.get('commercial_program_id'):
        return
    current=conn.execute(text("select proposta_numero from erp_work_orders where id=cast(:id as uuid)"),{'id':work_id}).scalar_one()
    return select_proposal(conn,work_id,{
        'program_id':payload['commercial_program_id'],
        'program_version':payload.get('commercial_program_version'),
        'expected_proposal':current,
        'motivo':payload.get('commercial_link_reason') or 'Proposta selecionada na abertura/edição da O.S.'
    },actor)


def select_proposal(conn,work_id,payload,actor):
    if not available(conn):raise ValueError('Seletor comercial indisponível. Aplique a atualização do Comercial.')
    if not isinstance(payload,dict) or not str(payload.get('program_id') or '').strip():
        raise ValueError('Selecione uma proposta da lista.')
    if type(payload.get('program_version')) is not int:raise ValueError('Versão da proposta obrigatória. Atualize a lista.')
    if 'expected_proposal' not in payload:raise ValueError('Referência vigente do card obrigatória. Atualize a tela.')
    try:
        return conn.execute(text("""select comercial_selecionar_proposta_mes(cast(:work as uuid),:program,:version,:old,:actor,:reason)"""),
          {'work':work_id,'program':payload['program_id'],'version':payload['program_version'],
           'old':str(payload['expected_proposal'] or ''),'actor':actor,'reason':str(payload.get('motivo') or '').strip()}).scalar_one()
    except DBAPIError as exc:
        raise ValueError(str(getattr(exc.orig,'diag',None).message_primary if getattr(exc.orig,'diag',None) else 'Vínculo não salvo. Confira a proposta e atualize o card.')) from exc
