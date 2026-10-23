"""Quality occurrences and FTT over current MES stage completions."""
from datetime import date, datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo
from uuid import UUID, uuid4
import unicodedata
from collections import Counter, defaultdict
from sqlalchemy import MetaData, Table, Column, String, Integer, Date, DateTime, Uuid, select, text, inspect

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
    Column("actor", String(100), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False))
issues = Table("erp_quality_issues", metadata,
    Column("id", String(36), primary_key=True), Column("report_id", String(36), nullable=False),
    Column("defect_id", String(36), nullable=False), Column("description", String(200), nullable=False),
    Column("stage_code", String(40), nullable=False), Column("disposition", String(12), nullable=False))
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

def work_options(conn, search=""):
    term = "%" + str(search or "").strip()[:100].upper() + "%"
    return [dict(r) for r in conn.execute(text("""
        select w.id,w.numero_os,e.item_number,v.chassi,w.status
        from erp_work_orders w join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        where w.is_current=true and w.status not in ('CANCELADA','RETIRADA')
          and (upper(w.numero_os) like :q or upper(v.chassi) like :q
               or cast(e.item_number as varchar) like :q)
        order by e.item_number desc limit 50
    """), {"q": term}).mappings()]

def create_report(conn, data, user, stage_codes):
    require_ready(conn)
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
        existing = list(conn.execute(select(issues).where(issues.c.report_id == identifier)).mappings())
        requested = sorted((str(x.get("defect_id")), str(x.get("stage_code")), "DEFEITO")
                           for x in data.get("issues", []))
        saved = sorted((x["defect_id"], x["stage_code"], x["disposition"]) for x in existing)
        if old["actor_id"] != user.id or old["work_order_id"] != work_id or old["observed_on"] != observed or requested != saved:
            raise ValueError("Identificador já usado em outro lançamento. Atualize a tela.")
        return {"id": identifier, "replayed": True}
    if observed < START or observed > today():
        raise ValueError("A data deve estar entre 26/10/2026 e hoje.")
    work = conn.execute(text("select id from erp_work_orders where id=:id and is_current=true and status not in ('CANCELADA','RETIRADA')"),
                        {"id": work_id}).first()
    if not work:
        raise ValueError("O.S. vigente não encontrada.")
    entries = data.get("issues")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 100:
        raise ValueError("Inclua de 1 a 100 problemas.")
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
        applicable = conn.execute(text("select aplicavel from erp_work_order_stages where work_order_id=:work and stage_code=:code"),
                                  {"work": work_id, "code": code}).scalar()
        if applicable is not True and applicable != 1:
            raise ValueError("A área selecionada não é aplicável à O.S.")
        seen.add((code, defect_id))
        rows.append(dict(id=str(uuid4()), report_id=identifier, defect_id=defect_id,
                         description=valid[defect_id], stage_code=code, disposition=disposition))
    conn.execute(reports.insert().values(id=identifier, work_order_id=work_id, observed_on=observed,
        actor_id=user.id, actor=user.username, created_at=datetime.now(timezone.utc)))
    conn.execute(issues.insert(), rows)
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

def dashboard(conn, stage_codes, start=None, end=None):
    require_ready(conn)
    first, last = period(start, end)
    cutoff = datetime.combine(first, time.min, TZ)
    until = datetime.combine(last + timedelta(days=1), time.min, TZ)
    completions = [dict(r) for r in conn.execute(text("""
        select s.work_order_id,w.vehicle_entry_id as unit_id,s.stage_code,s.termino
        from erp_work_order_stages s join erp_work_orders w on w.id=s.work_order_id
        where s.aplicavel=true and s.status='CONCLUÍDA' and s.termino>=:cutoff and s.termino<:until
          and w.is_current=true and w.status not in ('CANCELADA','RETIRADA')
    """), {"cutoff":cutoff,"until":until}).mappings()]
    rows = [dict(r) for r in conn.execute(text("""
        select i.description,i.stage_code,i.disposition,r.work_order_id,r.observed_on,
               r.actor,r.created_at,r.id,w.numero_os,w.vehicle_entry_id as unit_id,v.chassi
        from erp_quality_issues i join erp_quality_reports r on r.id=i.report_id
        join erp_work_orders w on w.id=r.work_order_id
        join erp_vehicle_entries e on e.id=w.vehicle_entry_id
        join erp_vehicles v on v.id=e.vehicle_id
        where r.observed_on>=:cutoff
        order by r.observed_on desc,r.created_at desc,i.id
    """), {"cutoff":START}).mappings()]
    return calculate(completions, rows, stage_codes, first, last)

