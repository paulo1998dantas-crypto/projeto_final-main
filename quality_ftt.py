"""Quality occurrences and FTT over current MES stage completions."""
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4
import unicodedata
from collections import Counter, defaultdict
from sqlalchemy import MetaData, Table, Column, String, Integer, Boolean, Date, DateTime, Uuid, JSON, select, text, inspect

TZ = ZoneInfo("America/Sao_Paulo")
START = date(2026, 10, 26)
AREA_NAMES = {"PREP":"Preparação", "EXPE":"Expedição", "SERRA":"Serralheria", "VIDROS":"Vidros",
              "A/C":"Ar-condicionado", "DESMONT":"Desmontagem", "REVEST":"Revestimento",
              "ELÉTRICA":"Elétrica", "BCO":"Bancos", "ACESSÓRIO":"Acessório",
              "PLOTAGEM":"Plotagem", "LIBERAÇÃO":"Liberação"}
# LIBERAÇÃO encerra a O.S.; não é uma área auditável do FTT.
FTT_EXCLUDED_STAGES = frozenset({"LIBERAÇÃO"})
metadata = MetaData()
defects = Table("erp_quality_defects", metadata,
    Column("id", String(36), primary_key=True), Column("description", String(200), nullable=False),
    Column("normalized", String(200), unique=True, nullable=False),
    Column("created_by", String(100), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))
reports = Table("erp_quality_reports", metadata,
    Column("id", String(36), primary_key=True), Column("work_order_id", Uuid(as_uuid=False).with_variant(String(36), "sqlite"), nullable=False),
    Column("observed_on", Date, nullable=False), Column("actor_id", Integer, nullable=False),
    Column("actor", String(100), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("audit_complete", Boolean, nullable=False, server_default=text("false")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("voided", Boolean, nullable=False, server_default=text("false")))
issues = Table("erp_quality_issues", metadata,
    Column("id", String(36), primary_key=True), Column("report_id", String(36), nullable=False),
    Column("defect_id", String(36), nullable=False), Column("description", String(200), nullable=False),
    Column("stage_code", String(40), nullable=False), Column("disposition", String(12), nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("true")))
audit_areas = Table("erp_quality_report_areas", metadata,
    Column("report_id", String(36), primary_key=True), Column("stage_code", String(40), primary_key=True))
corrections = Table("erp_quality_corrections", metadata,
    Column("id", String(36), primary_key=True), Column("report_id", String(36), nullable=False),
    Column("issue_id", String(36), nullable=False), Column("from_stage", String(40), nullable=False),
    Column("to_stage", String(40), nullable=False), Column("reason", String(500), nullable=False),
    Column("actor_id", Integer, nullable=False), Column("actor", String(100), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("version", Integer, nullable=False))
report_events = Table("erp_quality_report_events", metadata,
    Column("id", String(36), primary_key=True), Column("report_id", String(36), nullable=False),
    Column("action", String(12), nullable=False), Column("before_data", JSON, nullable=False),
    Column("after_data", JSON, nullable=False), Column("reason", String(500), nullable=False),
    Column("actor_id", Integer, nullable=False), Column("actor", String(100), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("version", Integer, nullable=False))

AUDIT_MIGRATION = "migrations/20261103195306_mes_quality_audits.sql"
EDIT_MIGRATION = "migrations/20261103200911_mes_quality_report_editing.sql"

def require_audits(conn):
    require_ready(conn)
    inspector = inspect(conn)
    columns = {c["name"] for c in inspector.get_columns(reports.name)}
    if not {"audit_complete", "version"} <= columns or not all(
            inspector.has_table(t.name) for t in (audit_areas, corrections)):
        raise ValueError("FTT indisponível: aplique a migração " + AUDIT_MIGRATION + ".")
    if "voided" not in columns or "active" not in {c["name"] for c in inspector.get_columns(issues.name)} or not inspector.has_table(report_events.name):
        raise ValueError("FTT indisponível: aplique a migração " + EDIT_MIGRATION + ".")
def normalize(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value).strip().upper())
                   if not unicodedata.combining(c))

def today():
    return datetime.now(TZ).date()

def require_ready(conn):
    if not all(inspect(conn).has_table(t.name) for t in (defects, reports, issues)):
        raise ValueError("FTT indisponível: aplique a migração 20261022_mes_quality_ftt.sql no banco do MES.")

def day(value):
    if isinstance(value, str) and len(value.strip()) == 10:
        return date.fromisoformat(value.strip())
    if isinstance(value, datetime):
        return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(TZ).date()
    if isinstance(value, date):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return day(parsed)

def period(start=None, end=None):
    try:
        first = max(START, date.fromisoformat(start) if start else START)
        last = date.fromisoformat(end) if end else max(START, today())
    except (ValueError, TypeError):
        raise ValueError("Informe datas válidas.")
    if last < first:
        raise ValueError("A data final deve ser igual ou posterior à inicial (início em 26/10/2026).")
    return first, last

def create_defect(conn, description, actor):
    require_ready(conn)
    value = " ".join(str(description or "").split())
    if not 3 <= len(value) <= 200:
        raise ValueError("Descreva o defeito com 3 a 200 caracteres.")
    key = normalize(value)
    # PostgreSQL serializes equivalent descriptions across concurrent requests.
    if conn.dialect.name == "postgresql":
        conn.execute(text("select pg_advisory_xact_lock(hashtextextended(:key,0))"),
                     {"key": "quality-defect:" + key})
    row = conn.execute(select(defects).where(defects.c.normalized == key)).mappings().first()
    if row:
        return dict(row)
    row = dict(id=str(uuid4()), description=value, normalized=key, created_by=actor,
               created_at=datetime.now(timezone.utc))
    conn.execute(defects.insert().values(**row))
    return row

def work_options(conn, search="", include_history=False):
    term = "%" + str(search or "").strip()[:100].upper() + "%"
    scope = "true" if include_history else "w.is_current=true and w.status not in ('CANCELADA','RETIRADA')"
    return [dict(r) for r in conn.execute(text("""
        select w.id,w.numero_os,e.item_number,v.chassi,w.status
        from erp_work_orders w join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        where """ + scope + """
          and (upper(w.numero_os) like :q or upper(v.chassi) like :q
               or cast(e.item_number as varchar) like :q)
        order by e.item_number desc limit 50
    """), {"q": term}).mappings()]

def create_report(conn, data, user, stage_codes):
    require_audits(conn)
    complete = data.get("audit_complete", False)
    if not isinstance(complete, bool):
        raise ValueError("Confirme se a auditoria do veículo é completa.")
    try:
        identifier = str(UUID(str(data.get("idempotency_key", ""))))
        work_id = str(UUID(str(data.get("work_order_id", ""))))
        observed = date.fromisoformat(str(data.get("observed_on", "")))
    except (ValueError, TypeError):
        raise ValueError("Informe O.S., data e identificador válido do lançamento.")
    if not isinstance(data.get("issues"), list) or not all(isinstance(x, dict) for x in data["issues"]):
        raise ValueError("Inclua uma lista válida de problemas.")
    if conn.dialect.name == "postgresql":
        conn.execute(text("select pg_advisory_xact_lock(hashtextextended(:key,0))"),
                     {"key": "quality-report:" + identifier})
    old = conn.execute(select(reports).where(reports.c.id == identifier)).mappings().first()
    if old:
        if old["voided"]:
            raise ValueError("Este apontamento foi excluído. Consulte o histórico antes de registrar novamente.")
        existing = list(conn.execute(select(issues).where(issues.c.report_id == identifier, issues.c.active.is_(True))).mappings())
        requested = sorted((str(x.get("defect_id")), str(x.get("stage_code")), "DEFEITO")
                           for x in data.get("issues", []))
        saved = sorted((x["defect_id"], x["stage_code"], x["disposition"]) for x in existing)
        if old["actor_id"] != user.id or old["work_order_id"] != work_id or old["observed_on"] != observed or old["audit_complete"] != complete or requested != saved:
            raise ValueError("Identificador já usado em outro lançamento. Atualize a tela.")
        return {"id": identifier, "replayed": True}
    if observed < START or observed > today():
        raise ValueError("A data deve estar entre 26/10/2026 e hoje.")
    work = conn.execute(text("select id from erp_work_orders where id=:id and is_current=true and status not in ('CANCELADA','RETIRADA')"),
                        {"id": work_id}).first()
    if not work:
        raise ValueError("O.S. vigente não encontrada.")
    entries = data.get("issues")
    if not (0 if complete else 1) <= len(entries) <= 100:
        raise ValueError("Inclua até 100 problemas; sem defeitos, confirme a auditoria completa.")
    applicable_codes = set(conn.execute(text("select stage_code from erp_work_order_stages where work_order_id=:work and aplicavel=true"),
                                        {"work": work_id}).scalars()) & set(stage_codes) - FTT_EXCLUDED_STAGES
    if complete and not applicable_codes:
        raise ValueError("A O.S. não possui áreas aplicáveis para a auditoria FTT.")
    valid = {r["id"]: r["description"] for r in conn.execute(select(defects)).mappings()}
    rows, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Cada problema deve conter defeito, área e classificação.")
        code, defect_id = str(entry.get("stage_code", "")), str(entry.get("defect_id", ""))
        # A classificação única da regra atual é defeito. O valor legado do
        # cliente é ignorado para manter repetição idempotente.
        disposition = "DEFEITO"
        if code not in stage_codes or defect_id not in valid:
            raise ValueError("Selecione defeito e área válidos.")
        if (code, defect_id) in seen:
            raise ValueError("O mesmo defeito/área está repetido neste apontamento.")
        if code not in applicable_codes:
            raise ValueError("A área selecionada não é aplicável à O.S.")
        seen.add((code, defect_id))
        rows.append(dict(id=str(uuid4()), report_id=identifier, defect_id=defect_id,
                         description=valid[defect_id], stage_code=code, disposition=disposition))
    conn.execute(reports.insert().values(id=identifier, work_order_id=work_id, observed_on=observed,
        actor_id=user.id, actor=user.username, created_at=datetime.now(timezone.utc), audit_complete=complete, version=1))
    if rows:
        conn.execute(issues.insert(), rows)
    if complete:
        conn.execute(audit_areas.insert(), [dict(report_id=identifier, stage_code=code) for code in sorted(applicable_codes)])
    return {"id": identifier, "replayed": False}

def calculate(completions, occurrences, stage_codes, first, last):
    completed = defaultdict(set)
    for row in completions:
        if row["stage_code"] in stage_codes and row.get("termino") and first <= day(row["termino"]) <= last:
            completed[row["stage_code"]].add(str(row.get("unit_id") or row["work_order_id"]))
    failed = defaultdict(set)
    counts = Counter()
    defect_areas = defaultdict(set)
    records = []
    pending = 0
    for row in occurrences:
        code, work = row["stage_code"], str(row.get("unit_id") or row["work_order_id"])
        if code not in stage_codes:
            continue
        if day(row["observed_on"]) >= START and work in completed[code]:
            # Registros legados REFUGO também representam uma falha na regra
            # atual, sem criar uma segunda reprovação da mesma passagem.
            failed[code].add(work)
        if first <= day(row["observed_on"]) <= last:
            counts[row["description"]] += 1
            defect_areas[row["description"]].add(code)
            records.append(row)
            if work not in completed[code]:
                pending += 1
    areas = []
    for code in stage_codes:
        total = len(completed[code])
        bad = len(failed[code])
        good = total - bad
        ftt = round(100 * good / total, 2) if total else None
        areas.append(dict(area=code, produced=total, defective=bad, first_pass=good,
                          ftt=ftt, status="Sem base" if ftt is None else
                          "Classe Mundial" if ftt >= 95 else "Atenção" if ftt >= 90 else "Crítico"))
    total_failures = sum(counts.values())
    pareto, cumulative = [], 0
    for rank, (description, count) in enumerate(sorted(counts.items(), key=lambda x:(-x[1],x[0]))[:10], 1):
        cumulative += count
        pareto.append(dict(rank=rank, description=description, area=" · ".join(AREA_NAMES.get(c,c) for c in sorted(defect_areas[description])), frequency=count,
                           accumulated=round(100*cumulative/total_failures, 2)))
    totals = {key: sum(r[key] for r in areas) for key in ("produced","defective","first_pass")}
    totals["ftt"] = round(100*totals["first_pass"]/totals["produced"],2) if totals["produced"] else None
    return dict(areas=areas, totals=totals, pareto=pareto, total_failures=total_failures,
                records=records, pending=pending, start=first.isoformat(), end=last.isoformat())

def production_completions(conn, first, last):
    cutoff = datetime.combine(first, time.min, TZ)
    until = datetime.combine(last + timedelta(days=1), time.min, TZ)
    return [dict(r) for r in conn.execute(text("""
        select s.work_order_id,w.vehicle_entry_id as unit_id,s.stage_code,s.termino
        from erp_work_order_stages s join erp_work_orders w on w.id=s.work_order_id
        where s.aplicavel=true and s.status='CONCLUÍDA' and s.termino>=:cutoff and s.termino<:until
          and w.is_current=true and w.status not in ('CANCELADA','RETIRADA')
    """), {"cutoff":cutoff,"until":until}).mappings()]


def dashboard(conn, stage_codes, start=None, end=None):
    """Legacy API semantics are retained for integrations outside the new MES view."""
    require_ready(conn)
    first, last = period(start, end)
    completions = production_completions(conn, first, last)
    rows = [dict(r) for r in conn.execute(text("""
        select i.description,i.stage_code,i.disposition,r.work_order_id,r.observed_on,
               r.actor,r.created_at,r.id,w.numero_os,w.vehicle_entry_id as unit_id,v.chassi
        from erp_quality_issues i join erp_quality_reports r on r.id=i.report_id
        join erp_work_orders w on w.id=r.work_order_id
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        where r.observed_on>=:cutoff and r.voided=false and i.active=true
        order by r.observed_on desc,r.created_at desc,i.id
    """), {"cutoff":START}).mappings()]
    return calculate(completions, rows, stage_codes, first, last)


def correct_issue(conn, issue_id, data, user, stage_codes):
    """Only correct responsibility; append evidence and never rewrite production."""
    require_audits(conn)
    try:
        identifier = str(UUID(str(data.get("idempotency_key", ""))))
        issue_id = str(UUID(str(issue_id)))
    except (ValueError, TypeError):
        raise ValueError("Identificador da correção inválido.")
    version = data.get("version")
    reason = " ".join(str(data.get("reason") or "").split())
    code = str(data.get("stage_code", ""))
    if type(version) is not int or version < 1 or not 5 <= len(reason) <= 500 or code not in stage_codes or code in FTT_EXCLUDED_STAGES:
        raise ValueError("Selecione a área e informe o motivo da correção (5 a 500 caracteres).")
    if conn.dialect.name == "postgresql":
        conn.execute(text("select pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "quality-correction:" + identifier})
    replay = conn.execute(select(corrections).where(corrections.c.id == identifier)).mappings().first()
    if replay:
        if (replay["actor_id"], replay["issue_id"], replay["to_stage"], replay["reason"], replay["version"]) != (user.id, issue_id, code, reason, version + 1):
            raise ValueError("Identificador já usado em outra correção.")
        return {"id": identifier, "replayed": True}
    row = conn.execute(select(issues, reports.c.work_order_id, reports.c.version, reports.c.audit_complete, reports.c.voided)
                       .join(reports, reports.c.id == issues.c.report_id).where(issues.c.id == issue_id)).mappings().first()
    if not row:
        raise ValueError("Defeito não encontrado. Atualize o histórico.")
    if not row["active"] or row["voided"]:
        raise ValueError("O apontamento ou defeito foi excluído. Atualize o histórico.")
    if row["version"] != version:
        raise ValueError("Este apontamento já foi corrigido por outra pessoa. Atualize o histórico.")
    if row["stage_code"] == code:
        raise ValueError("A área selecionada já é a área responsável.")
    if row["audit_complete"]:
        applicable = conn.execute(select(audit_areas.c.stage_code).where(audit_areas.c.report_id == row["report_id"])).scalars()
    else:
        applicable = conn.execute(text("select stage_code from erp_work_order_stages where work_order_id=:work and aplicavel=true"), {"work": row["work_order_id"]}).scalars()
    if code not in set(applicable):
        raise ValueError("A área selecionada não é aplicável à auditoria desta O.S.")
    duplicate = conn.execute(select(issues.c.id).where(issues.c.report_id == row["report_id"], issues.c.defect_id == row["defect_id"], issues.c.stage_code == code, issues.c.active.is_(True))).first()
    if duplicate:
        raise ValueError("Este defeito já está registrado na área escolhida neste apontamento.")
    changed = conn.execute(reports.update().where(reports.c.id == row["report_id"], reports.c.version == version).values(version=version + 1))
    if changed.rowcount != 1:
        raise ValueError("Este apontamento já foi corrigido. Atualize o histórico.")
    conn.execute(issues.update().where(issues.c.id == issue_id).values(stage_code=code))
    conn.execute(corrections.insert().values(id=identifier, report_id=row["report_id"], issue_id=issue_id,
        from_stage=row["stage_code"], to_stage=code, reason=reason, actor_id=user.id, actor=user.username,
        created_at=datetime.now(timezone.utc), version=version + 1))
    return {"id": identifier, "replayed": False}


def change_report(conn, report_id, data, user, stage_codes, delete=False):
    """Edit dates/defect lines or exclude a Quality record; retain all evidence."""
    require_audits(conn)
    try:
        identifier = str(UUID(str(data.get("idempotency_key", ""))))
        report_id = str(UUID(str(report_id)))
    except (ValueError, TypeError):
        raise ValueError("Identificador do apontamento ou da alteração inválido.")
    version = data.get("version")
    reason = " ".join(str(data.get("reason") or "").split())
    if type(version) is not int or version < 1 or not 5 <= len(reason) <= 500:
        raise ValueError("Informe a versão atual e o motivo (5 a 500 caracteres).")
    if delete and data.get("confirm") is not True:
        raise ValueError("Confirme a exclusão do apontamento.")
    action = "DELETE" if delete else "EDIT"
    request = {"action": action, "version": version, "reason": reason}
    if not delete:
        raw = data.get("issues")
        complete = data.get("audit_complete")
        if not isinstance(complete, bool) or not isinstance(raw, list) or not (0 if complete else 1) <= len(raw) <= 100 or not all(isinstance(x,dict) for x in raw):
            raise ValueError("Informe os defeitos ou confirme uma auditoria completa sem defeitos.")
        request.update(observed_on=str(data.get("observed_on", "")), audit_complete=complete,
            issues=[{k:str(x.get(k) or "") for k in ("id","defect_id","stage_code")} for x in raw])
    if conn.dialect.name == "postgresql":
        conn.execute(text("select pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key":"quality-edit:" + identifier})
    replay = conn.execute(select(report_events).where(report_events.c.id == identifier)).mappings().first()
    if replay:
        if replay["actor_id"] != user.id or replay["report_id"] != report_id or replay["after_data"].get("request") != request:
            raise ValueError("Identificador já usado em outra alteração.")
        return {"id":identifier,"replayed":True}
    report = conn.execute(select(reports).where(reports.c.id == report_id)).mappings().first()
    if not report or report["voided"]:
        raise ValueError("Apontamento não encontrado ou já excluído. Atualize o histórico.")
    if report["version"] != version:
        raise ValueError("Este apontamento já foi alterado. Atualize o histórico antes de editar.")
    old = [dict(r) for r in conn.execute(select(issues).where(issues.c.report_id == report_id,issues.c.active.is_(True))).mappings()]
    def snapshot(observed,complete,voided,rows):
        return {"observed_on":str(observed),"audit_complete":complete,"voided":voided,
                "issues":[{k:r[k] for k in ("id","defect_id","description","stage_code","disposition")} for r in rows]}
    before = snapshot(report["observed_on"],report["audit_complete"],False,old)
    values, prepared, applicable = {}, [], set()
    if delete:
        values["voided"] = True
        after = snapshot(report["observed_on"],report["audit_complete"],True,old)
    else:
        try:
            observed = date.fromisoformat(request["observed_on"])
        except ValueError:
            raise ValueError("Informe uma data válida para a auditoria.")
        if not START <= observed <= today():
            raise ValueError("A data deve estar entre 26/10/2026 e hoje.")
        frozen = set(conn.execute(select(audit_areas.c.stage_code).where(audit_areas.c.report_id == report_id)).scalars())
        applicable = frozen or set(conn.execute(text("select stage_code from erp_work_order_stages where work_order_id=:work and aplicavel=true"), {"work":report["work_order_id"]}).scalars())
        applicable &= set(stage_codes) - FTT_EXCLUDED_STAGES
        if complete and not applicable:
            raise ValueError("A O.S. não possui áreas aplicáveis para a auditoria.")
        catalog = {r["id"]:r["description"] for r in conn.execute(select(defects)).mappings()}
        old_ids, used_ids, seen = {r["id"] for r in old}, set(), set()
        for entry in request["issues"]:
            code, defect, item_id = entry["stage_code"],entry["defect_id"],entry["id"]
            if code not in applicable or defect not in catalog:
                raise ValueError("Selecione defeitos e setores aplicáveis à O.S.")
            if (code,defect) in seen or (item_id and item_id in used_ids):
                raise ValueError("Há um defeito/setor repetido neste apontamento.")
            if item_id and item_id not in old_ids:
                raise ValueError("A linha do defeito não pertence ao apontamento atual.")
            seen.add((code,defect));used_ids.add(item_id)
            prepared.append(dict(id=item_id or str(uuid4()),report_id=report_id,defect_id=defect,
                description=catalog[defect],stage_code=code,disposition="DEFEITO",active=True))
        values.update(observed_on=observed,audit_complete=complete)
        after = snapshot(observed,complete,False,prepared)
        if before == after:
            raise ValueError("Nenhuma alteração foi feita no apontamento.")
    changed = conn.execute(reports.update().where(reports.c.id == report_id,reports.c.version == version,reports.c.voided.is_(False)).values(**values,version=version+1))
    if changed.rowcount != 1:
        raise ValueError("Este apontamento já foi alterado. Atualize o histórico.")
    if not delete:
        conn.execute(issues.update().where(issues.c.report_id == report_id,issues.c.active.is_(True)).values(active=False))
        old_ids = {r["id"] for r in old}
        for entry in prepared:
            if entry["id"] in old_ids:
                conn.execute(issues.update().where(issues.c.id == entry["id"]).values(**{k:v for k,v in entry.items() if k not in ("id","report_id")}))
            else:
                conn.execute(issues.insert().values(**entry))
        if complete and not frozen:
            conn.execute(audit_areas.insert(),[dict(report_id=report_id,stage_code=code) for code in sorted(applicable)])
    after["request"] = request
    conn.execute(report_events.insert().values(id=identifier,report_id=report_id,action=action,
        before_data=before,after_data=after,reason=reason,actor_id=user.id,actor=user.username,
        created_at=datetime.now(timezone.utc),version=version+1))
    return {"id":identifier,"replayed":False}


def history(conn, work_id=None, observed_on=None, start=None, end=None, include_voided=False):
    """Day list, or all quality records for the same vehicle entry (including O.S. revisions)."""
    require_audits(conn)
    params, conditions = {}, ["r.observed_on>=:cutoff"]
    if not include_voided:
        conditions.append("r.voided=false")
    params["cutoff"] = START
    if work_id:
        try:
            params["work"] = str(UUID(str(work_id)))
        except (ValueError, TypeError):
            raise ValueError("Selecione uma O.S. válida.")
        conditions.append("w.vehicle_entry_id=(select vehicle_entry_id from erp_work_orders where id=:work)")
    if observed_on:
        try:
            params["observed"] = date.fromisoformat(observed_on)
        except (ValueError, TypeError):
            raise ValueError("Informe uma data válida para consultar as O.S. apontadas.")
        conditions.append("r.observed_on=:observed")
    if start or end:
        params["first"], params["last"] = period(start, end)
        conditions.append("r.observed_on between :first and :last")
    if not work_id and not observed_on and not (start or end):
        raise ValueError("Selecione uma O.S. ou o dia dos apontamentos.")
    rows = [dict(r) for r in conn.execute(text("""
        select r.*,w.numero_os,w.vehicle_entry_id as unit_id,e.item_number,v.chassi
        from erp_quality_reports r join erp_work_orders w on w.id=r.work_order_id
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id join erp_vehicles v on v.id=e.vehicle_id
        where """ + " and ".join(conditions) + " order by r.observed_on desc,r.created_at desc,r.id").bindparams(**params)).mappings()]
    if not rows:
        return {"reports": [], "vehicles": 0}
    ids = [r["id"] for r in rows]
    by_report, changes, areas, events = defaultdict(list), defaultdict(list), defaultdict(list), defaultdict(list)
    for issue in conn.execute(select(issues).where(issues.c.report_id.in_(ids), issues.c.active.is_(True)).order_by(issues.c.description)).mappings():
        by_report[issue["report_id"]].append(dict(issue))
    for change in conn.execute(select(corrections).where(corrections.c.report_id.in_(ids)).order_by(corrections.c.created_at)).mappings():
        changes[change["report_id"]].append(dict(change))
    for area in conn.execute(select(audit_areas).where(audit_areas.c.report_id.in_(ids))).mappings():
        areas[area["report_id"]].append(area["stage_code"])
    for event in conn.execute(select(report_events).where(report_events.c.report_id.in_(ids)).order_by(report_events.c.created_at)).mappings():
        events[event["report_id"]].append(dict(event))
    for row in rows:
        row["audit_complete"] = bool(row["audit_complete"])
        row["voided"] = bool(row["voided"])
        row.update(issues=by_report[row["id"]], corrections=changes[row["id"]], areas=areas[row["id"]], events=events[row["id"]])
    return {"reports": rows, "vehicles": len({r["unit_id"] for r in rows if not r["voided"]})}


def quality_dashboard(conn, stage_codes, start=None, end=None):
    """Quality cohort is first complete audit, independent of productive completions.

    Legacy defect-only records never imply approval of uninspected areas.
    Reaudits cannot turn a previously failed first pass into an approval.
    Each vehicle ENTRY is a unit: repeated chassis are not merged.
    """
    require_audits(conn)
    first, last = period(start, end)
    codes = [c for c in stage_codes if c not in FTT_EXCLUDED_STAGES]
    # Reuse production data only; do not load every historical issue twice.
    production_counts = calculate(production_completions(conn, first, last), [], codes, first, last)
    audited = [dict(r) for r in conn.execute(text("""
        select r.id,r.observed_on,r.created_at,w.vehicle_entry_id as unit_id,a.stage_code
        from erp_quality_reports r join erp_work_orders w on w.id=r.work_order_id
        join erp_quality_report_areas a on a.report_id=r.id
        where r.audit_complete=true and r.voided=false and r.observed_on>=:cutoff and r.observed_on<=:until
        order by r.observed_on,r.created_at,r.id,a.stage_code
    """), {"cutoff": START, "until": last}).mappings()]
    first_audits, first_areas = {}, defaultdict(set)
    for row in audited:
        unit = str(row["unit_id"])
        first_audits.setdefault(unit, row)
        if row["id"] == first_audits[unit]["id"] and row["stage_code"] in codes:
            first_areas[unit].add(row["stage_code"])
    cohort = {unit for unit, r in first_audits.items() if first <= day(r["observed_on"]) <= last}
    all_issues = [dict(r) for r in conn.execute(text("""
        select i.*,r.work_order_id,r.observed_on,r.actor,r.created_at,w.numero_os,
               w.vehicle_entry_id as unit_id,v.chassi
        from erp_quality_issues i join erp_quality_reports r on r.id=i.report_id
        join erp_work_orders w on w.id=r.work_order_id
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id join erp_vehicles v on v.id=e.vehicle_id
        where r.voided=false and i.active=true and r.observed_on>=:cutoff and r.observed_on<=:until
        order by r.observed_on desc,r.created_at desc,i.id
    """), {"cutoff": START, "until": last}).mappings()]
    failed = defaultdict(set)
    failed_vehicles = set()
    for row in all_issues:
        unit, code = str(row["unit_id"]), row["stage_code"]
        if unit in cohort and code in first_areas[unit]:
            failed[code].add(unit)
            failed_vehicles.add(unit)
    areas = []
    for production in production_counts["areas"]:
        code = production["area"]
        total = sum(code in first_areas[unit] for unit in cohort)
        bad = len(failed[code])
        value = round(100 * (total - bad) / total, 2) if total else None
        areas.append(dict(area=code, produced=production["produced"], audited=total,
            defective=bad, first_pass=total-bad, ftt=value, status="Sem base" if value is None else
            "Classe Mundial" if value >= 95 else "Atenção" if value >= 90 else "Crítico"))
    result = calculate([], all_issues, codes, first, last)  # Pareto still follows detection date.
    totals = {key: sum(r[key] for r in areas) for key in ("produced", "audited", "defective", "first_pass")}
    totals["ftt"] = round(100 * totals["first_pass"] / totals["audited"], 2) if totals["audited"] else None
    vehicles = dict(audited=len(cohort), defective=len(failed_vehicles), first_pass=len(cohort)-len(failed_vehicles))
    vehicles["ftt"] = round(100 * vehicles["first_pass"] / vehicles["audited"], 2) if vehicles["audited"] else None
    result.update(areas=areas, totals=totals, vehicles=vehicles,
        pending=sum(str(r["unit_id"]) not in first_audits for r in result["records"]),
        basis="first_complete_quality_audit")
    return result

