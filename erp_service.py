"""New operational O.S./MES domain. Legacy MES tables remain read-only compatible."""
from datetime import datetime, date, timedelta
from functools import cmp_to_key
import json
from uuid import uuid4
import re
import unicodedata
from sqlalchemy import text
from erp_catalogs import (
    REQUIRED_WORK_ORDER_FIELDS, VENDEDORES, MERCADOS, TIPOS_VEICULO, LINHAS,
    AR_FORNECEDORES, AR_TIPOS, SIM_NAO, TRANSFORMACOES,
)

STAGES = [
    ("VIDROS", 10, []), ("A/C", 20, []), ("PREP", 30, []), ("SERRA", 40, []),
    ("EXPE", 50, []), ("DESMONT", 60, ["VIDROS", "A/C"]), ("ELÉTRICA", 70, ["DESMONT"]),
    ("REVEST", 80, ["DESMONT"]), ("BCO", 90, ["REVEST"]), ("ACESSÓRIO", 100, []),
    ("PLOTAGEM", 110, []), ("LIBERAÇÃO", 120, ["BCO"]),
]

STAGE_INPUT_TO_STATUS = {
    "?": ("PENDENTE", None, False),
    "N": ("PENDENTE", True, True),
    "P": ("EM_ANDAMENTO", True, True),
    "S": ("CONCLUÍDA", True, True),
    "N/A": ("NÃO_APLICÁVEL", False, True),
}

WORK_ORDER_FIELDS = (
    "tipo_servico", "proposta_numero", "data_aprovacao", "vendedor", "mercado",
    "cliente_nome", "municipio", "uf", "tipo_veiculo", "linha", "transformacao",
    "transformacao_codigo", "codigo_banco", "conjunto_bancos", "acessibilidade", "lotacao",
    "ar_condicionado", "tipo_sistema_ar", "ar_quente", "acessorio", "plotagem",
    "data_comercial_prevista",
)
WORK_ORDER_DATE_FIELDS = {"data_aprovacao", "data_comercial_prevista"}

LEAD_TIME_DAYS = {"LE": 45, "LAE": 45, "LB": 30, "LAB": 30}

# A sequência operacional é deliberadamente separada das colunas *_legacy.
# Estas últimas continuam servindo exclusivamente à rastreabilidade da planilha.
SEQUENCE_FIELDS = {
    "delivery_date": "Data de entrega vigente",
    "manual_priority": "Prioridade manual",
    "line": "Linha",
    "vehicle_type": "Tipo de veículo",
    "transformation": "Transformação",
    "air_conditioning": "Ar-condicionado",
    "banks": "Conjunto de bancos",
    "client": "Cliente",
    "item_number": "ITEM",
}
DEFAULT_SEQUENCE_CRITERIA = [
    {"field": "delivery_date", "direction": "ASC"},
    {"field": "manual_priority", "direction": "ASC"},
    {"field": "line", "direction": "ASC"},
    {"field": "item_number", "direction": "ASC"},
]

def _id(): return str(uuid4())
def _one(result):
    row=result.first(); return dict(row._mapping) if row else None


def _normalize_chassis(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def _is_complete_vin(value):
    return bool(re.fullmatch(r"[A-Z0-9]{17}", str(value or "")))


def _resolve_vehicle(conn, chassi, payload):
    """Return the physical vehicle, promoting one unambiguous legacy row.

    Migration 202607290800 must be applied before this code is deployed.  This
    function deliberately performs no runtime DDL and never guesses a full VIN
    from a reduced chassis.
    """
    # Serializes two simultaneous entries for the same VIN without locking
    # unrelated vehicles. The unique constraint on chassi remains the final
    # protection against writers outside this application.
    conn.execute(
        text("select pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"erp_vehicle_vin:{chassi}"},
    )
    vehicle = _one(conn.execute(
        text("select * from erp_vehicles where chassi=:chassi for update"),
        {"chassi": chassi},
    ))
    if vehicle:
        return vehicle, False

    if not _is_complete_vin(chassi):
        raise ValueError(
            "Informe o chassi completo com 17 caracteres. "
            "Chassi reduzido é aceito somente em registros legados importados."
        )

    legacy_rows = [
        dict(row._mapping)
        for row in conn.execute(text("""
            select *
              from erp_vehicles
             where chassi_completo=false
               and legacy_chassi_reduzido=:reduced
             order by id
             for update
        """), {"reduced": chassi[-8:]})
    ]
    if len(legacy_rows) > 1:
        raise ValueError(
            "Existem vários veículos legados com os mesmos oito caracteres finais; "
            "a promoção para VIN completo exige reconciliação manual."
        )
    if legacy_rows:
        vehicle = legacy_rows[0]
        conn.execute(text("""
            update erp_vehicles
               set chassi=:chassi,
                   chassi_completo=true,
                   legacy_chassi_reduzido=null
             where id=:id
        """), {"chassi": chassi, "id": vehicle["id"]})
        vehicle.update({
            "chassi": chassi,
            "chassi_completo": True,
            "legacy_chassi_reduzido": None,
        })
        return vehicle, False

    vehicle_id = _id()
    vehicle = {
        "id": vehicle_id,
        "chassi": chassi,
        "marca": str(payload.get("marca") or ""),
        "modelo": str(payload.get("modelo") or ""),
        "versao": str(payload.get("versao") or ""),
        "mmv": str(payload.get("mmv") or ""),
        "chassi_completo": True,
        "legacy_chassi_reduzido": None,
    }
    conn.execute(text("""
        insert into erp_vehicles(
            id,chassi,marca,modelo,versao,mmv,
            chassi_completo,legacy_chassi_reduzido
        ) values(
            :id,:chassi,:marca,:modelo,:versao,:mmv,
            true,null
        )
    """), vehicle)
    return vehicle, True


def _token(value):
    return "".join(
        char for char in unicodedata.normalize("NFKD", str(value or "").strip().upper())
        if not unicodedata.combining(char)
    )


def operational_work_order_status(value):
    """Canonical lifecycle value for MES filters, independent of labels/encoding."""
    normalized = _token(value).replace("_", " ").strip()
    # Older imports/releases may have persisted either the accented canonical
    # value or its ASCII equivalent.  Both are the same WIP lifecycle state.
    if normalized in {"EM PRODUCAO", "EM PRODUCAO "}:
        return "EM_PRODUCAO"
    if normalized == "ATIVA":
        return "ATIVA"
    return normalized.replace(" ", "_")


def service_type_group(value):
    """Classify the service without turning status into a compound database value."""
    normalized = _token(value).replace("_", " ").replace("-", " ")
    if "POS" in normalized and "VENDA" in normalized:
        return "PÓS-VENDAS"
    if normalized in {"", "TRANSFORMACAO"}:
        return "TRANSFORMAÇÃO"
    # INSTALAÇÃO DE ACESSÓRIO, RETORNO e OUTRO seguem o mesmo agrupamento
    # operacional e financeiro de entregas fora da transformação.
    return "OUTROS"


def work_order_situation(status, service_type="", stage_configuration_status=""):
    """Presentation label; canonical status and service type remain independent."""
    normalized_status = _token(status)
    if (
        normalized_status in {"RASCUNHO", "AGUARDANDO O S", "AGUARDANDO_O_S"}
        and _token(stage_configuration_status) == "PENDENTE"
    ):
        base = "AG. PARAMETRIZAÇÃO DE ETAPAS"
    elif normalized_status == "ATIVA":
        base = "EM PÁTIO"
    else:
        base = str(status or "AGUARDANDO O.S.").replace("_", " ")
    group = service_type_group(service_type)
    return base if group == "TRANSFORMAÇÃO" else f"{base} {group}"

def _date_value(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None

def _work_field_value(name, value):
    if name in WORK_ORDER_DATE_FIELDS:
        return _date_value(value)
    return str(value or "").strip()

def _stage_applicable(code, work):
    return not (
        (code == "A/C" and _token(work.get("ar_condicionado")) in {"", "NAO"})
        or (code == "BCO" and _token(work.get("conjunto_bancos")) in {"", "NAO", "SEM"})
        or (code == "ACESSÓRIO" and _token(work.get("acessorio")) in {"", "NAO", "SEM"})
        or (code == "PLOTAGEM" and _token(work.get("plotagem")) in {"", "NAO", "SEM"})
    )

def stage_input_code(stage):
    if not bool(stage.get("parametrizado")):
        return "?"
    if not bool(stage.get("aplicavel")) or _token(stage.get("status")) == "NAO_APLICAVEL":
        return "N/A"
    return {
        "CONCLUIDA": "S",
        "EM_ANDAMENTO": "P",
    }.get(_token(stage.get("status")), "N")

def _ensure_stage_rows(conn, work_id, work):
    for code, order, _ in STAGES:
        applicable = _stage_applicable(code, work)
        conn.execute(text("""
            insert into erp_work_order_stages(
                id,work_order_id,stage_code,aplicavel,status,ordem,
                data_planejada,parametrizado
            ) values(
                :id,:work,:code,:applicable,'PENDENTE',:order,:planned,false
            )
            on conflict(work_order_id,stage_code) do nothing
        """), {
            "id": _id(), "work": work_id, "code": code,
            "applicable": applicable, "order": order,
            "planned": work.get("data_comercial_prevista"),
        })

def _commercial_date(arrival, approval, line):
    dates = [value for value in (_date_value(arrival), _date_value(approval)) if value]
    if not dates:
        return None
    return max(dates) + timedelta(days=LEAD_TIME_DAYS.get(str(line or "").strip().upper(), 30))


def _sequence_schema_ready(conn):
    """Keep the application backward-compatible while the additive migration rolls out."""
    return bool(conn.execute(text(
        "select to_regclass('public.erp_work_order_sequences') is not null"
    )).scalar())


def _normalized_sequence_criteria(criteria):
    if not isinstance(criteria, list):
        raise ValueError("Os critérios de sequenciamento devem ser uma lista.")
    normalized = []
    for item in criteria:
        if not isinstance(item, dict):
            raise ValueError("Cada critério de sequenciamento deve informar campo e direção.")
        field = str(item.get("field") or "").strip()
        direction = str(item.get("direction") or "ASC").strip().upper()
        if field not in SEQUENCE_FIELDS or direction not in {"ASC", "DESC"}:
            raise ValueError("Critério de sequenciamento inválido.")
        if field in {entry["field"] for entry in normalized}:
            raise ValueError("Um campo não pode se repetir na sequência.")
        normalized.append({"field": field, "direction": direction})
    if not normalized or len(normalized) > 5:
        raise ValueError("Informe entre um e cinco critérios de sequenciamento.")
    return normalized


def _sequence_value(row, field):
    mapping = {
        "delivery_date": _date_value(row.get("data_comercial_prevista")),
        "manual_priority": row.get("prioridade_manual"),
        "line": _token(row.get("linha")),
        "vehicle_type": _token(row.get("tipo_veiculo")),
        "transformation": _token(row.get("transformacao")),
        "air_conditioning": _token(row.get("ar_condicionado")),
        "banks": _token(row.get("conjunto_bancos")),
        "client": _token(row.get("cliente_nome")),
        "item_number": row.get("item_number"),
    }
    return mapping[field]


def _compare_sequence_rows(left, right, criteria):
    for criterion in criteria:
        left_value = _sequence_value(left, criterion["field"])
        right_value = _sequence_value(right, criterion["field"])
        left_empty = left_value in (None, "")
        right_empty = right_value in (None, "")
        if left_empty != right_empty:
            return 1 if left_empty else -1  # vazios sempre depois, inclusive DESC
        if left_empty:
            continue
        if left_value != right_value:
            comparison = 1 if left_value > right_value else -1
            return comparison if criterion["direction"] == "ASC" else -comparison
    left_item, right_item = int(left.get("item_number") or 0), int(right.get("item_number") or 0)
    if left_item != right_item:
        return -1 if left_item < right_item else 1
    return -1 if str(left["id"]) < str(right["id"]) else (1 if str(left["id"]) > str(right["id"]) else 0)


def _active_sequence_profile(conn):
    if not _sequence_schema_ready(conn):
        return {"id": None, "nome": "Prazo de entrega", "criterios": DEFAULT_SEQUENCE_CRITERIA}
    profile = _one(conn.execute(text("""
        select id,nome,criterios
        from erp_sequence_profiles
        where ativo=true
        order by updated_at desc,created_at desc
        limit 1
    """)))
    if not profile:
        return {"id": None, "nome": "Prazo de entrega", "criterios": DEFAULT_SEQUENCE_CRITERIA}
    profile["criterios"] = _normalized_sequence_criteria(profile.get("criterios"))
    return profile


def recalculate_work_order_sequences(conn, actor="SISTEMA"):
    """Persist the WIP order and mirror its delivery week/rank into every stage.

    One advisory transaction lock avoids two concurrent reprogramações producing
    different ranks.  It never changes dates, status, historical schedules or
    legacy sequence columns.
    """
    if not _sequence_schema_ready(conn):
        return {"recalculated": False, "reason": "schema_pending", "count": 0}
    conn.execute(text("select pg_advisory_xact_lock(hashtext('erp_wip_sequence'))"))
    profile = _active_sequence_profile(conn)
    rows = [dict(row._mapping) for row in conn.execute(text("""
        select w.id,w.status,w.data_comercial_prevista,w.linha,w.tipo_veiculo,
               w.transformacao,w.ar_condicionado,w.conjunto_bancos,w.cliente_nome,
               e.item_number,seq.prioridade_manual
          from erp_work_orders w
          join erp_vehicle_entries e on e.id=w.vehicle_entry_id
          left join erp_work_order_sequences seq on seq.work_order_id=w.id
         where w.status in ('ATIVA','EM_PRODUÇÃO')
         for update of w
    """))]
    rows.sort(key=cmp_to_key(lambda left, right: _compare_sequence_rows(
        left, right, profile["criterios"]
    )))
    for position, row in enumerate(rows, start=1):
        delivery = _date_value(row.get("data_comercial_prevista"))
        week = str(delivery.isocalendar().week) if delivery else None
        conn.execute(text("""
            insert into erp_work_order_sequences(
                work_order_id,profile_id,data_entrega_vigente,semana_planejada,
                sequencia,ativo,updated_by,updated_at
            ) values(:work,:profile,:delivery,:week,:sequence,true,:actor,now())
            on conflict(work_order_id) do update set
                profile_id=excluded.profile_id,
                data_entrega_vigente=excluded.data_entrega_vigente,
                semana_planejada=excluded.semana_planejada,
                sequencia=excluded.sequencia,
                ativo=true,
                updated_by=excluded.updated_by,
                updated_at=excluded.updated_at
        """), {
            "work": row["id"], "profile": profile.get("id"),
            "delivery": delivery, "week": week, "sequence": position, "actor": actor,
        })
        conn.execute(text("""
            update erp_work_order_stages
               set data_planejada=:delivery,
                   semana_planejada=:week,
                   sequencia_planejada=:sequence
             where work_order_id=:work
        """), {"work": row["id"], "delivery": delivery, "week": week, "sequence": position})
    conn.execute(text("""
        update erp_work_order_sequences
           set ativo=false,updated_at=now(),updated_by=:actor
         where ativo=true
           and work_order_id not in (
               select id from erp_work_orders where status in ('ATIVA','EM_PRODUÇÃO')
           )
    """), {"actor": actor})
    return {"recalculated": True, "count": len(rows), "profile": profile["nome"]}


def sequence_overview(conn):
    if not _sequence_schema_ready(conn):
        return {"schema_ready": False, "profile": None, "orders": []}
    profile = _active_sequence_profile(conn)
    rows = [dict(row._mapping) for row in conn.execute(text("""
        select seq.work_order_id,seq.sequencia,seq.prioridade_manual,
               seq.data_entrega_vigente,seq.semana_planejada,seq.updated_at,
               w.numero_os,w.status,w.linha,w.cliente_nome,w.transformacao,
               e.item_number,v.chassi,v.marca,v.modelo,v.versao
          from erp_work_order_sequences seq
          join erp_work_orders w on w.id=seq.work_order_id
          join erp_vehicle_entries e on e.id=w.vehicle_entry_id
          join erp_vehicles v on v.id=e.vehicle_id
         where seq.ativo=true and w.status in ('ATIVA','EM_PRODUÇÃO')
         order by seq.sequencia nulls last,e.item_number
    """))]
    return {"schema_ready": True, "profile": profile, "orders": rows}


def update_sequence_profile(conn, payload, actor):
    if not _sequence_schema_ready(conn):
        raise ValueError("A migration de sequenciamento ainda não foi aplicada.")
    criteria = _normalized_sequence_criteria(payload.get("criterios"))
    name = str(payload.get("nome") or "Sequência WIP").strip()[:100]
    if not name:
        raise ValueError("Informe o nome do critério de sequenciamento.")
    profile_id = str(payload.get("id") or _id())
    conn.execute(text("update erp_sequence_profiles set ativo=false where ativo=true"))
    conn.execute(text("""
        insert into erp_sequence_profiles(id,nome,criterios,ativo,created_by,updated_by)
        values(:id,:name,cast(:criteria as jsonb),true,:actor,:actor)
        on conflict(id) do update set
            nome=excluded.nome,criterios=excluded.criterios,ativo=true,
            updated_by=excluded.updated_by,updated_at=now()
    """), {"id": profile_id, "name": name, "criteria": json.dumps(criteria), "actor": actor})
    conn.execute(text("""
        insert into erp_audit_events(entity_type,entity_id,action,actor,origin,after_data)
        values('SEQUENCE_PROFILE',:id,'SEQUENCE_PROFILE_UPDATED',:actor,'MES',
               jsonb_build_object('nome',:name,'criterios',cast(:criteria as jsonb)))
    """), {"id": profile_id, "actor": actor, "name": name, "criteria": json.dumps(criteria)})
    return recalculate_work_order_sequences(conn, actor)


def update_manual_sequence_priority(conn, work_id, priority, actor):
    if not _sequence_schema_ready(conn):
        raise ValueError("A migration de sequenciamento ainda não foi aplicada.")
    work = _one(conn.execute(text("""
        select id,status from erp_work_orders where id=:id for update
    """), {"id": work_id}))
    if not work or work["status"] not in {"ATIVA", "EM_PRODUÇÃO"}:
        raise ValueError("A prioridade manual só pode ser definida para O.S. em WIP.")
    parsed = None if priority in (None, "") else int(priority)
    if parsed is not None and not 0 <= parsed <= 999999:
        raise ValueError("A prioridade manual deve estar entre 0 e 999999.")
    conn.execute(text("""
        insert into erp_work_order_sequences(work_order_id,prioridade_manual,ativo,updated_by)
        values(:id,:priority,true,:actor)
        on conflict(work_order_id) do update set
            prioridade_manual=excluded.prioridade_manual,updated_by=excluded.updated_by,updated_at=now()
    """), {"id": work_id, "priority": parsed, "actor": actor})
    conn.execute(text("""
        insert into erp_audit_events(entity_type,entity_id,action,actor,origin,after_data)
        values('WORK_ORDER',:id,'SEQUENCE_PRIORITY_UPDATED',:actor,'MES',
               jsonb_build_object('prioridade_manual',cast(:priority as integer)))
    """), {"id": work_id, "actor": actor, "priority": parsed})
    return recalculate_work_order_sequences(conn, actor)

def create_entry(conn, payload, actor):
    chassi = _normalize_chassis(payload.get("chassi"))
    if not chassi: raise ValueError('Chassi completo e obrigatorio.')
    vehicle, created = _resolve_vehicle(conn, chassi, payload)
    vehicle_id = str(vehicle["id"])
    if not created:
        # O chassi identifica o veículo físico. Em uma nova passagem, os dados
        # informados no cadastro devem corrigir descrições antigas (PACK,
        # STANDARD etc.) sem apagar valores existentes quando o campo vier vazio.
        vehicle_fields = {
            field: str(payload.get(field) or "").strip()
            for field in ("marca", "modelo", "versao", "mmv")
        }
        changed = {
            field: value
            for field, value in vehicle_fields.items()
            if value and value != str(vehicle.get(field) or "").strip()
        }
        if changed:
            assignments = ",".join(f"{field}=:{field}" for field in changed)
            conn.execute(
                text(f"update erp_vehicles set {assignments} where id=:id"),
                {"id": vehicle_id, **changed},
            )
    entry_id=_id(); row=_one(conn.execute(text("insert into erp_vehicle_entries(id,vehicle_id,data_chegada,cliente_id,cliente_nome,origem,observacoes,avarias,criado_por,status) values(:id,:vehicle,:arrival,:client_id,:client,:origin,:notes,:damage,:actor,'AGUARDANDO_O_S') returning item_number"),{'id':entry_id,'vehicle':vehicle_id,'arrival':payload.get('data_chegada') or datetime.utcnow(),'client_id':payload.get('cliente_id'),'client':str(payload.get('cliente_nome') or ''),'origin':str(payload.get('origem') or 'MANUAL'),'notes':str(payload.get('observacoes') or ''),'damage':str(payload.get('avarias') or ''),'actor':actor}))
    return {'id':entry_id,'vehicle_id':vehicle_id,'item_number':int(row['item_number'])}

def create_work_order(conn, entry_id, payload, actor):
    entry=_one(conn.execute(text('select item_number,data_chegada from erp_vehicle_entries where id=:id for update'),{'id':entry_id}))
    if not entry: raise ValueError('Entrada de veiculo nao encontrada.')
    forecast_id=str(payload.get('forecast_id') or '').strip() or None
    current=_one(conn.execute(text('select id,numero_os from erp_work_orders where vehicle_entry_id=:id'),{'id':entry_id}))
    if current:
        if forecast_id:
            linked_forecast=_one(conn.execute(text("""
                select id,codigo from suprimentos_forecasts
                where work_order_id=:work_id
            """), {'work_id': current['id']}))
            if not linked_forecast or str(linked_forecast['id']) != forecast_id:
                raise ValueError('Esta entrada ja possui O.S.; o Forecast nao pode ser trocado apos a abertura.')
            return {
                'id':str(current['id']), 'numero_os':current['numero_os'],'replayed':True,
                'forecast_id':str(linked_forecast['id']),'forecast_codigo':linked_forecast['codigo'],
            }
        return {'id':str(current['id']),'numero_os':current['numero_os'],'replayed':True}

    forecast=None
    if forecast_id:
        forecast=_one(conn.execute(text("""
            select id,codigo,status,vehicle_entry_id,work_order_id
            from suprimentos_forecasts
            where id=:id
            for update
        """), {'id':forecast_id}))
        if not forecast:
            raise ValueError('Forecast selecionado nao foi encontrado.')
        if forecast['status'] != 'ATIVO':
            raise ValueError('O Forecast selecionado nao esta ativo para alocacao.')
        if forecast['vehicle_entry_id'] or forecast['work_order_id']:
            raise ValueError('O Forecast selecionado ja esta vinculado a outra entrada ou O.S.')

    work_id=_id(); number=str(payload.get('numero_os') or entry['item_number'])
    fields={'tipo_servico':'TRANSFORMAÇÃO','proposta_numero':'','data_aprovacao':None,'vendedor':'','mercado':'','cliente_nome':'','municipio':'','uf':'','tipo_veiculo':'','linha':'','transformacao':'','transformacao_codigo':'','codigo_banco':'','conjunto_bancos':'','acessibilidade':'','lotacao':'','ar_condicionado':'','tipo_sistema_ar':'','ar_quente':'','acessorio':'','plotagem':'','data_comercial_prevista':None}
    fields.update({
        key: _work_field_value(key, value)
        for key, value in payload.items()
        if key in fields
    })
    conn.execute(text("""insert into erp_work_orders(id,vehicle_entry_id,numero_os,tipo_servico,proposta_numero,data_aprovacao,vendedor,mercado,cliente_nome,municipio,uf,tipo_veiculo,linha,transformacao_codigo,transformacao,codigo_banco,conjunto_bancos,acessibilidade,lotacao,ar_condicionado,tipo_sistema_ar,ar_quente,acessorio,plotagem,data_comercial_prevista,criado_por) values(:id,:entry,:number,:tipo_servico,:proposta_numero,:data_aprovacao,:vendedor,:mercado,:cliente_nome,:municipio,:uf,:tipo_veiculo,:linha,:transformacao_codigo,:transformacao,:codigo_banco,:conjunto_bancos,:acessibilidade,:lotacao,:ar_condicionado,:tipo_sistema_ar,:ar_quente,:acessorio,:plotagem,:data_comercial_prevista,:actor)"""),{'id':work_id,'entry':entry_id,'number':number,'actor':actor,**fields})
    conn.execute(text("insert into erp_work_order_status_history(work_order_id,novo_status,usuario,observacao) values(:id,'RASCUNHO',:actor,'O.S. aberta')"),{'id':work_id,'actor':actor})
    _ensure_stage_rows(conn, work_id, fields)
    if fields["data_comercial_prevista"]:
        conn.execute(text("""
            insert into erp_work_order_schedules(
                work_order_id,data_anterior,nova_data,motivo,usuario,vigente
            ) values(:id,null,:date,'PROGRAMAÇÃO INICIAL',:actor,true)
        """), {"id": work_id, "date": fields["data_comercial_prevista"], "actor": actor})
    conn.execute(text("update erp_vehicle_entries set status='O_S_ABERTA' where id=:id"), {"id": entry_id})
    if forecast:
        allocation=conn.execute(text("""
            update suprimentos_forecasts
               set status='CONVERTIDO',
                   vehicle_entry_id=:entry_id,
                   work_order_id=:work_id,
                   convertido_at=now(),
                   convertido_por=:actor,
                   atualizado_por=:actor,
                   updated_at=now(),
                   version=version+1
             where id=:forecast_id
               and status='ATIVO'
               and vehicle_entry_id is null
               and work_order_id is null
        """), {'forecast_id':forecast_id,'entry_id':entry_id,'work_id':work_id,'actor':actor})
        if allocation.rowcount != 1:
            raise ValueError('O Forecast foi alterado por outro usuario. Atualize a tela e tente novamente.')
        conn.execute(text("""
            insert into erp_audit_events(entity_type,entity_id,action,actor,origin,after_data)
            values(
                'FORECAST',:forecast_id,'ALOCADO_NA_ABERTURA_OS',:actor,'GESTAO_OS',
                jsonb_build_object('vehicle_entry_id',:entry_id,'work_order_id',:work_id,'numero_os',:numero_os)
            )
        """), {'forecast_id':forecast_id,'entry_id':entry_id,'work_id':work_id,'numero_os':number,'actor':actor})
    return {
        'id':work_id, 'numero_os':number, 'replayed':False,
        'stage_configuration_status':'PENDENTE',
        'forecast_id':forecast_id,
        'forecast_codigo':forecast['codigo'] if forecast else None,
    }

def update_work_order(conn, work_id, payload, actor):
    work = _one(conn.execute(text("""
        select w.*,e.data_chegada from erp_work_orders w
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        where w.id=:id for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    editable_statuses = {"RASCUNHO", "AGUARDANDO_O_S", "ATIVA", "EM_PRODUÇÃO"}
    if work["status"] not in editable_statuses:
        raise ValueError("O.S. finalizada, entregue, retirada ou cancelada não pode ser editada.")
    is_draft = work["status"] in {"RASCUNHO", "AGUARDANDO_O_S"}
    fields = {
        name: _work_field_value(name, payload[name]) if name in payload else work.get(name)
        for name in WORK_ORDER_FIELDS
    }
    if (
        not is_draft
        and "data_comercial_prevista" in payload
        and fields["data_comercial_prevista"] != _date_value(work.get("data_comercial_prevista"))
    ):
        raise ValueError("Depois da ativação, altere a data de entrega pela função Reprogramar do MES.")
    assignments = ",".join(f"{name}=:{name}" for name in WORK_ORDER_FIELDS)
    conn.execute(
        text(f"update erp_work_orders set {assignments},updated_at=now(),version=version+1 where id=:id"),
        {"id": work_id, **fields},
    )
    if is_draft and fields["data_comercial_prevista"]:
        initial = _one(conn.execute(text("""
            select id from erp_work_order_schedules
            where work_order_id=:id and motivo='PROGRAMAÇÃO INICIAL'
            order by created_at limit 1
        """), {"id": work_id}))
        if initial:
            conn.execute(text("""
                update erp_work_order_schedules
                set nova_data=:date,data_anterior=null,vigente=true
                where id=:schedule
            """), {"date": fields["data_comercial_prevista"], "schedule": initial["id"]})
        else:
            conn.execute(text("""
                insert into erp_work_order_schedules(
                    work_order_id,data_anterior,nova_data,motivo,usuario,vigente
                ) values(:id,null,:date,'PROGRAMAÇÃO INICIAL',:actor,true)
            """), {"id": work_id, "date": fields["data_comercial_prevista"], "actor": actor})
        conn.execute(text("""
            update erp_work_order_stages
               set data_planejada=:date,
                   semana_planejada=to_char(:date::date,'IW')
             where work_order_id=:id
        """), {"id": work_id, "date": fields["data_comercial_prevista"]})
    if not is_draft:
        for code, _, _ in STAGES:
            stage = _one(conn.execute(text("""
                select id,aplicavel,status from erp_work_order_stages
                where work_order_id=:work and stage_code=:code for update
            """), {"work": work_id, "code": code}))
            if not stage:
                continue
            applicable = _stage_applicable(code, fields)
            if bool(stage["aplicavel"]) == applicable:
                continue
            if _token(stage["status"]) not in {"PENDENTE", "LIBERADA", "NAO_APLICAVEL"}:
                raise ValueError(
                    f"A aplicabilidade da etapa {code} não pode mudar porque ela já possui apontamento."
                )
            new_status = "PENDENTE" if applicable else "NÃO_APLICÁVEL"
            conn.execute(text("""
                update erp_work_order_stages
                set aplicavel=:applicable,status=:status
                where id=:id
            """), {"applicable": applicable, "status": new_status, "id": stage["id"]})
            conn.execute(text("""
                insert into erp_work_order_stage_events(
                    work_order_stage_id,action,status_anterior,novo_status,operador,observacao
                ) values(
                    :stage,'APLICABILIDADE_ATUALIZADA',:old,:new,:actor,
                    'Aplicabilidade ajustada pela edição da O.S. em Suprimentos'
                )
            """), {
                "stage": stage["id"], "old": stage["status"],
                "new": new_status, "actor": actor,
            })
    conn.execute(text("""
        insert into erp_audit_events(entity_type,entity_id,action,actor,origin,after_data)
        values('WORK_ORDER',:id,:action,:actor,'SUPRIMENTOS',
               jsonb_build_object('version',cast(:version as integer)+1))
    """), {
        "id": work_id, "actor": actor, "version": work["version"],
        "action": "RASCUNHO_ATUALIZADO" if is_draft else "O_S_ATUALIZADA",
    })
    if not is_draft:
        recalculate_work_order_sequences(conn, actor)
    return {"id": work_id, "numero_os": work["numero_os"], "data_comercial_prevista": fields["data_comercial_prevista"]}

def activate_work_order(conn, work_id, actor):
    work=_one(conn.execute(text('select * from erp_work_orders where id=:id for update'),{'id':work_id}))
    if not work: raise ValueError('O.S. nao encontrada.')
    if work['status'] in ('ATIVA','EM_PRODUÇÃO'): return {'id':work_id,'replayed':True}
    _ensure_stage_rows(conn, work_id, work)
    pending_stages = conn.execute(text("""
        select count(*)
        from erp_work_order_stages
        where work_order_id=:id and not parametrizado
    """), {"id": work_id}).scalar_one()
    if work.get("stage_configuration_status") != "CONCLUIDA" or pending_stages:
        raise ValueError(
            f"A O.S. aguarda parametrização das etapas no MES ({pending_stages} etapa(s) com ?)."
        )
    missing = [field for field in REQUIRED_WORK_ORDER_FIELDS if not str(work.get(field) or "").strip()]
    if (
        _token(work.get("tipo_sistema_ar")) not in {"NAO", "AR ORIGINAL", "AG"}
        and not str(work.get("ar_condicionado") or "").strip()
    ):
        missing.append("ar_condicionado")
    if missing:
        raise ValueError("Campos obrigatórios pendentes para ativar: " + ", ".join(missing) + ".")
    controlled = {
        "vendedor": VENDEDORES,
        "mercado": MERCADOS,
        "tipo_veiculo": TIPOS_VEICULO,
        "linha": LINHAS,
        "tipo_sistema_ar": AR_TIPOS,
        "ar_quente": SIM_NAO,
    }
    invalid = [
        field for field, options in controlled.items()
        if _token(work.get(field)) not in {_token(option) for option in options}
    ]
    if (
        str(work.get("ar_condicionado") or "").strip()
        and _token(work.get("ar_condicionado")) not in {_token(option) for option in AR_FORNECEDORES}
    ):
        invalid.append("ar_condicionado")
    transformations = {str(code): description for code, description in TRANSFORMACOES}
    if (
        str(work.get("transformacao_codigo") or "") not in transformations
        or _token(transformations.get(str(work.get("transformacao_codigo") or ""), ""))
           != _token(work.get("transformacao"))
    ):
        invalid.append("transformacao")
    if invalid:
        raise ValueError("Valores fora das listas controladas: " + ", ".join(invalid) + ".")
    conn.execute(text("update erp_work_orders set status='ATIVA',ativado_por=:actor,ativado_at=now(),updated_at=now(),version=version+1 where id=:id"),{'id':work_id,'actor':actor})
    conn.execute(text("update erp_vehicle_entries set status='ATIVA' where id=:id"), {"id": work["vehicle_entry_id"]})
    conn.execute(text("insert into erp_work_order_status_history(work_order_id,status_anterior,novo_status,usuario,observacao) values(:id,:old,'ATIVA',:actor,'Etapas publicadas no MES')"),{'id':work_id,'old':work['status'],'actor':actor})
    recalculate_work_order_sequences(conn, actor)
    return {'id':work_id,'replayed':False}

def active_cards(conn):
    rows=conn.execute(text("""
        select w.id,w.numero_os,w.status,w.tipo_servico,w.technical_status,e.item_number,
               v.chassi,v.marca,v.modelo,v.versao,
               w.cliente_nome,w.linha,w.transformacao,w.data_comercial_prevista,
               seq.sequencia,seq.semana_planejada,seq.prioridade_manual,
               count(s.id) filter(where s.aplicavel) as etapas_aplicaveis,
               count(s.id) filter(where s.status='CONCLUÍDA') as etapas_concluidas
        from erp_work_orders w
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        left join erp_work_order_sequences seq on seq.work_order_id=w.id and seq.ativo=true
        left join erp_work_order_stages s on s.work_order_id=w.id
        where w.status in ('ATIVA','EM_PRODUÇÃO')
        group by w.id,e.item_number,v.chassi,v.marca,v.modelo,v.versao,
                 seq.sequencia,seq.semana_planejada,seq.prioridade_manual
        order by seq.sequencia nulls last,w.data_comercial_prevista nulls last,e.item_number
    """))
    cards = [dict(x._mapping) for x in rows]
    for card in cards:
        card["tipo_servico_grupo"] = service_type_group(card.get("tipo_servico"))
        card["situacao"] = work_order_situation(card.get("status"), card.get("tipo_servico"))
    return cards


def active_work_order_options(conn, search="", limit=20):
    """Return compact active O.S. options for other ERP backends.

    The full chassis remains the stored identifier; ``chassi_exibicao`` is
    presentation-only. Closed technical records are intentionally excluded
    from new stock commitments.
    """
    query = str(search or "").strip()
    bounded_limit = min(max(int(limit or 20), 1), 100)
    rows = conn.execute(text("""
        select w.id as work_order_id,w.numero_os,e.item_number,v.chassi,
               right(v.chassi,8) as chassi_exibicao,
               coalesce(nullif(trim(w.cliente_nome),''),nullif(trim(e.cliente_nome),''),'') as cliente,
               concat_ws(' ',nullif(trim(v.marca),''),nullif(trim(v.modelo),''),
                         nullif(trim(v.versao),'')) as veiculo
          from erp_work_orders w
          join erp_vehicle_entries e on e.id=w.vehicle_entry_id
          join erp_vehicles v on v.id=e.vehicle_id
         where w.status in ('ATIVA','EM_PRODUÇÃO')
           and coalesce(w.technical_status,'ABERTA')='ABERTA'
           and (
                :search=''
                or concat_ws(' ',w.numero_os,e.item_number,v.chassi,right(v.chassi,8),
                             w.cliente_nome,e.cliente_nome,v.marca,v.modelo,v.versao)
                   ilike :pattern
           )
         order by e.item_number desc,w.numero_os
         limit :limit
    """), {
        "search": query,
        "pattern": f"%{query}%",
        "limit": bounded_limit,
    }).mappings()
    options = []
    for row in rows:
        option = dict(row)
        option["label"] = " · ".join(
            value for value in (
                f"O.S. {option['numero_os']}",
                str(option.get("chassi_exibicao") or ""),
                str(option.get("cliente") or ""),
            ) if value
        )
        options.append(option)
    return options


def list_work_orders(conn, search="", status="", limit=1000):
    params = {
        "search": f"%{str(search or '').strip()}%",
        "status": str(status or "").strip().upper(),
        "limit": min(max(int(limit or 1000), 1), 2000),
    }
    rows = conn.execute(text("""
        select e.id as entry_id,e.item_number,e.status as entry_status,e.data_chegada,
               e.cliente_nome as entry_client,e.observacoes as entry_notes,e.avarias,
               v.id as vehicle_id,v.chassi,v.marca,v.modelo,v.versao,v.mmv,
               w.id as work_order_id,w.numero_os,w.tipo_servico,w.proposta_numero,
               w.data_aprovacao,w.vendedor,w.mercado,w.cliente_nome,w.municipio,w.uf,
               w.tipo_veiculo,w.linha,w.transformacao_codigo,w.transformacao,w.codigo_banco,w.conjunto_bancos,
               w.acessibilidade,w.lotacao,w.ar_condicionado,w.tipo_sistema_ar,w.ar_quente,
               w.acessorio,w.plotagem,w.data_comercial_prevista,w.status,w.version,
               w.stage_configuration_status,w.stage_configured_at,w.stage_configured_by,
               w.technical_status,w.technical_closed_at,w.technical_closed_by,
               w.technical_close_reason,
               w.created_at,w.updated_at,
               f.id as forecast_id,f.codigo as forecast_codigo,f.status as forecast_status,
               seq.sequencia,seq.semana_planejada,seq.prioridade_manual,
               count(s.id) as etapas_total,
               count(s.id) filter(where s.aplicavel) as etapas_aplicaveis,
               count(s.id) filter(where s.status='CONCLUÍDA') as etapas_concluidas,
               count(s.id) filter(where not s.parametrizado) as etapas_nao_parametrizadas
        from erp_vehicle_entries e
        join erp_vehicles v on v.id=e.vehicle_id
        left join erp_work_orders w on w.vehicle_entry_id=e.id
        left join suprimentos_forecasts f on f.work_order_id=w.id
        left join erp_work_order_sequences seq on seq.work_order_id=w.id and seq.ativo=true
        left join erp_work_order_stages s on s.work_order_id=w.id
        where (:status='' or coalesce(w.status,e.status)=:status)
          and (:search='%%' or concat_ws(' ',e.item_number,v.chassi,v.marca,v.modelo,
               e.cliente_nome,w.numero_os,w.cliente_nome,w.proposta_numero,w.linha,
               w.transformacao) ilike :search)
        group by e.id,v.id,w.id,f.id,f.codigo,f.status,seq.sequencia,seq.semana_planejada,seq.prioridade_manual
        order by
          case
            when w.status in ('ATIVA','EM_PRODUÇÃO') then 0
            when w.status in ('RASCUNHO','AGUARDANDO_O_S') or w.id is null then 1
            else 2
          end,
          seq.sequencia nulls last,e.item_number desc
        limit :limit
    """), params)
    orders = [dict(row._mapping) for row in rows]
    for order in orders:
        order["status_operacional"] = operational_work_order_status(
            order.get("status") or order.get("entry_status")
        )
        order["em_wip"] = bool(order.get("work_order_id")) and (
            order["status_operacional"] in {"ATIVA", "EM_PRODUCAO"}
        )
        order["tipo_servico_grupo"] = service_type_group(order.get("tipo_servico")) if order.get("work_order_id") else ""
        order["situacao"] = work_order_situation(
            order.get("status") or order.get("entry_status"),
            order.get("tipo_servico"),
            order.get("stage_configuration_status"),
        )
    return orders

def work_order_detail(conn, work_id):
    work = _one(conn.execute(text("""
        select w.*,e.item_number,e.data_chegada,e.status as entry_status,e.observacoes as entry_notes,
               e.avarias,v.chassi,v.marca,v.modelo,v.versao,v.mmv,
               f.id as forecast_id,f.codigo as forecast_codigo,f.status as forecast_status
               ,seq.sequencia,seq.semana_planejada,seq.prioridade_manual
        from erp_work_orders w
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        left join suprimentos_forecasts f on f.work_order_id=w.id
        left join erp_work_order_sequences seq on seq.work_order_id=w.id
        where w.id=:id
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    work["tipo_servico_grupo"] = service_type_group(work.get("tipo_servico"))
    work["situacao"] = work_order_situation(
        work.get("status"), work.get("tipo_servico"), work.get("stage_configuration_status")
    )
    if (
        work.get("status") in {"RASCUNHO", "AGUARDANDO_O_S"}
        and work.get("stage_configuration_status") == "PENDENTE"
    ):
        # Compatibilidade idempotente para rascunhos criados antes da migração.
        _ensure_stage_rows(conn, work_id, work)
    stages = [dict(row._mapping) for row in conn.execute(text("""
        select * from erp_work_order_stages where work_order_id=:id order by ordem
    """), {"id": work_id})]
    cycle_starts = [stage.get("inicio") for stage in stages if stage.get("inicio")]
    release = next((stage for stage in stages if stage["stage_code"] == "LIBERAÇÃO"), None)
    work["inicio_ciclo_produtivo"] = min(cycle_starts) if cycle_starts else None
    work["fim_ciclo_produtivo"] = (
        release.get("termino")
        if release and _token(release.get("status")) == "CONCLUIDA"
        else None
    )
    for stage in stages:
        stage["input_code"] = stage_input_code(stage)
    schedules = [dict(row._mapping) for row in conn.execute(text("""
        select * from erp_work_order_schedules where work_order_id=:id order by created_at desc
    """), {"id": work_id})]
    history = [dict(row._mapping) for row in conn.execute(text("""
        select * from erp_work_order_status_history where work_order_id=:id order by created_at desc
    """), {"id": work_id})]
    stage_events = [dict(row._mapping) for row in conn.execute(text("""
        select e.*,s.stage_code
        from erp_work_order_stage_events e
        join erp_work_order_stages s on s.id=e.work_order_stage_id
        where s.work_order_id=:id
        order by e.created_at desc
        limit 500
    """), {"id": work_id})]
    return {
        "work_order": work,
        "stages": stages,
        "schedules": schedules,
        "status_history": history,
        "stage_events": stage_events,
    }

def configure_stages(conn, work_id, payload, actor):
    work = _one(conn.execute(text("""
        select * from erp_work_orders where id=:id for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    if work["status"] not in {"RASCUNHO", "AGUARDANDO_O_S"}:
        raise ValueError("A parametrização inicial só pode ser alterada antes da ativação.")
    _ensure_stage_rows(conn, work_id, work)

    raw_choices = payload.get("stages") or {}
    if isinstance(raw_choices, list):
        raw_choices = {
            str(item.get("stage_code") or ""): item.get("input_code")
            for item in raw_choices if isinstance(item, dict)
        }
    if not isinstance(raw_choices, dict):
        raise ValueError("Informe as etapas em um objeto stage_code -> código.")

    valid_codes = {code for code, _, _ in STAGES}
    invalid_codes = [code for code in raw_choices if str(code).upper() not in valid_codes]
    if invalid_codes:
        raise ValueError("Etapas desconhecidas: " + ", ".join(invalid_codes) + ".")

    for code, raw_input in raw_choices.items():
        code = str(code).upper()
        input_code = str(raw_input or "?").strip().upper().replace(" ", "")
        input_code = {"NA": "N/A", "NÃOAPLICÁVEL": "N/A", "NAOAPLICAVEL": "N/A"}.get(
            _token(input_code).replace("_", ""), input_code
        )
        if input_code not in STAGE_INPUT_TO_STATUS:
            raise ValueError(f"Código inválido para {code}: use ?, P, N, S ou N/A.")
        status, applicable, parameterized = STAGE_INPUT_TO_STATUS[input_code]
        stage = _one(conn.execute(text("""
            select * from erp_work_order_stages
            where work_order_id=:work and stage_code=:code for update
        """), {"work": work_id, "code": code}))
        if not stage:
            raise ValueError(f"Etapa {code} não encontrada.")
        if applicable is None:
            applicable = _stage_applicable(code, work)
        started = datetime.utcnow() if status == "EM_ANDAMENTO" else None
        finished = datetime.utcnow() if status == "CONCLUÍDA" else None
        conn.execute(text("""
            update erp_work_order_stages
            set parametrizado=:parameterized,
                aplicavel=:applicable,
                status=:status,
                inicio=case when :status='EM_ANDAMENTO' then coalesce(inicio,:started)
                            when :status='PENDENTE' then null else inicio end,
                termino=case when :status='CONCLUÍDA' then coalesce(termino,:finished)
                             when :status in ('PENDENTE','EM_ANDAMENTO') then null else termino end
            where id=:id
        """), {
            "parameterized": parameterized, "applicable": applicable,
            "status": status, "started": started, "finished": finished,
            "id": stage["id"],
        })
        if (
            bool(stage.get("parametrizado")) != parameterized
            or stage_input_code(stage) != input_code
        ):
            conn.execute(text("""
                insert into erp_work_order_stage_events(
                    work_order_stage_id,action,status_anterior,novo_status,
                    operador,inicio,termino,observacao
                ) values(
                    :stage,'PARAMETRIZACAO',:old,:new,:actor,:started,:finished,:note
                )
            """), {
                "stage": stage["id"], "old": stage_input_code(stage),
                "new": input_code, "actor": actor, "started": started,
                "finished": finished,
                "note": f"Etapa parametrizada como {input_code}",
            })

    rows = [
        dict(row._mapping) for row in conn.execute(text("""
            select * from erp_work_order_stages
            where work_order_id=:id order by ordem
        """), {"id": work_id})
    ]
    pending = [row["stage_code"] for row in rows if not row["parametrizado"]]
    complete = bool(payload.get("complete"))
    if complete and pending:
        raise ValueError("Defina todas as etapas antes de ativar: " + ", ".join(pending) + ".")
    if complete:
        status_by_code = {row["stage_code"]: row["status"] for row in rows}
        for code, _, dependencies in STAGES:
            if status_by_code.get(code) not in {"EM_ANDAMENTO", "CONCLUÍDA"}:
                continue
            missing = [
                dep for dep in dependencies
                if status_by_code.get(dep) not in {"CONCLUÍDA", "NÃO_APLICÁVEL"}
            ]
            if missing:
                raise ValueError(
                    f"A etapa {code} não pode iniciar/concluir: pré-requisitos pendentes "
                    + ", ".join(missing) + "."
                )
        conn.execute(text("""
            update erp_work_orders
            set stage_configuration_status='CONCLUIDA',
                stage_configured_at=now(),
                stage_configured_by=:actor,
                updated_at=now(),
                version=version+1
            where id=:id
        """), {"id": work_id, "actor": actor})
    else:
        conn.execute(text("""
            update erp_work_orders
            set stage_configuration_status=case when :pending > 0 then 'PENDENTE' else 'CONCLUIDA' end,
                stage_configured_at=case when :pending > 0 then null else coalesce(stage_configured_at,now()) end,
                stage_configured_by=case when :pending > 0 then null else coalesce(stage_configured_by,:actor) end,
                updated_at=now(),
                version=version+1
            where id=:id
        """), {"id": work_id, "actor": actor, "pending": len(pending)})
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,after_data
        ) values(
            'WORK_ORDER',:id,'ETAPAS_PARAMETRIZADAS',:actor,'MES',
            jsonb_build_object('pendentes',:pending,'concluida',:complete)
        )
    """), {
        "id": work_id, "actor": actor, "pending": len(pending),
        "complete": complete and not pending,
    })
    return {
        "id": work_id,
        "complete": complete and not pending,
        "pending_stages": pending,
        "stages": [
            {"stage_code": row["stage_code"], "input_code": stage_input_code(row)}
            for row in rows
        ],
    }

def configure_and_activate(conn, work_id, payload, actor):
    """Parametrize and activate inside the caller's single database transaction."""
    configuration = configure_stages(
        conn,
        work_id,
        {**payload, "complete": True},
        actor,
    )
    activation = activate_work_order(conn, work_id, actor)
    return {
        **configuration,
        "activation": activation,
        "activated": True,
    }

def update_stage(conn, work_id, code, payload, actor):
    code=str(code).upper()
    work = _one(conn.execute(text("""
        select id,status,vehicle_entry_id
        from erp_work_orders
        where id=:work
        for update
    """), {"work": work_id}))
    if not work: raise ValueError('O.S. nao encontrada.')
    if work["status"] == "CONCLUIDA":
        raise ValueError("O.S. concluida tecnicamente deve ser reaberta antes de receber apontamentos.")
    stage=_one(conn.execute(text('select * from erp_work_order_stages where work_order_id=:work and stage_code=:code for update'),{'work':work_id,'code':code}))
    if not stage: raise ValueError('Etapa da O.S. nao encontrada.')
    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    if idempotency_key:
        replay = _one(conn.execute(text("""
            select id,novo_status from erp_work_order_stage_events where idempotency_key=:key
        """), {"key": idempotency_key}))
        if replay:
            return {"replayed": True, "status": replay["novo_status"]}
    raw_status=str(payload.get('input_code') or payload.get('status') or '').upper()
    normalized=''.join(c for c in unicodedata.normalize('NFKD', raw_status) if not unicodedata.combining(c))
    aliases={
        'N':'PENDENTE', 'P':'EM_ANDAMENTO', 'S':'CONCLUÍDA',
        'N/A':'NÃO_APLICÁVEL', 'NA':'NÃO_APLICÁVEL',
        'NAO_APLICAVEL':'NÃO_APLICÁVEL','CONCLUIDA':'CONCLUÍDA'
    }
    new=aliases.get(normalized, raw_status)
    valid={'PENDENTE','LIBERADA','EM_ANDAMENTO','CONCLUÍDA','NÃO_APLICÁVEL'}
    if new not in valid: raise ValueError('Status de etapa invalido.')
    # O apontamento é operacional: qualquer área pode iniciar ou concluir sua
    # própria etapa. As dependências permanecem apenas como orientação visual
    # no Kanban, sem bloquear o registro do operador.
    started = payload.get("inicio") or (
        datetime.utcnow()
        if new in {"EM_ANDAMENTO", "CONCLUÍDA"} and not stage["inicio"]
        else None
    )
    finished = payload.get("termino") or (
        datetime.utcnow()
        if new == "CONCLUÍDA" and not stage["termino"]
        else None
    )
    conn.execute(text('update erp_work_order_stages set parametrizado=true,aplicavel=:applicable,status=:status,responsavel=:responsavel,localizacao=:localizacao,inicio=coalesce(:inicio,inicio),termino=coalesce(:termino,termino),observacoes=:notes,bloqueio_motivo=:blocked where id=:id'),{'applicable':new!='NÃO_APLICÁVEL','status':new,'responsavel':payload.get('responsavel'),'localizacao':payload.get('localizacao'),'inicio':started,'termino':finished,'notes':str(payload.get('observacoes') or ''),'blocked':str(payload.get('bloqueio_motivo') or ''),'id':stage['id']})
    conn.execute(text('insert into erp_work_order_stage_events(work_order_stage_id,action,status_anterior,novo_status,operador,inicio,termino,localizacao,observacao,idempotency_key) values(:stage,:action,:old,:new,:actor,:inicio,:termino,:location,:note,:key)'),{'stage':stage['id'],'action':'APONTAMENTO','old':stage['status'],'new':new,'actor':actor,'inicio':started,'termino':finished,'location':payload.get('localizacao'),'note':str(payload.get('observacoes') or ''),'key':idempotency_key})
    next_status = work["status"]
    completion_at = finished or stage.get("termino") or datetime.utcnow()
    if work["status"] in {"ATIVA", "EM_PRODUÇÃO"}:
        if code == "LIBERAÇÃO" and new == "CONCLUÍDA":
            next_status = "FINALIZADA"
        elif new in {"EM_ANDAMENTO", "CONCLUÍDA"}:
            next_status = "EM_PRODUÇÃO"

    if next_status != work["status"]:
        conn.execute(text("""
            update erp_work_orders
            set status=:status,
                termino_producao=case when :status='FINALIZADA' then :completion_at else termino_producao end,
                finalizado_por=case when :status='FINALIZADA' then :actor else finalizado_por end,
                finalizado_at=case when :status='FINALIZADA' then :completion_at else finalizado_at end,
                updated_at=now(),
                version=version+1
            where id=:id
        """), {
            "id": work_id,
            "status": next_status,
            "completion_at": completion_at,
            "actor": actor,
        })
        conn.execute(text("update erp_vehicle_entries set status=:status where id=:id"), {
            "status": next_status,
            "id": work["vehicle_entry_id"],
        })
        transition_note = (
            "Início automático da produção pelo apontamento da etapa " + code
            if next_status == "EM_PRODUÇÃO"
            else "Produção finalizada automaticamente pela conclusão da etapa LIBERAÇÃO"
        )
        conn.execute(text("""
            insert into erp_work_order_status_history(
                work_order_id,status_anterior,novo_status,usuario,observacao
            ) values(:id,:old,:new,:actor,:note)
        """), {
            "id": work_id,
            "old": work["status"],
            "new": next_status,
            "actor": actor,
            "note": transition_note,
        })
        recalculate_work_order_sequences(conn, actor)
    return {"replayed": False, "status": new, "work_order_status": next_status}

def update_work_order_location(conn, work_id, location, actor, idempotency_key=None):
    stage = _one(conn.execute(text("""
        select *
        from erp_work_order_stages
        where work_order_id=:work
          and aplicavel=true
        order by
          case when status not in ('CONCLUÍDA','NÃO_APLICÁVEL') then 0 else 1 end,
          ordem
        limit 1
        for update
    """), {"work": work_id}))
    if not stage:
        raise ValueError("A O.S. não possui etapa aplicável para registrar localização.")
    key = str(idempotency_key or "").strip() or None
    if key:
        replay = _one(conn.execute(text("""
            select id from erp_work_order_stage_events where idempotency_key=:key
        """), {"key": key}))
        if replay:
            return {
                "replayed": True,
                "stage_code": stage["stage_code"],
                "localizacao": stage.get("localizacao") or "",
            }
    location = str(location or "").strip()
    conn.execute(text("""
        update erp_work_order_stages
        set localizacao=:location
        where id=:stage
    """), {"location": location, "stage": stage["id"]})
    conn.execute(text("""
        insert into erp_work_order_stage_events(
            work_order_stage_id,action,status_anterior,novo_status,operador,
            localizacao,observacao,idempotency_key
        ) values(
            :stage,'LOCALIZACAO',:status,:status,:actor,:location,
            :note,:key
        )
    """), {
        "stage": stage["id"],
        "status": stage["status"],
        "actor": actor,
        "location": location,
        "note": f"Localização atualizada para {location or 'não informada'}",
        "key": key,
    })
    return {
        "replayed": False,
        "stage_code": stage["stage_code"],
        "localizacao": location,
    }

def finalize(conn, work_id, actor, delivered=False, notes='', target_status=None, event_at=None):
    work=_one(conn.execute(text('select status,vehicle_entry_id from erp_work_orders where id=:id for update'),{'id':work_id}))
    if not work: raise ValueError('O.S. nao encontrada.')
    status = str(target_status or ('ENTREGUE' if delivered else 'FINALIZADA')).strip().upper()
    if status not in {'FINALIZADA', 'ENTREGUE', 'RETIRADA'}:
        raise ValueError('Status de encerramento inválido.')
    if work['status'] in {'CANCELADA', 'ARQUIVADA', 'CONCLUIDA'}:
        raise ValueError('O.S. cancelada ou arquivada não pode ser encerrada.')
    event_time = event_at or datetime.utcnow()
    conn.execute(text("""
        update erp_work_orders
        set status=:status,
            termino_producao=case when :status='FINALIZADA' then :event_time else termino_producao end,
            data_entrega=case when :status in ('ENTREGUE','RETIRADA') then :event_time else data_entrega end,
            finalizado_por=:actor,
            finalizado_at=:event_time,
            updated_at=now(),
            version=version+1
        where id=:id
    """),{'status':status,'actor':actor,'event_time':event_time,'id':work_id})
    conn.execute(text("update erp_vehicle_entries set status=:status where id=:id"), {"status": status, "id": work["vehicle_entry_id"]})
    conn.execute(text('insert into erp_work_order_status_history(work_order_id,status_anterior,novo_status,usuario,observacao) values(:id,:old,:new,:actor,:notes)'),{'id':work_id,'old':work['status'],'new':status,'actor':actor,'notes':notes})
    conn.execute(text("""
        insert into erp_audit_events(entity_type,entity_id,action,actor,origin,before_data,after_data,reason)
        values('WORK_ORDER',:id,:action,:actor,'MES',
               jsonb_build_object('status',cast(:old as text)),
               jsonb_build_object('status',cast(:new as text),'data_evento',cast(:event_time as text)),
               :notes)
    """), {
        "id": work_id, "action": status, "actor": actor, "old": work["status"],
        "new": status, "event_time": event_time, "notes": notes,
    })
    recalculate_work_order_sequences(conn, actor)
    return {"id": work_id, "status": status}

def technical_close_work_order(conn, work_id, actor, reason=""):
    """Conclude an O.S. in the management flow without deleting its history.

    ``technical_status`` keeps the closure rationale, while the canonical
    ``status`` becomes ``CONCLUIDA`` so all modules consistently stop offering
    this O.S. for new commitments.  Its prior status is retained exclusively
    for an explicit technical reopening.
    """
    work = _one(conn.execute(text("""
        select id,status,technical_status,technical_previous_status
        from erp_work_orders
        where id=:id
        for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    if work["status"] == "CANCELADA":
        raise ValueError("O.S. cancelada não pode receber conclusão técnica.")
    if work.get("technical_status") == "CONCLUIDA":
        return {
            "id": work_id,
            "status": work["status"],
            "technical_status": "CONCLUIDA",
            "replayed": True,
        }
    reason = str(reason or "").strip()
    conn.execute(text("""
        update erp_work_orders
        set status='CONCLUIDA',
            technical_previous_status=case
                when status <> 'CONCLUIDA' then status
                else technical_previous_status
            end,
            technical_status='CONCLUIDA',
            technical_closed_at=now(),
            technical_closed_by=:actor,
            technical_close_reason=:reason,
            updated_at=now(),
            version=version+1
        where id=:id
    """), {"id": work_id, "actor": actor, "reason": reason})
    conn.execute(text("""
        insert into erp_work_order_status_history(
            work_order_id,status_anterior,novo_status,usuario,observacao
        ) values(:id,:old,'CONCLUIDA',:actor,:reason)
    """), {
        "id": work_id, "old": work["status"], "actor": actor,
        "reason": reason,
    })
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,before_data,after_data,reason
        ) values(
            'WORK_ORDER',:id,'CONCLUSAO_TECNICA',:actor,'SUPRIMENTOS',
            jsonb_build_object('status',cast(:old as text),'technical_status','ABERTA'),
            jsonb_build_object('status','CONCLUIDA','technical_status','CONCLUIDA'),
            :reason
        )
    """), {
        "id": work_id, "actor": actor, "old": work["status"],
        "reason": reason,
    })
    recalculate_work_order_sequences(conn, actor)
    return {
        "id": work_id,
        "status": "CONCLUIDA",
        "technical_status": "CONCLUIDA",
        "replayed": False,
    }

def technical_reopen_work_order(conn, work_id, actor, reason=""):
    work = _one(conn.execute(text("""
        select id,status,technical_status,technical_previous_status
        from erp_work_orders
        where id=:id
        for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    if work.get("technical_status") != "CONCLUIDA":
        return {
            "id": work_id,
            "status": work["status"],
            "technical_status": "ABERTA",
            "replayed": True,
        }
    reason = str(reason or "").strip()
    restored_status = (
        str(work.get("technical_previous_status") or "").strip().upper()
        if work.get("status") == "CONCLUIDA"
        else work["status"]
    )
    if not restored_status:
        restored_status = "ATIVA"
    conn.execute(text("""
        update erp_work_orders
        set status=:status,
            technical_previous_status=null,
            technical_status='ABERTA',
            technical_closed_at=null,
            technical_closed_by=null,
            technical_close_reason='',
            updated_at=now(),
            version=version+1
        where id=:id
    """), {"id": work_id, "status": restored_status})
    conn.execute(text("""
        insert into erp_work_order_status_history(
            work_order_id,status_anterior,novo_status,usuario,observacao
        ) values(:id,:old,:new,:actor,:reason)
    """), {
        "id": work_id, "old": work["status"], "new": restored_status,
        "actor": actor, "reason": reason,
    })
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,before_data,after_data,reason
        ) values(
            'WORK_ORDER',:id,'REABERTURA_TECNICA',:actor,'SUPRIMENTOS',
            jsonb_build_object('status',cast(:old as text),'technical_status','CONCLUIDA'),
            jsonb_build_object('status',cast(:new as text),'technical_status','ABERTA'),
            :reason
        )
    """), {
        "id": work_id, "actor": actor, "old": work["status"],
        "new": restored_status, "reason": reason,
    })
    recalculate_work_order_sequences(conn, actor)
    return {
        "id": work_id,
        "status": restored_status,
        "technical_status": "ABERTA",
        "replayed": False,
    }

def reschedule(conn, work_id, new_date, reason, actor):
    if not _date_value(new_date):
        raise ValueError("Informe uma nova data de programação válida.")
    if not str(reason or "").strip():
        raise ValueError("O motivo da programação/reprogramação é obrigatório.")
    conn.execute(text('update erp_work_order_schedules set vigente=false where work_order_id=:id and vigente'),{'id':work_id})
    conn.execute(text('insert into erp_work_order_schedules(work_order_id,data_anterior,nova_data,motivo,usuario,vigente) values(:id,(select data_comercial_prevista from erp_work_orders where id=:id),:date,:reason,:actor,true)'),{'id':work_id,'date':new_date,'reason':reason,'actor':actor})
    conn.execute(text('update erp_work_orders set data_comercial_prevista=:date,updated_at=now() where id=:id'),{'id':work_id,'date':new_date})
    if _sequence_schema_ready(conn):
        conn.execute(text("""
            update erp_work_order_stages
               set data_planejada=:date,semana_planejada=to_char(:date::date,'IW')
             where work_order_id=:id
        """), {"id": work_id, "date": _date_value(new_date)})
    recalculate_work_order_sequences(conn, actor)
    return {"id": work_id, "data_comercial_prevista": _date_value(new_date)}
