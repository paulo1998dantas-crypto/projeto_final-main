"""New operational O.S./MES domain. Legacy MES tables remain read-only compatible."""
from datetime import datetime, date, timedelta, timezone
from functools import cmp_to_key
import json
from uuid import uuid4
import re
import unicodedata
from zoneinfo import ZoneInfo
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

# LIBERAÇÃO encerra o ciclo produtivo do veículo. ACESSÓRIO e PLOTAGEM podem
# ser executados depois da liberação e, por isso, não bloqueiam o fechamento
# nem ficam bloqueados quando a O.S. já estiver FINALIZADA.
POST_RELEASE_POINTING_STAGE_CODES = frozenset({"ACESSORIO", "PLOTAGEM"})

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
HISTORICAL_WORK_ORDER_FIELDS = tuple(
    field for field in WORK_ORDER_FIELDS if field != "cliente_nome"
)

LEAD_TIME_DAYS = {"LE": 45, "LAE": 45, "LB": 30, "LAB": 30}
VEHICLE_MODEL_TYPES = ("PACK", "STANDART", "ORIGINAL")
SERVICE_TYPES = (
    "TRANSFORMAÇÃO", "PÓS-VENDA", "INSTALAÇÃO_DE_ACESSÓRIO", "RETORNO", "OUTRO",
)
CLOSED_WORK_ORDER_STATUSES = frozenset(
    {"FINALIZADA", "ENTREGUE", "RETIRADA", "CANCELADA", "ARQUIVADA", "CONCLUIDA"}
)

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


def _vehicle_model_type(value, required=False):
    normalized = _token(value)
    if not normalized:
        if required:
            raise ValueError("Selecione o Modelo Veicular: PACK, STANDART ou ORIGINAL.")
        return None
    if normalized not in VEHICLE_MODEL_TYPES:
        raise ValueError("Modelo Veicular deve ser PACK, STANDART ou ORIGINAL.")
    return normalized


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


def canonical_service_type(value, default="TRANSFORMAÇÃO"):
    normalized = " ".join(
        _token(value).replace("_", " ").replace("-", " ").split()
    )
    aliases = {
        "": default,
        "TRANSFORMACAO": "TRANSFORMAÇÃO",
        "POS VENDA": "PÓS-VENDA",
        "POS VENDAS": "PÓS-VENDA",
        "INSTALACAO DE ACESSORIO": "INSTALAÇÃO_DE_ACESSÓRIO",
        "INSTALACAO ACESSORIO": "INSTALAÇÃO_DE_ACESSÓRIO",
        "INST ACESSORIO": "INSTALAÇÃO_DE_ACESSÓRIO",
        "RETORNO": "RETORNO",
        "OUTRO": "OUTRO",
        "OUTROS": "OUTRO",
    }
    result = aliases.get(normalized)
    if result not in SERVICE_TYPES:
        raise ValueError(
            "Tipo de serviço deve ser TRANSFORMAÇÃO, PÓS-VENDA, "
            "INSTALAÇÃO DE ACESSÓRIO, RETORNO ou OUTRO."
        )
    return result


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "sim", "yes"}


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


def work_order_is_archived(status, technical_previous_status=""):
    """Arquivamento operacional derivado do encerramento produtivo da O.S."""
    normalized_statuses = {
        _token(value).replace("_", " ").strip()
        for value in (status, technical_previous_status)
        if str(value or "").strip()
    }
    return any(
        value == "ARQUIVADA" or value.startswith(("FINALIZAD", "ENTREGUE"))
        for value in normalized_statuses
    )

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


def _optional_documento_os_id(payload):
    """Return the optional legacy O.S. document id without accepting guesses."""
    raw = (payload or {}).get("documento_os_id")
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise ValueError("documento_os_id deve ser um identificador numerico valido.")
    normalized = str(raw).strip()
    if not normalized.isdigit() or int(normalized) <= 0:
        raise ValueError("documento_os_id deve ser um identificador numerico valido.")
    return int(normalized)


def _normalize_os_number(value):
    token = re.sub(r"[^A-Z0-9]", "", _token(value))
    for prefix in ("ORDEMDESERVICO", "OS", "JI"):
        suffix = token[len(prefix):] if token.startswith(prefix) else ""
        if suffix.isdigit():
            return suffix
    return token


def _document_data(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _document_chassis(data):
    raw = data.get("chassi") or data.get("chassis") or data.get("chassi_completo") or ""
    normalized = _normalize_chassis(raw)
    if normalized in {"", "AG", "NA", "N/A", "0"}:
        return ""
    return normalized


def _compatible_document_chassis(document_chassis, vehicle_chassis):
    if not document_chassis:
        return True
    if document_chassis == vehicle_chassis:
        return True
    # Some preserved O.S. documents contain only the eight-character display
    # suffix.  It is accepted only as a suffix, never as a general substring.
    return (
        len(document_chassis) == 8
        and len(vehicle_chassis) >= 8
        and vehicle_chassis.endswith(document_chassis)
    )


def _link_suprimentos_os_document(
    conn, documento_os_id, work_id, numero_os, vehicle_entry_id, actor
):
    """Validate and bind one Suprimentos O.S. inside the caller transaction.

    The caller owns the transaction.  Consequently a validation/link failure
    also rolls back the work-order insert/update that preceded this function.
    """
    if documento_os_id is None:
        return None

    document = _one(conn.execute(text("""
        select id,tipo,numero,status,dados,erp_work_order_id
          from public.suprimentos_documentos
         where id=:document_id
         for update
    """), {"document_id": documento_os_id}))
    if not document:
        raise ValueError("Documento de O.S. informado nao foi encontrado.")
    if _token(document.get("tipo")) != "OS":
        raise ValueError("O documento informado nao e do tipo O.S.")
    if _token(document.get("status")) not in {"RASCUNHO", "EMITIDO"}:
        raise ValueError(
            "Somente documento de O.S. em rascunho ou emitido pode ser vinculado."
        )
    if _normalize_os_number(document.get("numero")) != _normalize_os_number(numero_os):
        raise ValueError(
            f"O numero do documento ({document.get('numero')}) nao corresponde a O.S. {numero_os}."
        )

    data = _document_data(document.get("dados"))
    column_link = str(document.get("erp_work_order_id") or "").strip()
    json_link = str(data.get("erp_work_order_id") or "").strip()
    existing_links = {value for value in (column_link, json_link) if value}
    if len(existing_links) > 1:
        raise ValueError("O documento possui vinculos ERP divergentes e exige reconciliacao.")
    current_link = next(iter(existing_links), "")
    if current_link and current_link != str(work_id):
        raise ValueError("Este documento de O.S. ja esta vinculado a outra O.S. operacional.")

    vehicle = _one(conn.execute(text("""
        select v.chassi
          from erp_vehicle_entries e
          join erp_vehicles v on v.id=e.vehicle_id
         where e.id=:entry_id
    """), {"entry_id": vehicle_entry_id}))
    if not vehicle:
        raise ValueError("Veiculo da O.S. operacional nao foi encontrado.")
    document_chassis = _document_chassis(data)
    vehicle_chassis = _normalize_chassis(vehicle.get("chassi"))
    if not _compatible_document_chassis(document_chassis, vehicle_chassis):
        raise ValueError("O chassi do documento nao corresponde ao veiculo da O.S. operacional.")

    conflict = _one(conn.execute(text("""
        select id,numero
          from public.suprimentos_documentos
         where id<>:document_id
           and (
                erp_work_order_id=cast(:work_id as uuid)
                or dados->>'erp_work_order_id'=:work_id
           )
         limit 1
         for update
    """), {
        "document_id": documento_os_id,
        "work_id": str(work_id),
    }))
    if conflict:
        raise ValueError(
            "A O.S. operacional ja esta vinculada ao documento "
            f"{conflict.get('numero') or conflict.get('id')}."
        )

    needs_update = column_link != str(work_id) or json_link != str(work_id)
    if needs_update:
        updated = conn.execute(text("""
            update public.suprimentos_documentos
               set erp_work_order_id=cast(:work_id as uuid),
                   dados=jsonb_set(
                       coalesce(dados,'{}'::jsonb),
                       '{erp_work_order_id}',
                       to_jsonb(cast(:work_id as text)),
                       true
                   ),
                   atualizado_por=:actor,
                   updated_at=now()
             where id=:document_id
               and (
                    erp_work_order_id is null
                    or erp_work_order_id=cast(:work_id as uuid)
               )
        """), {
            "document_id": documento_os_id,
            "work_id": str(work_id),
            "actor": actor,
        })
        if updated.rowcount != 1:
            raise ValueError("O documento foi vinculado por outro usuario. Atualize e tente novamente.")
        conn.execute(text("""
            insert into erp_audit_events(
                entity_type,entity_id,action,actor,origin,after_data
            ) values(
                'WORK_ORDER',:work_id,'SUPRIMENTOS_DOCUMENT_LINKED',:actor,'SUPRIMENTOS',
                jsonb_build_object(
                    'documento_os_id',cast(:document_id as bigint),
                    'documento_numero',:document_number
                )
            )
        """), {
            "work_id": work_id,
            "actor": actor,
            "document_id": documento_os_id,
            "document_number": str(document.get("numero") or ""),
        })
    return documento_os_id

def _stage_applicable(code, work):
    not_applicable = {"", "NAO", "NA", "N/A", "SEM"}
    return not (
        (
            code == "A/C"
            and (
                _token(work.get("ar_condicionado")) in not_applicable
                or _token(work.get("tipo_sistema_ar")) in not_applicable
            )
        )
        or (code == "BCO" and _token(work.get("conjunto_bancos")) in not_applicable)
        or (code == "ACESSÓRIO" and _token(work.get("acessorio")) in not_applicable)
        or (code == "PLOTAGEM" and _token(work.get("plotagem")) in not_applicable)
    )


def _can_recalculate_stage_applicability(work, stage):
    """Allow field-derived applicability only before an explicit PCP choice.

    Once a stage has been parametrized, its current value is an operational
    decision.  Editing descriptive O.S. fields in Suprimentos must not turn a
    manually selected N/A back into PENDENTE (or the reverse).
    """
    return (
        work.get("stage_configuration_status") != "CONCLUIDA"
        and not bool(stage.get("parametrizado"))
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


def productive_cycle_window(work, stages):
    """Return the real productive-cycle bounds for one work order.

    A stage can have a historical finish timestamp without the vehicle being
    finished.  The cycle end is therefore exposed only after the work order
    has actually entered a final operational status.  This avoids presenting a
    premature end of cycle when LIBERAÇÃO was pointed incorrectly or when a
    manual closing form is merely open in the browser.
    """
    starts = [stage.get("inicio") for stage in stages if stage.get("inicio")]
    start = min(starts) if starts else None
    status = _token(work.get("status"))
    if status not in {"FINALIZADA", "ENTREGUE", "RETIRADA", "CANCELADA"}:
        return start, None
    release = next(
        (stage for stage in stages if _token(stage.get("stage_code")) == "LIBERACAO"),
        None,
    )
    # Cancellation is terminal but must not be mistaken for production completion.
    end = work.get("termino_producao") or (
        work.get("finalizado_at") if status == "CANCELADA" else None
    )
    if not end and release and _token(release.get("status")) == "CONCLUIDA":
        end = release.get("termino")
    return start, end

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


def _pre_os_stage_schema_ready(conn):
    """Allow a safe additive rollout before every app instance is restarted."""
    try:
        result = conn.execute(text(
            "select to_regclass('public.erp_vehicle_entry_stages') is not null "
            "and to_regclass('public.erp_vehicle_entry_stage_events') is not null"
        ))
        return bool(result.scalar())
    except (AttributeError, TypeError):
        # Test doubles and an old database snapshot do not expose the new schema.
        return False


def _stage_pause_schema_ready(conn):
    """Keep application rollout compatible until the additive migration lands."""
    try:
        return bool(conn.execute(text(
            "select to_regclass('public.erp_stage_time_pauses') is not null "
            "and to_regclass('public.erp_stage_time_sessions') is not null"
        )).scalar())
    except (AttributeError, TypeError):
        return False


def _ensure_entry_stage_rows(conn, entry_id):
    if not _pre_os_stage_schema_ready(conn):
        return False
    for code, order, _ in STAGES:
        conn.execute(text("""
            insert into erp_vehicle_entry_stages(
                id,vehicle_entry_id,stage_code,aplicavel,status,ordem,parametrizado
            ) values(
                :id,:entry,:code,true,'PENDENTE',:order,false
            )
            on conflict(vehicle_entry_id,stage_code) do nothing
        """), {
            "id": _id(), "entry": entry_id, "code": code, "order": order,
        })
    return True


def _promote_entry_stage_pointings(conn, entry_id, work_id, actor):
    """Move preliminary ITEM pointings into the canonical O.S. transactionally.

    The source rows and events remain immutable evidence.  Transfer references
    make retries idempotent and the O.S. stage becomes the only writable state
    after the work order has been opened.
    """
    if not _pre_os_stage_schema_ready(conn):
        return 0
    preliminary = [dict(row._mapping) for row in conn.execute(text("""
        select *
        from erp_vehicle_entry_stages
        where vehicle_entry_id=:entry and parametrizado
        order by ordem
        for update
    """), {"entry": entry_id})]
    promoted = 0
    for source in preliminary:
        target = _one(conn.execute(text("""
            select * from erp_work_order_stages
            where work_order_id=:work and stage_code=:code
            for update
        """), {"work": work_id, "code": source["stage_code"]}))
        if not target:
            raise ValueError(
                f"A etapa {source['stage_code']} não foi criada na O.S.; a abertura foi cancelada."
            )
        conn.execute(text("""
            update erp_work_order_stages
               set parametrizado=true,
                   aplicavel=:applicable,
                   status=:status,
                   responsavel=:responsible,
                   localizacao=:location,
                   inicio=:started,
                   termino=:finished,
                   observacoes=:notes
             where id=:id
        """), {
            "id": target["id"],
            "applicable": source["aplicavel"],
            "status": source["status"],
            "responsible": source.get("responsavel"),
            "location": source.get("localizacao"),
            "started": source.get("inicio"),
            "finished": source.get("termino"),
            "notes": source.get("observacoes") or "",
        })
        events = [dict(row._mapping) for row in conn.execute(text("""
            select * from erp_vehicle_entry_stage_events
            where vehicle_entry_stage_id=:stage
            order by created_at,id
            for update
        """), {"stage": source["id"]})]
        last_target_event_id = None
        for event in events:
            transfer_key = f"PRE_OS:{event['id']}"
            inserted = _one(conn.execute(text("""
                insert into erp_work_order_stage_events(
                    work_order_stage_id,action,status_anterior,novo_status,
                    operador,inicio,termino,localizacao,observacao,
                    idempotency_key,created_at
                ) values(
                    :stage,'APONTAMENTO_PRE_OS',:old,:new,:actor,
                    :started,:finished,:location,:note,:key,:created_at
                )
                on conflict(idempotency_key) do nothing
                returning id
            """), {
                "stage": target["id"], "old": event.get("status_anterior"),
                "new": event["novo_status"], "actor": event["operador"],
                "started": event.get("inicio"), "finished": event.get("termino"),
                "location": event.get("localizacao"), "note": event.get("observacao") or "",
                "key": transfer_key, "created_at": event["created_at"],
            }))
            if inserted:
                last_target_event_id = inserted["id"]
            else:
                existing = _one(conn.execute(text("""
                    select id from erp_work_order_stage_events
                    where idempotency_key=:key
                """), {"key": transfer_key}))
                last_target_event_id = existing["id"] if existing else None
            conn.execute(text("""
                update erp_vehicle_entry_stage_events
                   set transferred_to_event_id=:target,transferred_at=coalesce(transferred_at,now())
                 where id=:id
            """), {"target": last_target_event_id, "id": event["id"]})
        conn.execute(text("""
            update erp_vehicle_entry_stages
               set transferred_to_work_order_stage_id=:target,
                   transferred_at=coalesce(transferred_at,now()),
                   transferred_by=coalesce(transferred_by,:actor),
                   updated_at=now()
             where id=:id
        """), {"target": target["id"], "actor": actor, "id": source["id"]})
        if _stage_pause_schema_ready(conn):
            conn.execute(text("""
                update erp_stage_time_pauses
                   set work_order_stage_id=:target,
                       vehicle_entry_stage_id=null,
                       updated_at=now()
                 where vehicle_entry_stage_id=:source
            """), {"target": target["id"], "source": source["id"]})
            conn.execute(text("""
                update erp_stage_time_sessions
                   set work_order_stage_id=:target,
                       vehicle_entry_stage_id=null,
                       updated_at=now()
                 where vehicle_entry_stage_id=:source
            """), {"target": target["id"], "source": source["id"]})
        promoted += 1
    if promoted:
        pending = conn.execute(text("""
            select count(*) from erp_work_order_stages
            where work_order_id=:work and not parametrizado
        """), {"work": work_id}).scalar_one()
        conn.execute(text("""
            update erp_work_orders
               set stage_configuration_status=case when :pending=0 then 'CONCLUIDA' else 'PENDENTE' end,
                   stage_configured_at=case when :pending=0 then now() else null end,
                   stage_configured_by=case when :pending=0 then :actor else null end,
                   updated_at=now()
             where id=:work
        """), {"work": work_id, "pending": pending, "actor": actor})
        conn.execute(text("""
            insert into erp_audit_events(
                entity_type,entity_id,action,actor,origin,after_data
            ) values(
                'WORK_ORDER',:work,'APONTAMENTOS_PRE_OS_PROMOVIDOS',:actor,'MES',
                jsonb_build_object('vehicle_entry_id',:entry,'etapas',:promoted)
            )
        """), {
            "work": work_id, "entry": entry_id, "promoted": promoted, "actor": actor,
        })
    return promoted

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
               w.transformacao,w.ar_condicionado,w.conjunto_bancos,e.cliente_nome,
               e.item_number,seq.prioridade_manual
          from erp_work_orders w
          join erp_vehicle_entries e on e.id=w.vehicle_entry_id
          left join erp_work_order_sequences seq on seq.work_order_id=w.id
         where w.status in ('ATIVA','EM_PRODUÇÃO')
           and w.is_current=true
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
                select id from erp_work_orders
                where status in ('ATIVA','EM_PRODUÇÃO') and is_current=true
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
               w.numero_os,w.status,w.linha,e.cliente_nome,w.transformacao,
               e.item_number,v.chassi,v.marca,v.modelo,v.versao
          from erp_work_order_sequences seq
          join erp_work_orders w on w.id=seq.work_order_id
          join erp_vehicle_entries e on e.id=w.vehicle_entry_id
          join erp_vehicles v on v.id=e.vehicle_id
         where seq.ativo=true and w.status in ('ATIVA','EM_PRODUÇÃO')
           and w.is_current=true
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
    origin = _token(payload.get("origem") or "MANUAL")
    modelo_veicular = _vehicle_model_type(
        payload.get("modelo_veicular"),
        required=not origin.startswith("LEGACY"),
    )
    tipo_preliminar = canonical_service_type(payload.get("tipo_preliminar"))
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
    entry_id=_id(); row=_one(conn.execute(text("insert into erp_vehicle_entries(id,vehicle_id,data_chegada,cliente_id,cliente_nome,origem,observacoes,avarias,modelo_veicular,tipo_preliminar,criado_por,status) values(:id,:vehicle,:arrival,:client_id,:client,:origin,:notes,:damage,:modelo_veicular,:tipo_preliminar,:actor,'AGUARDANDO_O_S') returning item_number"),{'id':entry_id,'vehicle':vehicle_id,'arrival':payload.get('data_chegada') or datetime.utcnow(),'client_id':payload.get('cliente_id'),'client':str(payload.get('cliente_nome') or ''),'origin':str(payload.get('origem') or 'MANUAL'),'notes':str(payload.get('observacoes') or ''),'damage':str(payload.get('avarias') or ''),'modelo_veicular':modelo_veicular,'tipo_preliminar':tipo_preliminar,'actor':actor}))
    _ensure_entry_stage_rows(conn, entry_id)
    reconcile_purchase_order_allocations(conn, actor, "ENTRADA_VEICULO")
    return {'id':entry_id,'vehicle_id':vehicle_id,'item_number':int(row['item_number']),'tipo_preliminar':tipo_preliminar}


def update_vehicle_entry(
    conn, entry_id, payload, actor, allow_closed_type_correction=False,
    audit_origin="MES",
):
    """Correct the arrival record and the physical vehicle without losing history."""
    current = _one(conn.execute(text("""
        select e.*,v.chassi,v.marca,v.modelo,v.versao,v.mmv,
               v.chassi_completo,v.legacy_chassi_reduzido
          from erp_vehicle_entries e
          join erp_vehicles v on v.id=e.vehicle_id
         where e.id=:id
         for update
    """), {"id": entry_id}))
    if not current:
        raise ValueError("Entrada de veiculo nao encontrada.")

    before = {
        key: current.get(key)
        for key in (
            "item_number", "vehicle_id", "chassi", "marca", "modelo", "versao", "mmv",
            "data_chegada", "cliente_nome", "observacoes", "avarias", "modelo_veicular",
            "tipo_preliminar", "status",
        )
    }
    vehicle_values = {
        key: str(payload.get(key) if key in payload else current.get(key) or "").strip()
        for key in ("marca", "modelo", "versao", "mmv")
    }
    current_chassis = _normalize_chassis(current.get("chassi"))
    new_chassis = _normalize_chassis(payload.get("chassi", current_chassis))
    chassis_changed = new_chassis != current_chassis
    if chassis_changed and not _is_complete_vin(new_chassis):
        raise ValueError("Informe o chassi completo com 17 caracteres.")
    if chassis_changed:
        conn.execute(
            text("select pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"erp_vehicle_vin:{new_chassis}"},
        )
        duplicate = _one(conn.execute(text("""
            select id from erp_vehicles
             where chassi=:chassi and id<>:vehicle_id
             for update
        """), {"chassi": new_chassis, "vehicle_id": current["vehicle_id"]}))
        if duplicate:
            raise ValueError("O chassi informado ja pertence a outro veiculo.")

    arrival = payload.get("data_chegada", current.get("data_chegada"))
    if arrival in (None, ""):
        raise ValueError("A data e hora de chegada sao obrigatorias.")
    entry_values = {
        "data_chegada": arrival,
        "cliente_nome": str(payload.get("cliente_nome") if "cliente_nome" in payload else current.get("cliente_nome") or "").strip(),
        "observacoes": str(payload.get("observacoes") if "observacoes" in payload else current.get("observacoes") or "").strip(),
        "avarias": str(payload.get("avarias") if "avarias" in payload else current.get("avarias") or "NAO").strip().upper(),
        "modelo_veicular": _vehicle_model_type(
            payload.get("modelo_veicular") if "modelo_veicular" in payload else current.get("modelo_veicular")
        ),
        "tipo_preliminar": (
            canonical_service_type(payload.get("tipo_preliminar"))
            if "tipo_preliminar" in payload
            else current.get("tipo_preliminar")
        ),
    }
    if _token(entry_values["avarias"]) not in {"SIM", "NAO", "N/A"}:
        raise ValueError("Avarias deve ser SIM, NAO ou N/A.")

    sync_work_order_type = _truthy(payload.get("atualizar_tipo_servico_os"))
    current_work = None
    work_order_type_changed = False
    if sync_work_order_type:
        current_work = _one(conn.execute(text("""
            select id,numero_os,status,tipo_servico
              from erp_work_orders
             where vehicle_entry_id=:entry and is_current=true
             for update
        """), {"entry": entry_id}))
        if not current_work:
            raise ValueError("Esta entrada ainda não possui O.S. para corrigir o tipo de serviço.")
        work_order_type_changed = (
            canonical_service_type(current_work.get("tipo_servico"))
            != entry_values["tipo_preliminar"]
        )
        work_status = str(current_work.get("status") or "").strip().upper()
        if work_order_type_changed and work_status in CLOSED_WORK_ORDER_STATUSES:
            if not allow_closed_type_correction:
                raise ValueError(
                    "Somente PCP ou ADMIN pode corrigir o tipo de uma O.S. encerrada."
                )
            if not str(payload.get("motivo") or "").strip():
                raise ValueError("Informe o motivo da correção histórica do tipo de serviço.")

    after = {
        **before,
        **vehicle_values,
        **entry_values,
        "chassi": new_chassis,
    }
    comparable_before = {key: str(value or "") for key, value in before.items()}
    comparable_after = {key: str(value or "") for key, value in after.items()}
    if comparable_before == comparable_after and not work_order_type_changed:
        return {"id": str(entry_id), "item_number": int(current["item_number"]), "replayed": True}

    complete_chassis = _is_complete_vin(new_chassis)
    legacy_chassis = (
        None
        if complete_chassis
        else str(current.get("legacy_chassi_reduzido") or new_chassis or "").strip() or None
    )
    conn.execute(text("""
        update erp_vehicles
           set chassi=:chassi,marca=:marca,modelo=:modelo,versao=:versao,mmv=:mmv,
               chassi_completo=:chassi_completo,
               legacy_chassi_reduzido=:legacy_chassi_reduzido
         where id=:vehicle_id
    """), {
        "vehicle_id": current["vehicle_id"],
        "chassi": new_chassis,
        "chassi_completo": complete_chassis,
        "legacy_chassi_reduzido": legacy_chassis,
        **vehicle_values,
    })
    conn.execute(text("""
        update erp_vehicle_entries
           set data_chegada=:data_chegada,cliente_nome=:cliente_nome,
                observacoes=:observacoes,avarias=:avarias,modelo_veicular=:modelo_veicular,
                tipo_preliminar=:tipo_preliminar
         where id=:id
    """), {"id": entry_id, **entry_values})
    conn.execute(text("""
        update erp_work_orders
           set cliente_nome=:cliente_nome,updated_at=now(),version=version+1
         where vehicle_entry_id=:id
           and cliente_nome is distinct from :cliente_nome
    """), {"id": entry_id, "cliente_nome": entry_values["cliente_nome"]})
    if work_order_type_changed:
        conn.execute(text("""
            update erp_work_orders
               set tipo_servico=:tipo_servico,updated_at=now(),version=version+1
             where id=:work_id
        """), {
            "work_id": current_work["id"],
            "tipo_servico": entry_values["tipo_preliminar"],
        })
        conn.execute(text("""
            insert into erp_audit_events(
                entity_type,entity_id,action,actor,origin,before_data,after_data,reason
            ) values(
                'WORK_ORDER',:work_id,'TIPO_SERVICO_OS_CORRIGIDO',:actor,'MES',
                jsonb_build_object(
                    'numero_os',cast(:numero_os as text),
                    'status',cast(:status as text),
                    'tipo_servico',cast(:tipo_anterior as text)
                ),
                jsonb_build_object(
                    'numero_os',cast(:numero_os as text),
                    'status',cast(:status as text),
                    'tipo_servico',cast(:tipo_novo as text)
                ),
                :reason
            )
        """), {
            "work_id": current_work["id"], "actor": actor,
            "numero_os": current_work.get("numero_os"),
            "status": current_work.get("status"),
            "tipo_anterior": current_work.get("tipo_servico"),
            "tipo_novo": entry_values["tipo_preliminar"],
            "reason": str(payload.get("motivo") or "Correção do tipo de serviço da O.S."),
        })
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,before_data,after_data,reason
        ) values(
            'VEHICLE_ENTRY',:id,'ENTRADA_VEICULO_ATUALIZADA',:actor,:origin,
            cast(:before_data as jsonb),cast(:after_data as jsonb),:reason
        )
    """), {
        "id": entry_id,
        "actor": actor,
        "origin": audit_origin,
        "before_data": json.dumps(before, default=str, ensure_ascii=False),
        "after_data": json.dumps(after, default=str, ensure_ascii=False),
        "reason": str(payload.get("motivo") or "Correcao dos dados informados na entrada do veiculo."),
    })
    reconcile_purchase_order_allocations(conn, actor, "ENTRADA_VEICULO")
    return {
        "id": str(entry_id), "vehicle_id": str(current["vehicle_id"]),
        "item_number": int(current["item_number"]), "replayed": False,
        "work_order_type_updated": work_order_type_changed,
        **after,
    }

def withdraw_vehicle_entry(conn, entry_id, actor, reason="", event_at=None):
    """Register a withdrawal from the yard before an O.S. is opened.

    This is an entry lifecycle event, never a delivery.  It preserves the
    original ITEM and preliminary pointings for audit and requires a new entry
    when the physical vehicle returns in the future.
    """
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("Informe o motivo da retirada do veiculo.")
    entry = _one(conn.execute(text("""
        select id,item_number,status
        from erp_vehicle_entries
        where id=:id
        for update
    """), {"id": entry_id}))
    if not entry:
        raise ValueError("Entrada de veiculo nao encontrada.")
    work = _one(conn.execute(text("""
        select id,numero_os
        from erp_work_orders
        where vehicle_entry_id=:entry and is_current=true
        for update
    """), {"entry": entry_id}))
    if work:
        raise ValueError(
            f"A entrada ja possui a O.S. {work['numero_os']}. Registre a retirada na O.S."
        )

    old_status = str(entry.get("status") or "").strip().upper()
    if old_status == "RETIRADA":
        return {
            "id": str(entry_id), "item_number": int(entry["item_number"]),
            "status": "RETIRADA", "replayed": True,
        }
    if old_status != "AGUARDANDO_O_S":
        raise ValueError("Somente veiculos aguardando O.S. podem ser retirados sem O.S.")

    event_time = event_at or datetime.utcnow()
    conn.execute(text("""
        update erp_vehicle_entries
        set status='RETIRADA'
        where id=:id
    """), {"id": entry_id})
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,before_data,after_data,reason
        ) values(
            'VEHICLE_ENTRY',:id,'RETIRADA_SEM_OS',:actor,'MES',
            jsonb_build_object('status',cast(:old_status as text)),
            jsonb_build_object('status','RETIRADA','data_evento',cast(:event_time as text)),
            :reason
        )
    """), {
        "id": entry_id, "actor": actor, "old_status": old_status,
        "event_time": event_time, "reason": reason,
    })
    return {
        "id": str(entry_id), "item_number": int(entry["item_number"]),
        "status": "RETIRADA", "replayed": False,
    }


def create_work_order(conn, entry_id, payload, actor):
    documento_os_id = _optional_documento_os_id(payload)
    entry=_one(conn.execute(text('select item_number,data_chegada,status,cliente_nome,tipo_preliminar from erp_vehicle_entries where id=:id for update'),{'id':entry_id}))
    if not entry: raise ValueError('Entrada de veiculo nao encontrada.')
    if str(entry.get('status') or '').strip().upper() == 'RETIRADA':
        raise ValueError('Veiculo retirado sem O.S. Nao e possivel abrir uma O.S. nesta entrada; registre uma nova entrada no retorno do veiculo.')
    forecast_id=str(payload.get('forecast_id') or '').strip() or None
    current=_one(conn.execute(text("""
        select id,numero_os,status,revision_number,is_current,supersedes_work_order_id
        from erp_work_orders
        where vehicle_entry_id=:id and is_current=true
        for update
    """),{'id':entry_id}))
    create_replacement = payload.get('create_replacement') is True
    expected_previous_id = str(payload.get('supersedes_work_order_id') or '').strip()
    if current:
        if create_replacement:
            if str(current.get('status') or '').strip().upper() != 'CANCELADA':
                # A retry after the replacement was committed reaches its new
                # current row. Return it instead of creating a duplicate.
                if (
                    expected_previous_id
                    and str(current.get('supersedes_work_order_id') or '') == expected_previous_id
                ):
                    result = {
                        'id':str(current['id']), 'numero_os':current['numero_os'],
                        'revision_number':int(current.get('revision_number') or 1),
                        'replayed':True,
                    }
                    return result
                raise ValueError('Somente uma O.S. cancelada pode receber uma nova revisao no mesmo ITEM.')
            if expected_previous_id and str(current.get('id')) != expected_previous_id:
                raise ValueError('A O.S. cancelada foi alterada por outro usuario. Atualize a tela.')
        else:
            _ensure_stage_rows(conn, current['id'], {})
            _promote_entry_stage_pointings(conn, entry_id, current['id'], actor)
            if documento_os_id is not None:
                _link_suprimentos_os_document(
                    conn, documento_os_id, current['id'], current['numero_os'], entry_id, actor
                )
            if forecast_id:
                linked_forecast=_one(conn.execute(text("""
                    select id,codigo from suprimentos_forecasts
                    where work_order_id=:work_id
                """), {'work_id': current['id']}))
                if not linked_forecast or str(linked_forecast['id']) != forecast_id:
                    raise ValueError('Esta entrada ja possui O.S.; o Forecast nao pode ser trocado apos a abertura.')
                result = {
                    'id':str(current['id']), 'numero_os':current['numero_os'],'replayed':True,
                    'forecast_id':str(linked_forecast['id']),'forecast_codigo':linked_forecast['codigo'],
                    'revision_number':int(current.get('revision_number') or 1),
                }
                if documento_os_id is not None:
                    result['documento_os_id'] = documento_os_id
                return result
            result = {
                'id':str(current['id']),'numero_os':current['numero_os'],'replayed':True,
                'revision_number':int(current.get('revision_number') or 1),
            }
            if documento_os_id is not None:
                result['documento_os_id'] = documento_os_id
            return result

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

    work_id=_id(); number=str(entry['item_number'])
    previous_work_id = current['id'] if current and create_replacement else None
    revision_number = int(current.get('revision_number') or 1) + 1 if previous_work_id else 1
    fields={'tipo_servico':canonical_service_type(entry.get('tipo_preliminar')),'proposta_numero':'','data_aprovacao':None,'vendedor':'','mercado':'','cliente_nome':'','municipio':'','uf':'','tipo_veiculo':'','linha':'','transformacao':'','transformacao_codigo':'','codigo_banco':'','conjunto_bancos':'','acessibilidade':'','lotacao':'','ar_condicionado':'','tipo_sistema_ar':'','ar_quente':'','acessorio':'','plotagem':'','data_comercial_prevista':None}
    fields.update({
        key: _work_field_value(key, value)
        for key, value in payload.items()
        if key in fields
    })
    fields['tipo_servico'] = canonical_service_type(fields.get('tipo_servico'))
    # O cliente pertence à entrada do veículo. A O.S. apenas mantém uma cópia
    # compatível para documentos e consultas legadas, sem aceitar divergência.
    fields['cliente_nome'] = str(entry.get('cliente_nome') or '').strip()
    # Opening the O.S. is already its business emission.  MES stage
    # parametrization is a later, independent step and remains represented by
    # stage_configuration_status=PENDENTE until the PCP configures all stages.
    # AGUARDANDO_O_S is the existing pre-activation state accepted throughout
    # the transition, so this change avoids introducing a second lifecycle.
    if previous_work_id:
        demoted = conn.execute(text("""
            update erp_work_orders
               set is_current=false,updated_at=now()
             where id=:previous and is_current=true and status='CANCELADA'
        """), {'previous': previous_work_id})
        if demoted.rowcount != 1:
            raise ValueError('A O.S. cancelada foi alterada por outro usuario. Atualize a tela.')
    conn.execute(text("""insert into erp_work_orders(id,vehicle_entry_id,numero_os,tipo_servico,proposta_numero,data_aprovacao,vendedor,mercado,cliente_nome,municipio,uf,tipo_veiculo,linha,transformacao_codigo,transformacao,codigo_banco,conjunto_bancos,acessibilidade,lotacao,ar_condicionado,tipo_sistema_ar,ar_quente,acessorio,plotagem,data_comercial_prevista,criado_por,status,revision_number,is_current,supersedes_work_order_id) values(:id,:entry,:number,:tipo_servico,:proposta_numero,:data_aprovacao,:vendedor,:mercado,:cliente_nome,:municipio,:uf,:tipo_veiculo,:linha,:transformacao_codigo,:transformacao,:codigo_banco,:conjunto_bancos,:acessibilidade,:lotacao,:ar_condicionado,:tipo_sistema_ar,:ar_quente,:acessorio,:plotagem,:data_comercial_prevista,:actor,'AGUARDANDO_O_S',:revision,true,:previous)"""),{'id':work_id,'entry':entry_id,'number':number,'actor':actor,'revision':revision_number,'previous':previous_work_id,**fields})
    history_note = (
        f'O.S. emitida como revisao {revision_number}; substitui a O.S. cancelada {number} sem apagar o historico.'
        if previous_work_id else 'O.S. emitida; parametrizacao MES pendente'
    )
    conn.execute(text("insert into erp_work_order_status_history(work_order_id,novo_status,usuario,observacao) values(:id,'AGUARDANDO_O_S',:actor,:note)"),{'id':work_id,'actor':actor,'note':history_note})
    _ensure_stage_rows(conn, work_id, fields)
    # Preliminary pointings belong to the first operational demand.  A new
    # revision after cancellation starts clean; the cancelled revision keeps
    # its own stages and events as immutable history.
    promoted_stages = 0 if previous_work_id else _promote_entry_stage_pointings(conn, entry_id, work_id, actor)
    if fields["data_comercial_prevista"]:
        conn.execute(text("""
            insert into erp_work_order_schedules(
                work_order_id,data_anterior,nova_data,motivo,usuario,vigente
            ) values(:id,null,:date,'PROGRAMAÇÃO INICIAL',:actor,true)
        """), {"id": work_id, "date": fields["data_comercial_prevista"], "actor": actor})
    conn.execute(text("""
        update erp_vehicle_entries
           set status='O_S_ABERTA',tipo_preliminar=:tipo_servico
         where id=:id
    """), {"id": entry_id, "tipo_servico": fields["tipo_servico"]})
    if previous_work_id:
        conn.execute(text("""
            insert into erp_audit_events(
                entity_type,entity_id,action,actor,origin,before_data,after_data,reason
            ) values(
                'WORK_ORDER',:work,'NOVA_REVISAO_APOS_CANCELAMENTO',:actor,'GESTAO_OS',
                jsonb_build_object('work_order_id',cast(:previous as text),'numero_os',:number,'status','CANCELADA'),
                jsonb_build_object('work_order_id',cast(:work as text),'numero_os',:number,'revision_number',:revision),
                'Nova demanda vinculada ao mesmo ITEM e veiculo; historico cancelado preservado.'
            )
        """), {
            'work': work_id, 'previous': previous_work_id, 'number': number,
            'revision': revision_number, 'actor': actor,
        })
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
    if documento_os_id is not None:
        _link_suprimentos_os_document(
            conn, documento_os_id, work_id, number, entry_id, actor
        )
    result = {
        'id':work_id, 'numero_os':number, 'replayed':False,
        'status':'AGUARDANDO_O_S',
        'revision_number':revision_number,
        'supersedes_work_order_id':str(previous_work_id) if previous_work_id else None,
        'stage_configuration_status':'CONCLUIDA' if promoted_stages == len(STAGES) else 'PENDENTE',
        'promoted_pre_os_stages':promoted_stages,
        'forecast_id':forecast_id,
        'forecast_codigo':forecast['codigo'] if forecast else None,
    }
    if documento_os_id is not None:
        result['documento_os_id'] = documento_os_id
    reconcile_purchase_order_allocations(conn, actor, "ABERTURA_OS")
    return result

def update_work_order(conn, work_id, payload, actor):
    documento_os_id = _optional_documento_os_id(payload)
    work = _one(conn.execute(text("""
        select w.*,e.data_chegada,e.cliente_nome as entry_client from erp_work_orders w
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
    fields["cliente_nome"] = str(work.get("entry_client") or "").strip()
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
                   semana_planejada=to_char(cast(:date as date),'IW')
             where work_order_id=:id
        """), {"id": work_id, "date": fields["data_comercial_prevista"]})
    if not is_draft:
        for code, _, _ in STAGES:
            stage = _one(conn.execute(text("""
                select id,aplicavel,status,parametrizado from erp_work_order_stages
                where work_order_id=:work and stage_code=:code for update
            """), {"work": work_id, "code": code}))
            if not stage:
                continue
            if not _can_recalculate_stage_applicability(work, stage):
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
    if documento_os_id is not None:
        _link_suprimentos_os_document(
            conn, documento_os_id, work_id, work["numero_os"],
            work["vehicle_entry_id"], actor,
        )
    if not is_draft:
        recalculate_work_order_sequences(conn, actor)
    result = {
        "id": work_id,
        "numero_os": work["numero_os"],
        "data_comercial_prevista": fields["data_comercial_prevista"],
    }
    if documento_os_id is not None:
        result["documento_os_id"] = documento_os_id
    reconcile_purchase_order_allocations(conn, actor, "EDICAO_OS")
    return result


def correct_work_order_bank(conn, work_id, payload, actor):
    """Correct only the bank reference without reopening a closed work order."""
    bank_code = str(payload.get("codigo_banco") or "").strip()
    bank_description = str(payload.get("conjunto_bancos") or "").strip()
    reason = str(payload.get("motivo") or payload.get("reason") or "").strip()
    if not bank_code:
        raise ValueError("Informe o código do banco ou N/A.")
    if _token(bank_code) == "N/A":
        bank_code = "N/A"
        bank_description = "N/A"
    elif not bank_description:
        raise ValueError("O código do banco precisa ter uma descrição válida do Cadastro.")
    if not reason:
        raise ValueError("Informe o motivo da correção do código do banco.")

    work = _one(conn.execute(text("""
        select id,numero_os,status,codigo_banco,conjunto_bancos,version
        from erp_work_orders
        where id=:id
        for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")

    previous_code = str(work.get("codigo_banco") or "").strip()
    previous_description = str(work.get("conjunto_bancos") or "").strip()
    if previous_code == bank_code and previous_description == bank_description:
        return {
            "id": work_id,
            "numero_os": work["numero_os"],
            "status": work["status"],
            "codigo_banco": bank_code,
            "conjunto_bancos": bank_description,
            "replayed": True,
        }

    conn.execute(text("""
        update erp_work_orders
        set codigo_banco=:codigo_banco,
            conjunto_bancos=:conjunto_bancos,
            updated_at=now(),
            version=version+1
        where id=:id
    """), {
        "id": work_id,
        "codigo_banco": bank_code,
        "conjunto_bancos": bank_description,
    })
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,before_data,after_data,reason
        ) values(
            'WORK_ORDER',:id,'CORRECAO_CODIGO_BANCO',:actor,'SUPRIMENTOS',
            jsonb_build_object(
                'codigo_banco',cast(:codigo_anterior as text),
                'conjunto_bancos',cast(:descricao_anterior as text),
                'status',cast(:status as text)
            ),
            jsonb_build_object(
                'codigo_banco',cast(:codigo_novo as text),
                'conjunto_bancos',cast(:descricao_nova as text),
                'status',cast(:status as text)
            ),
            :reason
        )
    """), {
        "id": work_id,
        "actor": actor,
        "codigo_anterior": previous_code or None,
        "descricao_anterior": previous_description or None,
        "codigo_novo": bank_code,
        "descricao_nova": bank_description,
        "status": work["status"],
        "reason": reason,
    })
    return {
        "id": work_id,
        "numero_os": work["numero_os"],
        "status": work["status"],
        "codigo_banco": bank_code,
        "conjunto_bancos": bank_description,
        "replayed": False,
    }


def correct_closed_work_order(conn, work_id, payload, actor):
    """Correct historical O.S. and entry data without reopening production.

    Status, stages, pointing events, delivery/finalization timestamps and
    structural links are deliberately outside the correction whitelist.
    """
    reason = str(payload.get("motivo") or payload.get("reason") or "").strip()
    if not reason:
        raise ValueError("Informe o motivo da correção histórica da O.S.")

    work_payload = payload.get("work_order") or {}
    entry_payload = payload.get("entry") or {}
    if not isinstance(work_payload, dict) or not isinstance(entry_payload, dict):
        raise ValueError("Os dados da correção histórica são inválidos.")

    reference = _one(conn.execute(text("""
        select id,vehicle_entry_id,numero_os,status
          from erp_work_orders
         where id=:id
    """), {"id": work_id}))
    if not reference:
        raise ValueError("O.S. não encontrada.")
    if str(reference.get("status") or "").strip().upper() not in CLOSED_WORK_ORDER_STATUSES:
        raise ValueError("Use a edição normal enquanto a O.S. estiver aberta ou em produção.")

    entry_result = {"replayed": True}
    if entry_payload:
        entry_result = update_vehicle_entry(
            conn,
            reference["vehicle_entry_id"],
            {**entry_payload, "motivo": reason},
            actor,
            audit_origin="SUPRIMENTOS",
        )

    work = _one(conn.execute(text("""
        select *
          from erp_work_orders
         where id=:id
         for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    if str(work.get("status") or "").strip().upper() not in CLOSED_WORK_ORDER_STATUSES:
        raise ValueError("A O.S. mudou de situação. Atualize a tela e tente novamente.")

    changed = {}
    previous = {}
    for name in HISTORICAL_WORK_ORDER_FIELDS:
        if name not in work_payload:
            continue
        value = _work_field_value(name, work_payload[name])
        if name == "tipo_servico":
            value = canonical_service_type(value)
        current = _date_value(work.get(name)) if name in WORK_ORDER_DATE_FIELDS else str(work.get(name) or "").strip()
        if current != value:
            previous[name] = work.get(name)
            changed[name] = value

    if changed:
        assignments = ",".join(f"{name}=:{name}" for name in changed)
        conn.execute(text(f"""
            update erp_work_orders
               set {assignments},updated_at=now(),version=version+1
             where id=:id
        """), {"id": work_id, **changed})

        conn.execute(text("""
            insert into erp_audit_events(
                entity_type,entity_id,action,actor,origin,before_data,after_data,reason
            ) values(
                'WORK_ORDER',:id,'CORRECAO_HISTORICA_DADOS_OS',:actor,'SUPRIMENTOS',
                cast(:before_data as jsonb),cast(:after_data as jsonb),:reason
            )
        """), {
            "id": work_id,
            "actor": actor,
            "before_data": json.dumps(
                {"status": work.get("status"), **previous},
                default=str,
                ensure_ascii=False,
            ),
            "after_data": json.dumps(
                {"status": work.get("status"), **changed},
                default=str,
                ensure_ascii=False,
            ),
            "reason": reason,
        })

    replayed = not changed and bool(entry_result.get("replayed"))
    if not replayed:
        reconcile_purchase_order_allocations(conn, actor, "CORRECAO_HISTORICA_OS")
    return {
        "id": work_id,
        "numero_os": work["numero_os"],
        "status": work["status"],
        "changed_fields": sorted(changed),
        "entry_updated": not bool(entry_result.get("replayed")),
        "replayed": replayed,
    }


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
    air_type = _token(work.get("tipo_sistema_ar"))
    if (
        air_type
        and air_type not in {"NAO", "AR ORIGINAL", "AG", "N/A"}
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
        if str(work.get(field) or "").strip()
        and _token(work.get(field)) not in {_token(option) for option in options}
    ]
    if (
        str(work.get("ar_condicionado") or "").strip()
        and _token(work.get("ar_condicionado")) not in {_token(option) for option in AR_FORNECEDORES}
    ):
        invalid.append("ar_condicionado")
    transformations = {str(code): description for code, description in TRANSFORMACOES}
    transformation_code = str(work.get("transformacao_codigo") or "").strip()
    transformation_description = str(work.get("transformacao") or "").strip()
    if transformation_code or transformation_description:
        if (
            transformation_code not in transformations
            or _token(transformations.get(transformation_code, ""))
               != _token(transformation_description)
        ):
            invalid.append("transformacao")
    if invalid:
        raise ValueError("Valores fora das listas controladas: " + ", ".join(invalid) + ".")
    has_started = bool(conn.execute(text("""
        select exists(
            select 1
            from erp_work_order_stages stage
            where stage.work_order_id=:id
              and stage.status in ('EM_ANDAMENTO','CONCLUÍDA')
              and exists(
                  select 1 from erp_work_order_stage_events event
                  where event.work_order_stage_id=stage.id
                    and event.action='APONTAMENTO_PRE_OS'
              )
        )
    """), {"id": work_id}).scalar_one())
    next_status = "EM_PRODUÇÃO" if has_started else "ATIVA"
    conn.execute(text("update erp_work_orders set status=:status,ativado_por=:actor,ativado_at=now(),updated_at=now(),version=version+1 where id=:id"),{'id':work_id,'actor':actor,'status':next_status})
    conn.execute(text("update erp_vehicle_entries set status=:status where id=:id"), {"id": work["vehicle_entry_id"], "status": next_status})
    conn.execute(text("insert into erp_work_order_status_history(work_order_id,status_anterior,novo_status,usuario,observacao) values(:id,:old,:new,:actor,:note)"),{
        'id':work_id,'old':work['status'],'new':next_status,'actor':actor,
        'note': 'Etapas publicadas no MES; produção já iniciada antes da abertura da O.S.' if has_started else 'Etapas publicadas no MES',
    })
    recalculate_work_order_sequences(conn, actor)
    reconcile_purchase_order_allocations(conn, actor, "ATIVACAO_OS")
    return {'id':work_id,'replayed':False,'status':next_status}

def active_cards(conn):
    rows=conn.execute(text("""
        select w.id,w.numero_os,w.status,w.tipo_servico,w.technical_status,e.item_number,
               v.chassi,v.marca,v.modelo,v.versao,e.modelo_veicular,
               e.cliente_nome as cliente_nome,w.linha,w.transformacao,w.data_comercial_prevista,
               seq.sequencia,seq.semana_planejada,seq.prioridade_manual,
               count(s.id) filter(where s.aplicavel) as etapas_aplicaveis,
               count(s.id) filter(where s.status='CONCLUÍDA') as etapas_concluidas
        from erp_work_orders w
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        left join erp_work_order_sequences seq on seq.work_order_id=w.id and seq.ativo=true
        left join erp_work_order_stages s on s.work_order_id=w.id
        where w.status in ('ATIVA','EM_PRODUÇÃO')
          and w.is_current=true
        group by w.id,e.item_number,e.cliente_nome,e.modelo_veicular,v.chassi,v.marca,v.modelo,v.versao,
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
               coalesce(nullif(trim(e.cliente_nome),''),'') as cliente,
               concat_ws(' ',nullif(trim(v.marca),''),nullif(trim(v.modelo),''),
                         nullif(trim(v.versao),'')) as veiculo
          from erp_work_orders w
          join erp_vehicle_entries e on e.id=w.vehicle_entry_id
          join erp_vehicles v on v.id=e.vehicle_id
         where w.status in ('ATIVA','EM_PRODUÇÃO')
           and w.is_current=true
           and coalesce(w.technical_status,'ABERTA')='ABERTA'
           and (
                :search=''
                or concat_ws(' ',w.numero_os,e.item_number,v.chassi,right(v.chassi,8),
                             e.cliente_nome,v.marca,v.modelo,v.versao)
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


def reconcile_purchase_order_allocations(conn, actor, origin="MES"):
    result = conn.execute(text(
        "select erp_reconcile_ag_chegada_allocations(:actor,:origin)"
    ), {"actor": actor, "origin": origin})
    # Some transactional test doubles deliberately implement only ``first``.
    # Production SQLAlchemy results use scalar_one(), while the fallback keeps
    # the lifecycle contract testable without hiding real database errors.
    scalar_one = getattr(result, "scalar_one", None)
    if callable(scalar_one):
        return int(scalar_one() or 0)
    row = result.first()
    if row is None:
        return 0
    try:
        return int(row[0] or 0)
    except (KeyError, TypeError):
        return 0


def add_work_order_note(conn, work_id, note, actor, origin="MES"):
    message = str(note or "").strip()
    if not message:
        raise ValueError("Escreva a observação antes de adicionar.")
    if len(message) > 4000:
        raise ValueError("A observação deve ter no máximo 4.000 caracteres.")
    exists = conn.execute(text(
        "select 1 from erp_work_orders where id=:id"
    ), {"id": work_id}).first()
    if not exists:
        raise ValueError("O.S. não encontrada.")
    note_id = _id()
    conn.execute(text("""
        insert into erp_work_order_notes(id,work_order_id,note,actor,origin)
        values(:id,:work_order,:note,:actor,:origin)
    """), {
        "id": note_id, "work_order": work_id, "note": message,
        "actor": actor, "origin": origin,
    })
    return {"id": note_id, "note": message}


def add_vehicle_entry_note(conn, entry_id, note, actor, origin="MES"):
    message = str(note or "").strip()
    if not message:
        raise ValueError("Escreva a observação antes de adicionar.")
    if len(message) > 4000:
        raise ValueError("A observação deve ter no máximo 4.000 caracteres.")
    exists = conn.execute(text(
        "select 1 from erp_vehicle_entries where id=:id"
    ), {"id": entry_id}).first()
    if not exists:
        raise ValueError("Entrada de veículo não encontrada.")
    note_id = _id()
    conn.execute(text("""
        insert into erp_vehicle_entry_notes(id,vehicle_entry_id,note,actor,origin)
        values(:id,:entry,:note,:actor,:origin)
    """), {
        "id": note_id, "entry": entry_id, "note": message,
        "actor": actor, "origin": origin,
    })
    return {"id": note_id, "note": message}


def purchase_order_options(conn, search="", limit=50):
    query = str(search or "").strip()
    bounded_limit = min(max(int(limit or 50), 1), 100)
    rows = conn.execute(text("""
        select o.id,o.numero_oc,o.fornecedor_nome,o.status,o.destino,
               o.allocation_mode,o.work_order_id,o.vehicle_entry_id,o.allocation_reference,
               coalesce(sum(l.quantidade_pedida),0) as quantidade_pedida,
               coalesce(sum(l.quantidade_recebida),0) as quantidade_recebida
          from erp_purchase_orders o
          left join erp_purchase_order_lines l on l.purchase_order_id=o.id
         where o.status<>'CANCELADA'
           and coalesce(o.technical_status,'ABERTA')='ABERTA'
           and (
                :search=''
                or concat_ws(' ',o.numero_oc,o.fornecedor_nome,o.destino,
                             o.allocation_reference) ilike :pattern
           )
         group by o.id
         order by o.data_emissao desc nulls last,o.created_at desc
         limit :limit
    """), {"search": query, "pattern": f"%{query}%", "limit": bounded_limit}).mappings()
    return [dict(row) for row in rows]


def set_purchase_order_allocation(
    conn, purchase_order_id, mode, work_order_id, reference, actor,
    vehicle_entry_id=None,
    *, reason="", origin="MES"
):
    target_mode = str(mode or "").strip().upper()
    if target_mode not in {"ESTOQUE", "WORK_ORDER", "AG_CHEGADA"}:
        raise ValueError("Destino de vínculo inválido.")
    order = _one(conn.execute(text("""
        select id,numero_oc,allocation_mode,work_order_id,vehicle_entry_id,
               allocation_reference,destino
          from erp_purchase_orders
         where id=:id
         for update
    """), {"id": purchase_order_id}))
    if not order:
        raise ValueError("O.C. não encontrada.")

    target_work_order = str(work_order_id or "").strip() or None
    target_vehicle_entry = str(vehicle_entry_id or "").strip() or None
    target_reference = str(reference or "").strip()
    if target_mode == "WORK_ORDER":
        if not target_work_order:
            raise ValueError("Informe a O.S. de destino.")
        work = _one(conn.execute(text("""
            select w.id,w.numero_os,w.vehicle_entry_id,e.item_number,v.chassi
              from erp_work_orders w
              join erp_vehicle_entries e on e.id=w.vehicle_entry_id
              join erp_vehicles v on v.id=e.vehicle_id
             where w.id=:id and w.is_current=true
               and coalesce(w.technical_status,'ABERTA')='ABERTA'
               and w.status in ('ATIVA','EM_PRODUÇÃO')
        """), {"id": target_work_order}))
        if not work:
            raise ValueError("A O.S. escolhida não está ativa ou em produção.")
        target_vehicle_entry = str(work["vehicle_entry_id"])
        target_reference = target_reference or (
            f"O.S. {work['numero_os']} · ITEM {work['item_number']} · "
            f"CHASSI {str(work['chassi'] or '')[-8:]}"
        )
    else:
        target_work_order = None
        if target_mode == "ESTOQUE":
            target_vehicle_entry = None
            target_reference = target_reference or "ESTOQUE"
        else:
            if target_vehicle_entry:
                entry = _one(conn.execute(text("""
                    select e.id,e.item_number,v.chassi
                      from erp_vehicle_entries e
                      join erp_vehicles v on v.id=e.vehicle_id
                     where e.id=:id and coalesce(e.status,'AGUARDANDO_O_S')<>'RETIRADA'
                """), {"id": target_vehicle_entry}))
                if not entry:
                    raise ValueError("A entrada escolhida não está disponível para vínculo.")
                target_reference = target_reference or (
                    f"ITEM {entry['item_number']} · CHASSI {str(entry['chassi'] or '')[-8:]}"
                )
            elif not target_reference:
                target_reference = str(order.get("destino") or "").strip()

    changed = (
        str(order.get("allocation_mode") or "ESTOQUE") != target_mode
        or str(order.get("work_order_id") or "") != str(target_work_order or "")
        or str(order.get("vehicle_entry_id") or "") != str(target_vehicle_entry or "")
        or str(order.get("allocation_reference") or "") != target_reference
    )
    if not changed:
        return {"id": str(order["id"]), "unchanged": True}

    conn.execute(text("""
        update erp_purchase_orders
           set allocation_mode=:mode,work_order_id=:work_order,
               vehicle_entry_id=:vehicle_entry,allocation_reference=:reference,
               allocation_updated_at=now(),
               allocation_updated_by=:actor,updated_at=now(),version=version+1
         where id=:id
    """), {
        "id": purchase_order_id, "mode": target_mode,
        "work_order": target_work_order, "reference": target_reference,
        "vehicle_entry": target_vehicle_entry,
        "actor": actor,
    })
    conn.execute(text("""
        update erp_purchase_order_lines
           set work_order_id=:work_order
         where purchase_order_id=:id
    """), {"id": purchase_order_id, "work_order": target_work_order})
    conn.execute(text("""
        insert into erp_purchase_order_allocation_events(
            purchase_order_id,from_mode,to_mode,from_work_order_id,to_work_order_id,
            from_vehicle_entry_id,to_vehicle_entry_id,reference_text,
            action,actor,origin,reason
        ) values(
            :order_id,:from_mode,:to_mode,:from_work_order,:to_work_order,
            :from_vehicle_entry,:to_vehicle_entry,:reference,
            :action,:actor,:origin,:reason
        )
    """), {
        "order_id": purchase_order_id,
        "from_mode": str(order.get("allocation_mode") or "ESTOQUE"),
        "to_mode": target_mode,"from_work_order": order.get("work_order_id"),
        "to_work_order": target_work_order,"reference": target_reference,
        "from_vehicle_entry": order.get("vehicle_entry_id"),
        "to_vehicle_entry": target_vehicle_entry,
        "action": (
            "MANUAL_LINK" if target_mode == "WORK_ORDER"
            else "MANUAL_LINK_ENTRY" if target_vehicle_entry
            else "UNLINK"
        ),
        "actor": actor,"origin": origin,"reason": str(reason or "").strip(),
    })
    conn.execute(text("""
        insert into erp_audit_events(
            entity_type,entity_id,action,actor,origin,before_data,after_data,reason
        ) values(
            'PURCHASE_ORDER',:id,'ALOCACAO_OS_ATUALIZADA',:actor,:origin,
            jsonb_build_object('mode',cast(:from_mode as text),'work_order_id',cast(:from_work as text)),
            jsonb_build_object('mode',cast(:to_mode as text),'work_order_id',cast(:to_work as text)),
            :reason
        )
    """), {
        "id": purchase_order_id,"actor": actor,"origin": origin,
        "from_mode": str(order.get("allocation_mode") or "ESTOQUE"),
        "from_work": str(order.get("work_order_id") or ""),
        "to_mode": target_mode,"to_work": str(target_work_order or ""),
        "reason": str(reason or "").strip(),
    })
    return {"id": str(order["id"]), "allocation_mode": target_mode,
            "work_order_id": target_work_order,
            "vehicle_entry_id": target_vehicle_entry}


def list_work_orders(conn, search="", status="", limit=1000):
    params = {
        "search": f"%{str(search or '').strip()}%",
        "status": str(status or "").strip().upper(),
        "limit": min(max(int(limit or 1000), 1), 2000),
    }
    rows = conn.execute(text("""
        select e.id as entry_id,e.item_number,e.status as entry_status,e.data_chegada,
               e.cliente_nome as entry_client,e.observacoes as entry_notes,e.avarias,e.modelo_veicular,e.tipo_preliminar,
               v.id as vehicle_id,v.chassi,v.marca,v.modelo,v.versao,v.mmv,
               w.id as work_order_id,w.numero_os,w.tipo_servico,w.proposta_numero,
               w.data_aprovacao,w.vendedor,w.mercado,e.cliente_nome as cliente_nome,w.municipio,w.uf,
               w.tipo_veiculo,w.linha,w.transformacao_codigo,w.transformacao,w.codigo_banco,w.conjunto_bancos,
               w.acessibilidade,w.lotacao,w.ar_condicionado,w.tipo_sistema_ar,w.ar_quente,
               w.acessorio,w.plotagem,w.data_comercial_prevista,w.status,w.version,
               w.revision_number,w.is_current,w.supersedes_work_order_id,
               w.stage_configuration_status,w.stage_configured_at,w.stage_configured_by,
               w.technical_status,w.technical_previous_status,
               w.technical_closed_at,w.technical_closed_by,
               w.technical_close_reason,
               w.created_at,w.updated_at,
               f.id as forecast_id,f.codigo as forecast_codigo,f.status as forecast_status,
               seq.sequencia,seq.semana_planejada,seq.prioridade_manual,
               latest_note.note as latest_note,
               latest_note.actor as latest_note_actor,
               latest_note.origin as latest_note_origin,
               latest_note.created_at as latest_note_created_at,
               count(s.id) as etapas_total,
               count(s.id) filter(where s.aplicavel) as etapas_aplicaveis,
               count(s.id) filter(where s.status='CONCLUÍDA') as etapas_concluidas,
               count(s.id) filter(where not s.parametrizado) as etapas_nao_parametrizadas
        from erp_vehicle_entries e
        join erp_vehicles v on v.id=e.vehicle_id
        left join erp_work_orders w on w.vehicle_entry_id=e.id and w.is_current=true
        left join suprimentos_forecasts f on f.work_order_id=w.id
        left join erp_work_order_sequences seq on seq.work_order_id=w.id and seq.ativo=true
        left join lateral (
            select notes.note,notes.actor,notes.origin,notes.created_at
              from (
                    select n.id,n.note,n.actor,n.origin,n.created_at
                      from erp_work_order_notes n
                     where n.work_order_id=w.id
                    union all
                    select n.id,n.note,n.actor,n.origin,n.created_at
                      from erp_vehicle_entry_notes n
                     where n.vehicle_entry_id=e.id
              ) notes
             order by notes.created_at desc,notes.id desc
             limit 1
        ) latest_note on true
        left join erp_work_order_stages s on s.work_order_id=w.id
        where (:status='' or coalesce(w.status,e.status)=:status)
          and (:search='%%' or concat_ws(' ',e.item_number,v.chassi,v.marca,v.modelo,
               e.cliente_nome,w.numero_os,w.cliente_nome,w.proposta_numero,w.linha,
               w.transformacao,latest_note.note) ilike :search)
        group by e.id,v.id,w.id,f.id,f.codigo,f.status,
                 seq.sequencia,seq.semana_planejada,seq.prioridade_manual,
                 latest_note.note,latest_note.actor,latest_note.origin,latest_note.created_at
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
        order["cliente_nome"] = str(order.get("entry_client") or "").strip()
    preliminary_by_entry = {}
    if orders and _pre_os_stage_schema_ready(conn):
        entry_ids = [str(order["entry_id"]) for order in orders if not order.get("work_order_id")]
        if entry_ids:
            preliminary_by_entry = {
                str(row["vehicle_entry_id"]): dict(row)
                for row in conn.execute(text("""
                    select vehicle_entry_id,
                           count(*) filter(where parametrizado) as etapas_apontadas,
                           count(*) filter(where status='CONCLUÍDA') as etapas_concluidas,
                           count(*) filter(where status='EM_ANDAMENTO') as etapas_parciais
                    from erp_vehicle_entry_stages
                    where vehicle_entry_id=any(cast(:entry_ids as uuid[]))
                    group by vehicle_entry_id
                """), {"entry_ids": entry_ids}).mappings()
            }
    for order in orders:
        order["can_create_replacement"] = bool(
            order.get("work_order_id")
            and order.get("is_current")
            and str(order.get("status") or "").strip().upper() == "CANCELADA"
        )
        order["status_operacional"] = operational_work_order_status(
            order.get("status") or order.get("entry_status")
        )
        order["em_wip"] = bool(order.get("work_order_id")) and (
            order["status_operacional"] in {"ATIVA", "EM_PRODUCAO"}
        )
        effective_service_type = order.get("tipo_servico") or order.get("tipo_preliminar")
        order["tipo_servico_grupo"] = service_type_group(effective_service_type)
        order["situacao"] = work_order_situation(
            order.get("status") or order.get("entry_status"),
            effective_service_type,
            order.get("stage_configuration_status"),
        )
        order["arquivado"] = bool(
            order.get("work_order_id") and work_order_is_archived(
                order.get("status"), order.get("technical_previous_status")
            )
        )
        order["arquivado_label"] = "SIM" if order["arquivado"] else "NÃO"
        if not order.get("work_order_id"):
            preliminary = preliminary_by_entry.get(str(order["entry_id"]), {})
            order["etapas_pre_os_apontadas"] = int(preliminary.get("etapas_apontadas") or 0)
            order["etapas_pre_os_concluidas"] = int(preliminary.get("etapas_concluidas") or 0)
            order["etapas_pre_os_parciais"] = int(preliminary.get("etapas_parciais") or 0)
    return orders


def list_production_targets(conn, search="", limit=1000):
    """Return the intentionally narrow card source used by shop-floor users."""
    targets = []
    for row in list_work_orders(conn, search=search, limit=limit):
        work_id = row.get("work_order_id")
        status = str(row.get("status") if work_id else row.get("entry_status") or "").upper()
        status_token = _token(status).replace(" ", "_")
        if work_id:
            if status_token not in {"ATIVA", "EM_PRODUCAO", "FINALIZADA"}:
                continue
            target_kind = "work"
            target_id = str(work_id)
            total = int(row.get("etapas_aplicaveis") or row.get("etapas_total") or 0)
            completed = int(row.get("etapas_concluidas") or 0)
        else:
            if status_token in {"ENTREGUE", "RETIRADA", "CANCELADA", "ARQUIVADA"}:
                continue
            target_kind = "entry"
            target_id = str(row["entry_id"])
            total = len(STAGES)
            completed = int(row.get("etapas_pre_os_concluidas") or 0)
        vehicle_name = " ".join(
            str(row.get(field) or "").strip()
            for field in ("marca", "modelo", "versao")
            if str(row.get(field) or "").strip()
        )
        targets.append({
            **row,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_status": status or "AGUARDANDO O.S.",
            "vehicle_name": vehicle_name or "Veículo não informado",
            "progress_total": total,
            "progress_completed": completed,
            "progress_percent": int((completed / total) * 100) if total else 0,
        })
    return targets


def vehicle_entry_stage_detail(conn, entry_id):
    entry = _one(conn.execute(text("""
        select e.*,v.chassi,v.marca,v.modelo,v.versao,v.mmv,
               w.id as work_order_id,w.numero_os
        from erp_vehicle_entries e
        join erp_vehicles v on v.id=e.vehicle_id
        left join erp_work_orders w on w.vehicle_entry_id=e.id and w.is_current=true
        where e.id=:id
    """), {"id": entry_id}))
    if not entry:
        raise ValueError("Entrada de veículo não encontrada.")
    if entry.get("work_order_id"):
        raise StageConflictError(
            f"A O.S. {entry.get('numero_os')} já foi aberta. Atualize a tela e aponte pela O.S."
        )
    if not _ensure_entry_stage_rows(conn, entry_id):
        raise ValueError("A estrutura de apontamento antes da O.S. ainda não foi instalada.")
    stages = [dict(row._mapping) for row in conn.execute(text("""
        select s.*,
               exists(
                   select 1 from erp_vehicle_entry_stage_events event
                   where event.vehicle_entry_stage_id=s.id
               ) as has_operational_pointing
        from erp_vehicle_entry_stages s
        where s.vehicle_entry_id=:entry
        order by s.ordem
    """), {"entry": entry_id})]
    for stage in stages:
        stage["input_code"] = stage_input_code(stage)
    events = [dict(row._mapping) for row in conn.execute(text("""
        select event.*,stage.stage_code
        from erp_vehicle_entry_stage_events event
        join erp_vehicle_entry_stages stage on stage.id=event.vehicle_entry_stage_id
        where stage.vehicle_entry_id=:entry
        order by event.created_at desc,event.id desc
        limit 500
    """), {"entry": entry_id})]
    status_history = [dict(row._mapping) for row in conn.execute(text("""
        select before_data->>'status' as status_anterior,
               after_data->>'status' as novo_status,
               actor as usuario,created_at,reason as observacao
        from erp_audit_events
        where entity_type='VEHICLE_ENTRY'
          and entity_id=:entry
          and action='RETIRADA_SEM_OS'
        order by created_at desc,id desc
    """), {"entry": entry_id})]
    notes = [dict(row._mapping) for row in conn.execute(text("""
        select id,note,actor,origin,created_at
          from erp_vehicle_entry_notes
         where vehicle_entry_id=:entry
         order by created_at desc,id desc
    """), {"entry": entry_id})]
    purchase_orders = [dict(row._mapping) for row in conn.execute(text("""
        select o.id,o.numero_oc,o.fornecedor_nome,o.status,o.destino,
               o.allocation_mode,o.allocation_reference,o.data_emissao,
               o.data_necessidade,o.valor_total_pedido,o.updated_at,
               coalesce((select count(*) from erp_goods_receipts r
                         where r.purchase_order_id=o.id and r.status='CONFIRMADO'),0)
                   as recebimentos_confirmados,
               case when exists(select 1 from erp_goods_receipts r
                                where r.purchase_order_id=o.id and r.status='CONFIRMADO')
                    then 'VERDE' else 'VERMELHO' end as receipt_signal
          from erp_purchase_orders o
         where o.vehicle_entry_id=:entry and o.work_order_id is null
         order by o.data_emissao desc nulls last,o.created_at desc
    """), {"entry": entry_id})]
    purchase_allocation_history = [dict(row._mapping) for row in conn.execute(text("""
        select event.id,event.purchase_order_id,event.from_mode,event.to_mode,
               event.from_work_order_id,event.to_work_order_id,
               event.from_vehicle_entry_id,event.to_vehicle_entry_id,
               event.reference_text,event.action,event.actor,event.origin,
               event.reason,event.created_at,o.numero_oc,o.fornecedor_nome
          from erp_purchase_order_allocation_events event
          join erp_purchase_orders o on o.id=event.purchase_order_id
         where event.from_vehicle_entry_id=:entry or event.to_vehicle_entry_id=:entry
         order by event.created_at desc,event.id desc
         limit 500
    """), {"entry": entry_id})]
    return {
        "mode": "ENTRY",
        "entry": entry,
        "stages": stages,
        "stage_events": events,
        "status_history": status_history,
        "schedules": [],
        "notes": notes,
        "purchase_orders": purchase_orders,
        "purchase_allocation_history": purchase_allocation_history,
    }


def _has_entry_operational_pointing(conn, stage_id):
    return bool(conn.execute(text("""
        select exists(
            select 1 from erp_vehicle_entry_stage_events
            where vehicle_entry_stage_id=:stage
        )
    """), {"stage": stage_id}).scalar_one())


def update_vehicle_entry_stage(conn, entry_id, code, payload, actor):
    """Record real production against an ITEM that does not have an O.S. yet."""
    code = str(code).upper()
    entry = _one(conn.execute(text("""
        select id,status from erp_vehicle_entries where id=:entry for update
    """), {"entry": entry_id}))
    if entry and _token(entry.get("status")) in {
        "ENTREGUE", "RETIRADA", "CANCELADA", "ARQUIVADA"
    }:
        raise ValueError("Veiculo entregue, retirado, cancelado ou arquivado nao pode receber novos apontamentos.")
    if not entry:
        raise ValueError("Entrada de veículo não encontrada.")
    work = _one(conn.execute(text("""
        select id,numero_os from erp_work_orders where vehicle_entry_id=:entry and is_current=true
    """), {"entry": entry_id}))
    if work:
        raise StageConflictError(
            f"A O.S. {work.get('numero_os')} foi aberta enquanto esta tela estava em uso. "
            "Atualize a tela antes de apontar."
        )
    if not _ensure_entry_stage_rows(conn, entry_id):
        raise ValueError("A estrutura de apontamento antes da O.S. ainda não foi instalada.")
    stage = _one(conn.execute(text("""
        select * from erp_vehicle_entry_stages
        where vehicle_entry_id=:entry and stage_code=:code
        for update
    """), {"entry": entry_id, "code": code}))
    if not stage:
        raise ValueError("Etapa da entrada não encontrada.")

    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    if idempotency_key:
        replay = _one(conn.execute(text("""
            select id from erp_vehicle_entry_stage_events where idempotency_key=:key
        """), {"key": idempotency_key}))
        if replay:
            return {
                "replayed": True, "status": stage["status"],
                "input_code": stage_input_code(stage), "entry_status": entry["status"],
            }

    new = _stage_status_from_input(payload.get("input_code") or payload.get("status"))
    if not new:
        raise ValueError("Informe P, N, S ou N/A para a etapa preliminar.")
    previous_code = stage_input_code(stage)
    expected_code = str(payload.get("expected_status") or "").strip().upper()
    if not expected_code:
        raise StageConflictError(
            "Esta tela está desatualizada. Atualize antes de registrar o apontamento."
        )
    if expected_code != previous_code:
        raise StageConflictError(
            "A etapa foi alterada por outro apontamento. Atualize a tela antes de salvar."
        )
    status_changed = previous_code != str(payload.get("input_code") or "").strip().upper()
    has_pointing = _has_entry_operational_pointing(conn, stage["id"])
    if status_changed and has_pointing and payload.get("confirmed_status_change") is not True:
        raise ValueError("Confirme a alteração do apontamento antes de salvar a etapa.")
    reopening = previous_code in {"S", "N/A"} and new != stage["status"]
    reopen_reason = str(payload.get("reopen_reason") or "").strip()
    if reopening and not reopen_reason:
        raise ValueError("Para reabrir uma etapa concluída ou não aplicável, informe o motivo.")

    now = datetime.utcnow()
    started = payload.get("inicio") or (
        now if new in {"EM_ANDAMENTO", "CONCLUÍDA"} and not stage.get("inicio") else stage.get("inicio")
    )
    finished = payload.get("termino") or (
        now if new == "CONCLUÍDA" and not stage.get("termino") else stage.get("termino")
    )
    if reopening:
        finished = None
    values = {
        "responsible": str(payload.get("responsavel") or stage.get("responsavel") or ""),
        "location": str(payload.get("localizacao") or stage.get("localizacao") or ""),
        "notes": str(payload.get("observacoes") or stage.get("observacoes") or ""),
    }
    conn.execute(text("""
        update erp_vehicle_entry_stages
           set parametrizado=true,
               aplicavel=:applicable,
               status=:status,
               responsavel=:responsible,
               localizacao=:location,
               inicio=coalesce(inicio,:started),
               termino=:finished,
               observacoes=:notes,
               version=version+1,
               updated_at=now()
         where id=:id
    """), {
        "id": stage["id"], "applicable": new != "NÃO_APLICÁVEL", "status": new,
        "started": started, "finished": finished, **values,
    })
    event_note = values["notes"]
    if reopening:
        event_note = f"Reabertura da etapa: {reopen_reason}" + (
            f" | {event_note}" if event_note else ""
        )
    conn.execute(text("""
        insert into erp_vehicle_entry_stage_events(
            id,vehicle_entry_stage_id,action,status_anterior,novo_status,
            operador,inicio,termino,localizacao,observacao,idempotency_key
        ) values(
            :id,:stage,:action,:old,:new,:actor,:started,:finished,:location,:note,:key
        )
    """), {
        "id": _id(), "stage": stage["id"],
        "action": "REABERTURA_PRE_OS" if reopening else "APONTAMENTO_PRE_OS",
        "old": stage["status"] if stage.get("parametrizado") else None,
        "new": new, "actor": actor, "started": started, "finished": finished,
        "location": values["location"], "note": event_note, "key": idempotency_key,
    })
    return {
        "replayed": False, "status": new,
        "input_code": {"EM_ANDAMENTO": "P", "CONCLUÍDA": "S", "NÃO_APLICÁVEL": "N/A"}.get(new, "N"),
        "entry_status": entry["status"], "has_operational_pointing": True,
    }

def work_order_detail(conn, work_id):
    work = _one(conn.execute(text("""
        select w.*,e.item_number,e.data_chegada,e.status as entry_status,
               e.cliente_nome as entry_client,e.observacoes as entry_notes,
               e.avarias,e.modelo_veicular,e.tipo_preliminar,v.chassi,v.marca,v.modelo,v.versao,v.mmv,
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
    work["cliente_nome"] = str(work.get("entry_client") or "").strip()
    work["tipo_servico_grupo"] = service_type_group(work.get("tipo_servico"))
    work["situacao"] = work_order_situation(
        work.get("status"), work.get("tipo_servico"), work.get("stage_configuration_status")
    )
    work["arquivado"] = work_order_is_archived(
        work.get("status"), work.get("technical_previous_status")
    )
    work["arquivado_label"] = "SIM" if work["arquivado"] else "NÃO"
    if (
        work.get("status") in {"RASCUNHO", "AGUARDANDO_O_S"}
        and work.get("stage_configuration_status") == "PENDENTE"
    ):
        # Compatibilidade idempotente para rascunhos criados antes da migração.
        _ensure_stage_rows(conn, work_id, work)
    stages = [dict(row._mapping) for row in conn.execute(text("""
        select s.*,
               exists(
                   select 1
                   from erp_work_order_stage_events event
                   where event.work_order_stage_id=s.id
                     and event.action in ('APONTAMENTO','REABERTURA','APONTAMENTO_PRE_OS')
               ) as has_operational_pointing
        from erp_work_order_stages s
        where s.work_order_id=:id
        order by s.ordem
    """), {"id": work_id})]
    (
        work["inicio_ciclo_produtivo"],
        work["fim_ciclo_produtivo"],
    ) = productive_cycle_window(work, stages)
    for stage in stages:
        stage["input_code"] = stage_input_code(stage)
    schedules = [dict(row._mapping) for row in conn.execute(text("""
        select * from erp_work_order_schedules where work_order_id=:id order by created_at desc
    """), {"id": work_id})]
    history = [dict(row._mapping) for row in conn.execute(text("""
        select * from erp_work_order_status_history where work_order_id=:id order by created_at desc
    """), {"id": work_id})]
    revisions = [dict(row._mapping) for row in conn.execute(text("""
        select id,revision_number,status,is_current,supersedes_work_order_id,
               criado_por,created_at,updated_at
        from erp_work_orders
        where vehicle_entry_id=:entry
        order by revision_number desc,created_at desc
    """), {"entry": work["vehicle_entry_id"]})]
    stage_events = [dict(row._mapping) for row in conn.execute(text("""
        select e.*,s.stage_code
        from erp_work_order_stage_events e
        join erp_work_order_stages s on s.id=e.work_order_stage_id
        where s.work_order_id=:id
        order by e.created_at desc
        limit 500
    """), {"id": work_id})]
    notes = [dict(row) for row in conn.execute(text("""
        select id,note,actor,origin,created_at
          from erp_work_order_notes
         where work_order_id=:id
         order by created_at desc,id desc
    """), {"id": work_id}).mappings()]
    purchase_orders = [dict(row) for row in conn.execute(text("""
        select o.id,o.numero_oc,o.fornecedor_nome,o.status,o.destino,
               o.allocation_mode,o.allocation_reference,o.data_emissao,
               o.data_necessidade,o.valor_total_pedido,o.updated_at,
               coalesce((
                   select count(*) from erp_goods_receipts r
                    where r.purchase_order_id=o.id and r.status='CONFIRMADO'
               ),0) as recebimentos_confirmados,
               case when exists(
                   select 1 from erp_goods_receipts r
                    where r.purchase_order_id=o.id and r.status='CONFIRMADO'
               ) then 'VERDE' else 'VERMELHO' end
                   as receipt_signal
          from erp_purchase_orders o
         where o.work_order_id=:id or o.vehicle_entry_id=:entry_id
         order by o.data_emissao desc nulls last,o.created_at desc
    """), {"id": work_id, "entry_id": work["vehicle_entry_id"]}).mappings()]
    purchase_allocation_history = [dict(row) for row in conn.execute(text("""
        select e.id,e.purchase_order_id,e.from_mode,e.to_mode,
               e.from_work_order_id,e.to_work_order_id,
               e.from_vehicle_entry_id,e.to_vehicle_entry_id,e.reference_text,
               e.action,e.actor,e.origin,e.reason,e.created_at,o.numero_oc,
               o.fornecedor_nome
          from erp_purchase_order_allocation_events e
          join erp_purchase_orders o on o.id=e.purchase_order_id
         where e.from_work_order_id=:id or e.to_work_order_id=:id
            or e.from_vehicle_entry_id=:entry_id or e.to_vehicle_entry_id=:entry_id
         order by e.created_at desc,e.id desc
         limit 500
    """), {"id": work_id, "entry_id": work["vehicle_entry_id"]}).mappings()]
    return {
        "work_order": work,
        "stages": stages,
        "schedules": schedules,
        "status_history": history,
        "revisions": revisions,
        "stage_events": stage_events,
        "notes": notes,
        "purchase_orders": purchase_orders,
        "purchase_allocation_history": purchase_allocation_history,
    }

def configure_stages(conn, work_id, payload, actor):
    work = _one(conn.execute(text("""
        select * from erp_work_orders where id=:id for update
    """), {"id": work_id}))
    if not work:
        raise ValueError("O.S. não encontrada.")
    initial_configuration = work["status"] in {"RASCUNHO", "AGUARDANDO_O_S"}
    recoverable_active_configuration = (
        work["status"] in {"ATIVA", "EM_PRODUÇÃO"}
        and work.get("stage_configuration_status") != "CONCLUIDA"
    )
    if not (initial_configuration or recoverable_active_configuration):
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
        if _has_operational_pointing(conn, stage["id"]):
            current_input = stage_input_code(stage)
            if input_code != current_input:
                raise ValueError(
                    f"A etapa {code} já foi apontada como {current_input} antes da O.S. "
                    "e não pode ser redefinida pela parametrização. Corrija-a pelo apontamento."
                )
            # O trabalho real já parametrizou a etapa; preserve datas e histórico.
            continue
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
            stage_row = next(row for row in rows if row["stage_code"] == code)
            if _has_operational_pointing(conn, stage_row["id"]):
                # Trabalho já executado antes da O.S. não pode ser invalidado
                # por uma regra de precedência aplicada posteriormente.
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


class StageConflictError(ValueError):
    """A stale stage command tried to overwrite a more recent pointing."""


def _stage_status_from_input(value):
    """Convert the UI codes and canonical labels into one database status."""
    normalized = _token(value).replace(" ", "").replace("_", "")
    aliases = {
        "N": "PENDENTE",
        "NAO": "PENDENTE",
        "P": "EM_ANDAMENTO",
        "PARCIAL": "EM_ANDAMENTO",
        "S": "CONCLUÍDA",
        "SIM": "CONCLUÍDA",
        "N/A": "NÃO_APLICÁVEL",
        "NA": "NÃO_APLICÁVEL",
        "NAOAPLICAVEL": "NÃO_APLICÁVEL",
        "PENDENTE": "PENDENTE",
        "LIBERADA": "LIBERADA",
        "EMANDAMENTO": "EM_ANDAMENTO",
        "CONCLUIDA": "CONCLUÍDA",
    }
    return aliases.get(normalized)


def _is_metadata_only_stage_update(payload):
    """Keep old mobile autosave requests from ever changing a stage status.

    Older pages sent ``registrar_historico=false`` together with the status that
    happened to be rendered before the user tapped a status button.  Treating
    those requests as fields-only is deliberately backward compatible and
    protects phones that still have the old page open during a rollout.
    """
    return bool(payload.get("metadata_only")) or payload.get("registrar_historico") is False


def _locked_work_and_stage(conn, work_id, code):
    work = _one(conn.execute(text("""
        select id,status,vehicle_entry_id
        from erp_work_orders
        where id=:work
        for update
    """), {"work": work_id}))
    if not work:
        raise ValueError("O.S. nao encontrada.")
    if work["status"] == "CONCLUIDA":
        raise ValueError("O.S. concluida tecnicamente deve ser reaberta antes de receber apontamentos.")
    if work["status"] in {"CANCELADA", "ARQUIVADA"}:
        raise ValueError("O.S. cancelada ou arquivada nao pode receber apontamentos.")
    stage = _one(conn.execute(text("""
        select * from erp_work_order_stages
        where work_order_id=:work and stage_code=:code
        for update
    """), {"work": work_id, "code": code}))
    if not stage:
        raise ValueError("Etapa da O.S. nao encontrada.")
    return work, stage


def _unfinished_applicable_stage_codes(conn, work_id, completing_stage_id=None):
    """Return applicable stages that still prevent LIBERAÇÃO.

    The rows are locked under the same work-order transaction as the pointing.
    This keeps two operators from independently completing the last steps and
    accidentally finalizing an order based on an inconsistent snapshot.

    ACESSÓRIO and PLOTAGEM are deliberately excluded: they remain production
    pointings, but may occur after the vehicle was released and finalized.
    """
    rows = [dict(row._mapping) for row in conn.execute(text("""
        select id,stage_code,status,aplicavel
        from erp_work_order_stages
        where work_order_id=:work
        order by ordem
        for update
    """), {"work": work_id})]
    completed = {"CONCLUIDA", "NAO_APLICAVEL"}
    return [
        row["stage_code"]
        for row in rows
        if row["id"] != completing_stage_id
        and bool(row.get("aplicavel"))
        and _token(row.get("stage_code")) not in POST_RELEASE_POINTING_STAGE_CODES
        and _token(row.get("status")) not in completed
    ]


def _has_operational_pointing(conn, stage_id):
    """Return whether a stage has already been pointed after parametrization.

    ``PARAMETRIZACAO`` records define the initial production plan and must not
    make the first real N/P/S/N-A change ask for confirmation.  After the
    first operational pointing (or re-opening), every status change is a
    correction and therefore requires explicit confirmation from the user.
    """
    return bool(conn.execute(text("""
        select exists(
            select 1
            from erp_work_order_stage_events
            where work_order_stage_id=:stage
              and action in ('APONTAMENTO','REABERTURA','APONTAMENTO_PRE_OS')
        )
    """), {"stage": stage_id}).scalar_one())


def _metadata_value(payload, field, current):
    """Use a field only when it was actually supplied by the caller.

    Empty date controls mean "keep the current timestamp", matching the prior
    ``coalesce`` behaviour and preventing a delayed browser event from clearing
    dates entered by another save.
    """
    if field not in payload:
        return current
    value = payload.get(field)
    if field in {"inicio", "termino"} and not value:
        return current
    if field in {"responsavel", "localizacao", "observacoes", "bloqueio_motivo"}:
        return str(value or "")
    return value


def _stage_result(stage, work_status, *, replayed=False, metadata_only=False, changed=True):
    return {
        "replayed": replayed,
        "metadata_only": metadata_only,
        "changed": changed,
        "status": stage["status"],
        "input_code": stage_input_code(stage),
        "work_order_status": work_status,
    }


def update_stage_metadata(conn, work_id, code, payload, actor):
    """Persist stage fields without altering status, applicability or setup.

    This is intentionally a distinct domain operation from a production
    pointing.  It is safe to call from ``blur`` handlers on a mobile browser.
    """
    code = str(code).upper()
    work, stage = _locked_work_and_stage(conn, work_id, code)
    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    if idempotency_key:
        replay = _one(conn.execute(text("""
            select id from erp_work_order_stage_events where idempotency_key=:key
        """), {"key": idempotency_key}))
        if replay:
            return _stage_result(
                stage,
                work["status"],
                replayed=True,
                metadata_only=True,
                changed=False,
            )

    values = {
        field: _metadata_value(payload, field, stage.get(field))
        for field in ("responsavel", "localizacao", "inicio", "termino", "observacoes", "bloqueio_motivo")
    }
    conn.execute(text("""
        update erp_work_order_stages
        set responsavel=:responsavel,
            localizacao=:localizacao,
            inicio=:inicio,
            termino=:termino,
            observacoes=:observacoes,
            bloqueio_motivo=:bloqueio_motivo
        where id=:id
    """), {"id": stage["id"], **values})
    conn.execute(text("""
        insert into erp_work_order_stage_events(
            work_order_stage_id,action,status_anterior,novo_status,operador,
            inicio,termino,localizacao,observacao,idempotency_key
        ) values(
            :stage,'METADADOS',:status,:status,:actor,
            :inicio,:termino,:location,:note,:key
        )
    """), {
        "stage": stage["id"],
        "status": stage["status"],
        "actor": actor,
        "inicio": values["inicio"],
        "termino": values["termino"],
        "location": values["localizacao"],
        "note": "Dados operacionais atualizados sem alterar o status da etapa.",
        "key": idempotency_key,
    })
    return _stage_result(stage, work["status"], metadata_only=True)


def update_stage(conn, work_id, code, payload, actor, allow_finalized_stage_pointing=False):
    # Compatibility guard for pages served before the fields-only endpoint.
    # A delayed old autosave must never be allowed to regress a fresh S/P/N/A
    # production pointing.
    if _is_metadata_only_stage_update(payload):
        return update_stage_metadata(conn, work_id, code, payload, actor)

    code = str(code).upper()
    work, stage = _locked_work_and_stage(conn, work_id, code)
    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    if idempotency_key:
        replay = _one(conn.execute(text("""
            select id,novo_status from erp_work_order_stage_events where idempotency_key=:key
        """), {"key": idempotency_key}))
        if replay:
            # The current row is authoritative even if a retry arrives after a
            # later, valid change.  Never make the caller repaint an old state.
            return _stage_result(stage, work["status"], replayed=True)

    new = _stage_status_from_input(payload.get("input_code") or payload.get("status"))
    if not new:
        raise ValueError("Informe explicitamente o status da etapa.")

    expected_raw = payload.get("expected_status")
    if expected_raw not in (None, ""):
        expected_code = str(expected_raw).strip().upper()
        if expected_code == "?":
            expected_matches = stage_input_code(stage) == "?"
        else:
            expected = _stage_status_from_input(expected_raw)
            if not expected:
                raise ValueError("Status esperado da etapa invalido.")
            expected_matches = expected == stage["status"]
        if not expected_matches:
            raise StageConflictError(
                "A etapa foi alterada por outro apontamento. Atualize a tela antes de salvar."
            )

    # The technical history screen may correct its metadata after production
    # is finalized, delivered or withdrawn.  It must never silently reopen a
    # production stage while the work order remains closed, though.
    post_release_pointing = (
        work["status"] == "FINALIZADA"
        and (
            _token(code) in POST_RELEASE_POINTING_STAGE_CODES
            or allow_finalized_stage_pointing
        )
    )
    if work["status"] in {"FINALIZADA", "ENTREGUE", "RETIRADA"} and not post_release_pointing:
        if new != stage["status"]:
            raise StageConflictError(
                "A O.S. esta encerrada. Reabra a O.S. explicitamente antes de alterar o status de uma etapa."
            )
        return update_stage_metadata(conn, work_id, code, payload, actor)

    status_changed = new != stage["status"]
    if (
        status_changed
        and _has_operational_pointing(conn, stage["id"])
        and payload.get("confirmed_status_change") is not True
    ):
        raise ValueError(
            "Confirme a alteracao do apontamento antes de salvar a etapa."
        )

    reopening = (
        stage["status"] in {"CONCLUÍDA", "NÃO_APLICÁVEL"}
        and new != stage["status"]
    )
    reopen_reason = str(payload.get("reopen_reason") or "").strip()
    if reopening and not reopen_reason:
        raise ValueError("Para reabrir uma etapa concluida ou nao aplicavel, informe o motivo.")

    if (
        work["status"] in {"ATIVA", "EM_PRODUÇÃO"}
        and code == "LIBERAÇÃO"
        and new == "CONCLUÍDA"
    ):
        pending_codes = _unfinished_applicable_stage_codes(
            conn,
            work_id,
            completing_stage_id=stage["id"],
        )
        if pending_codes:
            raise ValueError(
                "LIBERAÇÃO só pode ser concluída após as etapas aplicáveis: "
                + ", ".join(pending_codes)
                + "."
            )

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
    fields = {
        field: _metadata_value(payload, field, stage.get(field))
        for field in ("responsavel", "localizacao", "observacoes", "bloqueio_motivo")
    }
    # A reopened stage is no longer complete.  The event keeps the historic
    # completion timestamp, while the current stage must not retain it merely
    # because a browser posted the old disabled input value.
    clear_finish = reopening
    conn.execute(text("""
        update erp_work_order_stages
        set parametrizado=true,
            aplicavel=:applicable,
            status=:status,
            responsavel=:responsavel,
            localizacao=:localizacao,
            inicio=coalesce(:inicio,inicio),
            termino=case when :clear_finish then null else coalesce(:termino,termino) end,
            observacoes=:notes,
            bloqueio_motivo=:blocked
        where id=:id
    """), {
        "applicable": new != "NÃO_APLICÁVEL",
        "status": new,
        "responsavel": fields["responsavel"],
        "localizacao": fields["localizacao"],
        "inicio": started,
        "termino": finished,
        "clear_finish": clear_finish,
        "notes": fields["observacoes"],
        "blocked": fields["bloqueio_motivo"],
        "id": stage["id"],
    })
    event_note = str(payload.get("observacoes") or "")
    if reopening:
        event_note = f"Reabertura da etapa: {reopen_reason}" + (
            f" | {event_note}" if event_note else ""
        )
    conn.execute(text("""
        insert into erp_work_order_stage_events(
            work_order_stage_id,action,status_anterior,novo_status,operador,
            inicio,termino,localizacao,observacao,idempotency_key
        ) values(
            :stage,:action,:old,:new,:actor,:inicio,:termino,:location,:note,:key
        )
    """), {
        "stage": stage["id"],
        "action": "REABERTURA" if reopening else "APONTAMENTO",
        "old": stage["status"],
        "new": new,
        "actor": actor,
        "inicio": started,
        "termino": finished,
        "location": fields["localizacao"],
        "note": event_note,
        "key": idempotency_key,
    })
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
            else "Produção finalizada automaticamente pela conclusão de LIBERAÇÃO; ACESSÓRIO e PLOTAGEM permanecem apontáveis."
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
    updated_stage = {
        **stage,
        "status": new,
        "parametrizado": True,
        "aplicavel": new != "NÃO_APLICÁVEL",
    }
    return _stage_result(updated_stage, next_status)


def _production_datetime(value, default=None):
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return default
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Data/hora inválida para o apontamento.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
    return parsed.astimezone(timezone.utc)


def _pause_stage_column(target_kind):
    return (
        "work_order_stage_id"
        if str(target_kind or "").lower() == "work"
        else "vehicle_entry_stage_id"
    )


def _pause_summary(conn, target_kind, stage_id):
    if not _stage_pause_schema_ready(conn):
        return {
            "open_pause": None, "open_session": None,
            "total_paused_seconds": 0, "total_productive_seconds": 0,
        }
    column = _pause_stage_column(target_kind)
    open_pause = _one(conn.execute(text(f"""
        select * from erp_stage_time_pauses
         where {column}=:stage and ended_at is null
         order by started_at desc
         limit 1
    """), {"stage": stage_id}))
    total = conn.execute(text(f"""
        select coalesce(sum(duration_seconds),0)
          from erp_stage_time_pauses
         where {column}=:stage and ended_at is not null
    """), {"stage": stage_id}).scalar_one()
    open_session = _one(conn.execute(text(f"""
        select * from erp_stage_time_sessions
         where {column}=:stage and ended_at is null
         order by started_at desc
         limit 1
    """), {"stage": stage_id}))
    productive = conn.execute(text(f"""
        select coalesce(sum(productive_seconds),0)
          from erp_stage_time_sessions
         where {column}=:stage and ended_at is not null
    """), {"stage": stage_id}).scalar_one()
    return {
        "open_pause": open_pause,
        "open_session": open_session,
        "total_paused_seconds": int(total or 0),
        "total_productive_seconds": int(productive or 0),
    }


def production_target_detail(conn, target_kind, target_id):
    kind = str(target_kind or "").strip().lower()
    if kind == "work":
        detail = work_order_detail(conn, target_id)
        target = detail["work_order"]
        status = str(target.get("status") or "").upper()
        if _token(status).replace(" ", "_") not in {"ATIVA", "EM_PRODUCAO", "FINALIZADA"}:
            raise ValueError("Esta O.S. não está disponível para apontamento da Produção.")
        item = target.get("item_number")
        os_number = target.get("numero_os")
    elif kind == "entry":
        detail = vehicle_entry_stage_detail(conn, target_id)
        target = detail["entry"]
        status = str(target.get("status") or "AGUARDANDO O.S.").upper()
        if _token(status) in {"ENTREGUE", "RETIRADA", "CANCELADA", "ARQUIVADA"}:
            raise ValueError("Este veículo não está disponível para apontamento.")
        item = target.get("item_number")
        os_number = None
    else:
        raise ValueError("Tipo de apontamento inválido.")

    stages = detail["stages"]
    for stage in stages:
        stage.update(_pause_summary(conn, kind, stage["id"]))
        stage["can_point"] = stage.get("input_code") != "N/A"
    vehicle_name = " ".join(
        str(target.get(field) or "").strip()
        for field in ("marca", "modelo", "versao")
        if str(target.get(field) or "").strip()
    )
    return {
        "target_kind": kind,
        "target_id": str(target_id),
        "target": target,
        "target_status": status,
        "item_number": item,
        "numero_os": os_number,
        "chassi": target.get("chassi"),
        "vehicle_name": vehicle_name or "Veículo não informado",
        "stages": stages,
    }


def _production_locked_stage(conn, target_kind, target_id, stage_code):
    kind = str(target_kind or "").strip().lower()
    code = str(stage_code or "").strip().upper()
    if kind == "work":
        work, stage = _locked_work_and_stage(conn, target_id, code)
        if _token(work["status"]).replace(" ", "_") not in {"ATIVA", "EM_PRODUCAO", "FINALIZADA"}:
            raise ValueError("Esta O.S. não está disponível para apontamento da Produção.")
        return kind, work, stage
    if kind != "entry":
        raise ValueError("Tipo de apontamento inválido.")
    entry = _one(conn.execute(text("""
        select id,status from erp_vehicle_entries where id=:entry for update
    """), {"entry": target_id}))
    if not entry:
        raise ValueError("Entrada de veículo não encontrada.")
    if _token(entry.get("status")) in {"ENTREGUE", "RETIRADA", "CANCELADA", "ARQUIVADA"}:
        raise ValueError("Este veículo não está disponível para apontamento.")
    work = _one(conn.execute(text("""
        select id,numero_os from erp_work_orders
         where vehicle_entry_id=:entry and is_current=true
    """), {"entry": target_id}))
    if work:
        raise StageConflictError(
            f"A O.S. {work.get('numero_os')} foi aberta. Atualize a tela e selecione novamente o card."
        )
    if not _ensure_entry_stage_rows(conn, target_id):
        raise ValueError("A estrutura de apontamento antes da O.S. ainda não foi instalada.")
    stage = _one(conn.execute(text("""
        select * from erp_vehicle_entry_stages
         where vehicle_entry_id=:entry and stage_code=:code
         for update
    """), {"entry": target_id, "code": code}))
    if not stage:
        raise ValueError("Etapa da entrada não encontrada.")
    return kind, entry, stage


def _production_event_replay(conn, target_kind, key):
    if not key:
        return False
    table = (
        "erp_work_order_stage_events"
        if target_kind == "work"
        else "erp_vehicle_entry_stage_events"
    )
    return bool(conn.execute(text(
        f"select exists(select 1 from {table} where idempotency_key=:key)"
    ), {"key": key}).scalar_one())


def _close_stage_pause(conn, target_kind, stage_id, ended_at, actor):
    summary = _pause_summary(conn, target_kind, stage_id)
    pause = summary["open_pause"]
    if not pause:
        return False
    if conn.execute(text("select cast(:ended as timestamptz) < cast(:started as timestamptz)"), {
        "ended": ended_at, "started": pause["started_at"],
    }).scalar_one():
        raise ValueError("O fim da parada não pode ser anterior ao início.")
    conn.execute(text("""
        update erp_stage_time_pauses
           set ended_at=:ended,
               duration_seconds=greatest(
                   0, floor(extract(epoch from (cast(:ended as timestamptz)-started_at)))::bigint
               ),
               ended_by=:actor,
               updated_at=now()
         where id=:id
    """), {"ended": ended_at, "actor": actor, "id": pause["id"]})
    return True


def _open_stage_session(conn, target_kind, stage_id, started_at, actor, note, key):
    summary = _pause_summary(conn, target_kind, stage_id)
    if summary["open_session"]:
        raise ValueError("Esta etapa já possui uma sessão produtiva em andamento.")
    column = _pause_stage_column(target_kind)
    conn.execute(text(f"""
        insert into erp_stage_time_sessions(
            {column},started_at,started_by,observation,idempotency_key
        ) values(
            :stage,:started,:actor,:note,:key
        )
    """), {
        "stage": stage_id, "started": started_at, "actor": actor,
        "note": note, "key": f"{key}:session" if key else None,
    })


def _close_stage_session(conn, target_kind, stage_id, ended_at, actor):
    summary = _pause_summary(conn, target_kind, stage_id)
    session = summary["open_session"]
    if not session:
        raise ValueError("Inicie a etapa antes de parar, interromper ou finalizar.")
    if conn.execute(text("select cast(:ended as timestamptz) < cast(:started as timestamptz)"), {
        "ended": ended_at, "started": session["started_at"],
    }).scalar_one():
        raise ValueError("O fim da sessão não pode ser anterior ao início.")
    conn.execute(text("""
        update erp_stage_time_sessions
           set ended_at=:ended,
               productive_seconds=greatest(
                   0, floor(extract(epoch from (cast(:ended as timestamptz)-started_at)))::bigint
               ),
               ended_by=:actor,
               updated_at=now()
         where id=:id
    """), {"ended": ended_at, "actor": actor, "id": session["id"]})


def execute_production_stage_command(conn, target_kind, target_id, stage_code, payload, actor):
    """Execute the simplified shop-floor commands using canonical MES stages."""
    if not _stage_pause_schema_ready(conn):
        raise ValueError("A migration de paradas da Produção ainda não foi aplicada.")
    action = _token(payload.get("action")).replace(" ", "_")
    if action not in {"INICIAR", "PARAR", "FINALIZAR", "INTERROMPER"}:
        raise ValueError("Comando inválido. Use INICIAR, PARAR, FINALIZAR ou INTERROMPER.")
    kind, target, stage = _production_locked_stage(
        conn, target_kind, target_id, stage_code
    )
    key = str(payload.get("idempotency_key") or "").strip() or None
    if _production_event_replay(conn, kind, key):
        return {
            "replayed": True,
            "input_code": stage_input_code(stage),
            **_pause_summary(conn, kind, stage["id"]),
        }
    expected = str(payload.get("expected_status") or "").strip().upper()
    current = stage_input_code(stage)
    if not expected or expected != current:
        raise StageConflictError(
            "A etapa foi alterada por outro apontamento. Atualize a tela antes de continuar."
        )
    if current == "N/A":
        raise ValueError("Esta etapa não é aplicável.")

    now = datetime.now(timezone.utc)
    start_at = _production_datetime(payload.get("inicio"), now)
    finish_at = _production_datetime(payload.get("termino"), now)
    moment = _production_datetime(payload.get("momento"), now)
    time_state = _pause_summary(conn, kind, stage["id"])
    pause = time_state["open_pause"]
    session = time_state["open_session"]

    if action in {"PARAR", "INTERROMPER"}:
        if current != "P":
            raise ValueError("Inicie a etapa antes de registrar uma parada ou interrupção.")
        if pause:
            raise ValueError("Esta etapa já possui uma parada ou interrupção em aberto.")
        _close_stage_session(conn, kind, stage["id"], moment, actor)
        column = _pause_stage_column(kind)
        pause_type = "PARADA" if action == "PARAR" else "INTERRUPCAO"
        conn.execute(text(f"""
            insert into erp_stage_time_pauses(
                {column},pause_type,started_at,started_by,reason,idempotency_key
            ) values(
                :stage,:type,:started,:actor,:reason,:pause_key
            )
        """), {
            "stage": stage["id"], "type": pause_type, "started": moment,
            "actor": actor, "reason": str(payload.get("observacoes") or "").strip(),
            "pause_key": f"{key}:pause" if key else None,
        })
        event_table = (
            "erp_work_order_stage_events" if kind == "work"
            else "erp_vehicle_entry_stage_events"
        )
        stage_column = (
            "work_order_stage_id" if kind == "work"
            else "vehicle_entry_stage_id"
        )
        conn.execute(text(f"""
            insert into {event_table}(
                {stage_column},action,status_anterior,novo_status,
                operador,inicio,termino,localizacao,observacao,idempotency_key
            ) values(
                :stage,:action,:status,:status,
                :actor,:inicio,:moment,:location,:note,:key
            )
        """), {
            "stage": stage["id"], "action": pause_type,
            "status": stage["status"], "actor": actor,
            "inicio": stage.get("inicio"), "moment": moment,
            "location": stage.get("localizacao"),
            "note": str(payload.get("observacoes") or "").strip(), "key": key,
        })
        return {
            "replayed": False,
            "input_code": current,
            **_pause_summary(conn, kind, stage["id"]),
        }

    if action == "INICIAR":
        if session:
            raise ValueError("Esta etapa já está em andamento.")
        if pause:
            _close_stage_pause(conn, kind, stage["id"], start_at, actor)
        _open_stage_session(
            conn, kind, stage["id"], start_at, actor,
            str(payload.get("observacoes") or "").strip(), key,
        )
        input_code = "P"
    else:
        if pause:
            raise ValueError("Retome a etapa antes de finalizá-la.")
        _close_stage_session(conn, kind, stage["id"], finish_at, actor)
        input_code = "S"

    stage_payload = {
        "input_code": input_code,
        "expected_status": current,
        "responsavel": actor,
        "observacoes": str(payload.get("observacoes") or "").strip(),
        "confirmed_status_change": True,
        "idempotency_key": key,
        "reopen_reason": (
            "Reentrada produtiva para ajuste após conclusão anterior."
            if current == "S" and action == "INICIAR" else ""
        ),
    }
    if action == "INICIAR":
        stage_payload["inicio"] = start_at
    else:
        stage_payload["inicio"] = _production_datetime(payload.get("inicio"), None)
        stage_payload["termino"] = finish_at

    if kind == "work":
        result = update_stage(
            conn,
            target_id,
            stage_code,
            stage_payload,
            actor,
            allow_finalized_stage_pointing=True,
        )
    else:
        result = update_vehicle_entry_stage(
            conn, target_id, stage_code, stage_payload, actor
        )
    return {**result, **_pause_summary(conn, kind, stage["id"])}

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
    if status not in {'FINALIZADA', 'ENTREGUE', 'RETIRADA', 'CANCELADA'}:
        raise ValueError('Status de encerramento inválido.')
    notes = str(notes or '').strip()
    if status == 'CANCELADA' and not notes:
        raise ValueError('Informe o motivo do cancelamento da O.S.')
    # A conclusão técnica impede novos compromissos operacionais, mas não pode
    # impedir a correção administrativa do ciclo.  O cancelamento posterior
    # preserva a conclusão técnica no histórico e registra uma nova transição
    # terminal auditável.  Apenas ordens já canceladas/arquivadas são imutáveis.
    if work['status'] in {'CANCELADA', 'ARQUIVADA'}:
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
               set data_planejada=:date,semana_planejada=to_char(cast(:date as date),'IW')
             where work_order_id=:id
        """), {"id": work_id, "date": _date_value(new_date)})
    recalculate_work_order_sequences(conn, actor)
    return {"id": work_id, "data_comercial_prevista": _date_value(new_date)}
