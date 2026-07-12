import sys
import os
import pandas as pd
import io
import threading
import webbrowser
from fastapi import FastAPI, Request, Depends, Body, UploadFile, File, Form
from fastapi.responses import StreamingResponse, RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, cast, String, text, inspect
import uvicorn
from zoneinfo import ZoneInfo
import datetime
import unicodedata

# Configuração de diretórios e templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

import database, models

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

# Inicialização do banco de dados
database.Base.metadata.create_all(bind=database.engine)

# Garante novas colunas sem migração formal
def ensure_columns():
    insp = inspect(database.engine)
    dialect = database.engine.dialect.name
    ts_tz = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"

    def column_names(table):
        if not insp.has_table(table):
            return set()
        return {c["name"] for c in insp.get_columns(table)}

    with database.engine.begin() as conn:
        veiculo_cols = column_names("veiculos")
        if "ar_condicionado" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS ar_condicionado VARCHAR"))
        if "ordem" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS ordem INTEGER"))
        if "cj_bco" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS cj_bco VARCHAR"))
        if "cliente" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS cliente VARCHAR"))
        if "destino" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS destino VARCHAR"))
        if "data_entrega" not in veiculo_cols:
            conn.execute(text(f"ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS data_entrega {ts_tz}"))
        if "localizacao" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS localizacao VARCHAR"))
        if "banco_presente" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS banco_presente VARCHAR"))
        if "banco_comentario" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS banco_comentario VARCHAR"))

        apont_cols = column_names("apontamentos")
        if "responsavel" not in apont_cols:
            conn.execute(text("ALTER TABLE apontamentos ADD COLUMN IF NOT EXISTS responsavel VARCHAR"))
        if "inicio" not in apont_cols:
            conn.execute(text(f"ALTER TABLE apontamentos ADD COLUMN IF NOT EXISTS inicio {ts_tz}"))
        if "termino" not in apont_cols:
            conn.execute(text(f"ALTER TABLE apontamentos ADD COLUMN IF NOT EXISTS termino {ts_tz}"))
        if "localizacao" not in apont_cols:
            conn.execute(text("ALTER TABLE apontamentos ADD COLUMN IF NOT EXISTS localizacao VARCHAR"))

        hist_cols = column_names("historico")
        if "responsavel" not in hist_cols:
            conn.execute(text("ALTER TABLE historico ADD COLUMN IF NOT EXISTS responsavel VARCHAR"))
        if "inicio" not in hist_cols:
            conn.execute(text(f"ALTER TABLE historico ADD COLUMN IF NOT EXISTS inicio {ts_tz}"))
        if "termino" not in hist_cols:
            conn.execute(text(f"ALTER TABLE historico ADD COLUMN IF NOT EXISTS termino {ts_tz}"))
        if "localizacao" not in hist_cols:
            conn.execute(text("ALTER TABLE historico ADD COLUMN IF NOT EXISTS localizacao VARCHAR"))
        if "data_apontamento" in hist_cols and dialect == "postgresql":
            conn.execute(text("ALTER TABLE historico ALTER COLUMN data_apontamento TYPE TIMESTAMP WITH TIME ZONE"))

ensure_columns()

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

ETAPAS_PRODUCAO = [
    "VIDROS",
    "A/C",
    "PREP",
    "SERRA.",
    "EXPE.",
    "DESMONT",
    "ELÉTRICA",
    "REVEST",
    "BCO",
    "ACESSÓ.",
    "PLOTA.",
    "LIBERA."
]

ETAPAS_STATUS_ATUAL = ["VIDROS", "A/C", "DESMONT", "REVEST", "BCO", "LIBERA."]

ETAPAS_FILTRO = [e for e in ETAPAS_PRODUCAO if e != "A/C"] + ["GE", "CLIM", "LIBERAÇÃO"]

LOCALIZACOES = [
    "Pátio",
    "J I",
    "Linha",
    "Tenda",
    "R1",
    "R2",
    "R3",
    "R4",
    "R5",
    "R6",
    "R7",
    "R8",
    "R9",
    "R10",
    "R11",
]

STATUS_CONCLUIDO = ["SIM", "S", "OK", "N/A"]

def normalize_status(value) -> str:
    val = safe_str(value).upper()
    if val in ["S", "SIM", "OK"]:
        return "SIM"
    if val in ["N", "NAO", "NÃO", "X"]:
        return "NÃO"
    if val in ["?", "PARCIAL"]:
        return "PARCIAL"
    return "N/A"

def parse_local_dt(value):
    if value is None or (hasattr(pd, "isna") and pd.isna(value)) or value == "":
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        try:
            dt = datetime.datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)

def parse_data_entrega(value):
    if value is None or (hasattr(pd, "isna") and pd.isna(value)) or value == "":
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time.min)
    if isinstance(value, datetime.datetime):
        dt = value
    else:
        parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
        if pd.isna(parsed):
            return None
        dt = parsed.to_pydatetime()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)

def format_data_entrega(value):
    if not value:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(LOCAL_TZ)
    return value.strftime("%d/%m/%Y")

def to_excel_dt(value):
    if not value:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(LOCAL_TZ).replace(tzinfo=None)

def to_input_dt(value):
    if not value:
        return ""
    if value.tzinfo is not None:
        value = value.astimezone(LOCAL_TZ)
    return value.strftime("%Y-%m-%dT%H:%M")

def get_user_name(request: Request):
    return (request.cookies.get("pcp_nome") or "").strip()

def require_login(request: Request):
    nome = get_user_name(request)
    return nome

def safe_str(value):
    if value is None or (hasattr(pd, "isna") and pd.isna(value)):
        return ""
    return str(value).strip()

def normalize_filter(value: str) -> str:
    text = safe_str(value).upper()
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )

def is_liberacao_filter(value: str) -> bool:
    return normalize_filter(value) == "LIBERACAO"

def normalize_etapa(value: str) -> str:
    if not value:
        return ""
    v = str(value).strip().upper()
    v = v.replace("  ", " ")

    if v in ["AC", "A/C"]:
        return "A/C"
    if v in ["LIBERA", "LIBERA."]:
        return "LIBERA."
    if v in ["ACESSO", "ACESSO.", "ACESSÓ", "ACESSÓ."]:
        return "ACESSÓ."
    if v in ["SERRA", "SERRA."]:
        return "SERRA."
    if v in ["DESMON", "DESMONT"]:
        return "DESMONT"
    if v in ["ELETRICA", "ELÉTRICA", "ELÉTRIC", "ELÉTRIC."]:
        return "ELÉTRICA"
    return v

# Define regras de filtragem por etapa
# Ajustado para validar contra "NÃO" e "SIM" conforme consta no banco de dados
ETAPA_REGRAS = {
    "VIDROS": lambda s: s.get("VIDROS") == "NÃO",
    "A/C": lambda s: s.get("A/C") == "NÃO",
    "PREP": lambda s: s.get("PREP") == "NÃO",
    "SERRA.": lambda s: s.get("SERRA.") == "NÃO",
    "EXPE.": lambda s: s.get("EXPE.") == "NÃO",
    "DESMONT": lambda s: s.get("VIDROS") in ["SIM", "N/A"] and s.get("A/C") in ["SIM", "N/A"] and s.get("DESMONT") == "NÃO",
    "ELÉTRICA": lambda s: s.get("DESMONT") in ["SIM", "N/A"] and s.get("ELÉTRICA") == "NÃO",
    "REVEST": lambda s: s.get("DESMONT") in ["SIM", "N/A"] and s.get("REVEST") == "NÃO",
    "BCO": lambda s: s.get("REVEST") in ["SIM", "N/A"] and s.get("BCO") == "NÃO",
    "ACESSÓ.": lambda s: s.get("ACESSÓ.") == "NÃO",
    "PLOTA.": lambda s: s.get("PLOTA.") == "NÃO",
    "LIBERA.": lambda s: s.get("BCO") in ["SIM", "N/A"] and s.get("LIBERA.") == "NÃO"
}

@app.get("/")
async def home(request: Request, db: Session = Depends(database.get_db), modelo: str = None, etapa: str = None, visao: str = "resumida"):
    if not require_login(request):
        return RedirectResponse(url="/login", status_code=303)
    query = db.query(models.Veiculo)
    modo_liberacao = is_liberacao_filter(etapa)
    visao_atual = "completa" if safe_str(visao).lower() == "completa" else "resumida"
    if modo_liberacao:
        visao_atual = "resumida"
    modo_resumido = modo_liberacao or visao_atual == "resumida"

    # Filtragem por texto (Modelo, Chassi, Ar Condicionado, CJ. BCO, Localização)
    # Adicionado func.coalesce para evitar que valores NULL quebrem a busca LIKE
    if modelo and modelo.strip():
        termo = f"%{modelo.strip().upper()}%"
        query = query.filter(
            or_(
                func.upper(func.coalesce(cast(models.Veiculo.modelo, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.chassi, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.ar_condicionado, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.cj_bco, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.data_entrega, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.localizacao, String), "")).like(termo)
            )
        )

    veiculos_db = query.order_by(models.Veiculo.ordem.asc()).all()
    veiculos_exibicao = []

    chassis = [str(v.chassi).strip() for v in veiculos_db]
    apontamentos = []
    if chassis:
        apontamentos = db.query(models.Apontamento).filter(
            func.trim(cast(models.Apontamento.chassi, String)).in_(chassis)
        ).all()

    apont_por_chassi = {}
    for a in apontamentos:
        ch_key = str(a.chassi).strip()
        if ch_key not in apont_por_chassi:
            apont_por_chassi[ch_key] = []
        apont_por_chassi[ch_key].append(a)

    for v in veiculos_db:
        v.data_entrega_fmt = format_data_entrega(v.data_entrega)
        chassi_key = str(v.chassi).strip()
        aponts = apont_por_chassi.get(chassi_key, [])

        # Cria mapeamento de status atualizado para o veículo
        status_map = {
            normalize_etapa(a.etapa): str(a.status).strip().upper()
            for a in aponts
        }

        # Cálculo de progresso
        concluidos = sum(
            1 for e in ETAPAS_PRODUCAO
            if status_map.get(e.upper()) in STATUS_CONCLUIDO
        )
        v.progresso = int((concluidos / len(ETAPAS_PRODUCAO)) * 100) if ETAPAS_PRODUCAO else 0

        # Determinação da etapa atual
        v.etapa_atual = "FINALIZADO"
        for e in ETAPAS_STATUS_ATUAL:
            if status_map.get(e.upper()) not in STATUS_CONCLUIDO:
                v.etapa_atual = e
                break

        # FILTRAGEM POR ETAPA (Lógica de Negócio)
        if etapa and etapa.strip() and not modo_liberacao:
            filtro = etapa.strip().upper()
            if filtro in ["GE", "CLIM"]:
                status_map_s = {normalize_etapa(k): v.strip().upper() for k, v in status_map.items()}
                if (v.ar_condicionado or "").strip().upper() == filtro and ETAPA_REGRAS["A/C"](status_map_s):
                    veiculos_exibicao.append(v)
                continue
            if filtro == "BCO":
                banco_flag = (v.banco_presente or "").strip().upper()
                if banco_flag in ["N", "NAO", "NÃO", "NAO TEM", "SEM", "0"]:
                    continue
            # Normalização para garantir comparação correta
            status_map_s = {normalize_etapa(k): v.strip().upper() for k, v in status_map.items()}
            
            if filtro in ETAPA_REGRAS:
                if ETAPA_REGRAS[filtro](status_map_s):
                    veiculos_exibicao.append(v)
        else:
            veiculos_exibicao.append(v)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "veiculos": veiculos_exibicao,
            "etapas": ETAPAS_FILTRO,
            "termo_busca": modelo or "",
            "etapa_selecionada": etapa or "",
            "modo_liberacao": modo_liberacao,
            "modo_resumido": modo_resumido,
            "visao_atual": visao_atual
        }
    )

@app.get("/veiculo/{chassi}")
async def detalhes(request: Request, chassi: str, db: Session = Depends(database.get_db)):
    if not require_login(request):
        return RedirectResponse(url="/login", status_code=303)
    c_limpo = chassi.strip()
    user_name = get_user_name(request)

    veiculo = db.query(models.Veiculo).filter(
        func.trim(cast(models.Veiculo.chassi, String)) == c_limpo
    ).first()

    feitos = db.query(models.Apontamento).filter(
        func.trim(cast(models.Apontamento.chassi, String)) == c_limpo
    ).all()

    for f in feitos:
        f.inicio_str = to_input_dt(f.inicio)
        f.termino_str = to_input_dt(f.termino)

    apont_map = {
        normalize_etapa(f.etapa): f
        for f in feitos
    }

    if veiculo:
        veiculo.data_entrega_fmt = format_data_entrega(veiculo.data_entrega)

    return templates.TemplateResponse(
        request,
        "detalhes.html",
        {
            "request": request,
            "veiculo": veiculo,
            "etapas": ETAPAS_PRODUCAO,
            "apont_map": apont_map,
            "localizacoes": LOCALIZACOES,
            "user_name": user_name
        }
    )

@app.post("/upload")
async def upload_base(request: Request, file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    # Protege upload com login simples
    # (mantém compatível sem usuários cadastrados)
    if not require_login(request):
        return {"status": "erro", "detail": "Login necessário"}
    try:
        content = await file.read()

        df = (
            pd.read_excel(io.BytesIO(content))
            if file.filename.endswith(".xlsx")
            else pd.read_csv(io.BytesIO(content))
        )

        df.columns = [str(c).upper().strip() for c in df.columns]

        def get_col(row, *names):
            for n in names:
                if n in df.columns:
                    val = row.get(n, "")
                    if pd.isna(val):
                        return ""
                    return str(val).strip()
            return ""

        etapas_col = {normalize_etapa(c): c for c in df.columns}

        # Limpa dados anteriores para nova carga
        db.query(models.Apontamento).delete()
        db.query(models.Veiculo).delete()
        db.commit()

        for idx, row in df.iterrows():
            ch_raw = str(row.get("CHASSI", "")).strip().split(".")[0]
            if not ch_raw or ch_raw.lower() == "nan":
                continue

            modelo = str(row.get("MMMV", "")).strip().upper()
            ar_cond = get_col(row, "AR CONDICIONADO", "AR_CONDICIONADO", "AR-CONDICIONADO", "ARCONDICIONADO")
            cj_bco = get_col(row, "CJ. BCO", "CJ BCO", "CJ_BCO", "CJ-BCO")
            cliente = get_col(row, "CLIENTE")
            destino = get_col(row, "DESTINO")
            data_entrega = parse_data_entrega(get_col(row, "DATA DE ENTREGA", "DATA_ENTREGA", "DT ENTREGA", "ENTREGA"))
            localizacao = get_col(row, "LOCALIZACAO", "LOCALIZAÇÃO")
            banco_presente = get_col(row, "BANCO", "BANCO_PRESENTE", "POSSUI BANCO", "TEM BANCO")
            banco_comentario = get_col(row, "COMENTARIO BANCO", "COMENTARIO_BANCO", "BANCO OBS", "OBS BANCO")

            db.add(models.Veiculo(
                chassi=ch_raw,
                modelo=modelo,
                ordem=int(idx) + 1,
                ar_condicionado=ar_cond,
                cj_bco=cj_bco,
                cliente=cliente,
                destino=destino,
                data_entrega=data_entrega,
                localizacao=localizacao,
                banco_presente=banco_presente,
                banco_comentario=banco_comentario
            ))

            for etapa in ETAPAS_PRODUCAO:
                col_name = etapas_col.get(normalize_etapa(etapa))
                if col_name:
                    status = normalize_status(row[col_name])
                else:
                    status = "N/A"
                db.add(models.Apontamento(
                    chassi=ch_raw,
                    etapa=etapa,
                    status=status
                ))

        db.commit()
        return {"status": "sucesso"}

    except Exception as e:
        db.rollback()
        return {"status": "erro", "detail": str(e)}

@app.post("/upload_apontamentos")
async def upload_apontamentos(request: Request, file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    if not require_login(request):
        return {"status": "erro", "detail": "Login necessário"}
    try:
        content = await file.read()

        df = (
            pd.read_excel(io.BytesIO(content))
            if file.filename.endswith(".xlsx")
            else pd.read_csv(io.BytesIO(content))
        )

        df.columns = [str(c).upper().strip() for c in df.columns]

        # Normaliza e agrega para evitar N+1
        rows = {}
        banco_updates = {}

        for _, row in df.iterrows():
            ch_raw = safe_str(row.get("CHASSI", "")).split(".")[0]
            if not ch_raw or ch_raw.lower() == "nan":
                continue

            etapa = normalize_etapa(safe_str(row.get("ETAPA", "")))
            inicio = parse_local_dt(row.get("INICIO"))
            termino = parse_local_dt(row.get("TERMINO"))
            responsavel = safe_str(row.get("RESPONSAVEL", ""))

            banco_presente = safe_str(row.get("BANCO", ""))
            banco_comentario = safe_str(row.get("COMENTARIO BANCO", row.get("COMENTARIO_BANCO", "")))
            if banco_presente or banco_comentario:
                banco_updates[ch_raw] = {
                    "banco_presente": banco_presente,
                    "banco_comentario": banco_comentario
                }

            if not etapa:
                continue

            rows[(ch_raw, etapa)] = {
                "inicio": inicio,
                "termino": termino,
                "responsavel": responsavel
            }

        # Atualiza banco/comentário em lote
        for ch_raw, data in banco_updates.items():
            update_data = {}
            if data.get("banco_presente"):
                update_data["banco_presente"] = data["banco_presente"]
            if data.get("banco_comentario"):
                update_data["banco_comentario"] = data["banco_comentario"]
            if update_data:
                db.query(models.Veiculo).filter(
                    func.trim(cast(models.Veiculo.chassi, String)) == ch_raw
                ).update(update_data)

        if rows:
            chassis = list({k[0] for k in rows.keys()})
            existentes = db.query(models.Apontamento).filter(
                func.trim(cast(models.Apontamento.chassi, String)).in_(chassis)
            ).all()
            existentes_map = {
                (str(a.chassi).strip(), normalize_etapa(a.etapa)): a
                for a in existentes
            }

            for (ch_raw, etapa), data in rows.items():
                ap = existentes_map.get((ch_raw, etapa))
                if not ap:
                    ap = models.Apontamento(
                        chassi=ch_raw,
                        etapa=etapa,
                        status="N/A"
                    )
                    db.add(ap)
                ap.inicio = data["inicio"]
                ap.termino = data["termino"]
                ap.responsavel = data["responsavel"]

        db.commit()
        return {"status": "sucesso"}

    except Exception as e:
        db.rollback()
        return {"status": "erro", "detail": str(e)}

@app.post("/apontar")
async def salvar(request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    ch = str(data["chassi"]).strip()
    et = normalize_etapa(data["etapa"])
    st = normalize_status(data.get("status", ""))
    if not st:
        st = "N/A"
    responsavel = str(data.get("responsavel", "")).strip()
    inicio = parse_local_dt(data.get("inicio"))
    termino = parse_local_dt(data.get("termino"))

    registrar_historico = bool(data.get("registrar_historico", True))

    # Atualiza ou cria o apontamento
    db.query(models.Apontamento).filter(
        func.trim(cast(models.Apontamento.chassi, String)) == ch,
        func.trim(cast(models.Apontamento.etapa, String)) == et
    ).delete()

    db.add(models.Apontamento(
        chassi=ch,
        etapa=et,
        status=st,
        responsavel=responsavel,
        inicio=inicio,
        termino=termino,
        localizacao=None
    ))

    v = db.query(models.Veiculo).filter(
        func.trim(cast(models.Veiculo.chassi, String)) == ch
    ).first()

    # Registra no histórico apenas quando for status (SIM/NÃO/N/A/PARCIAL) e explícito
    if registrar_historico:
        db.add(models.Historico(
            chassi=ch,
            modelo=v.modelo if v else "N/A",
            etapa=et,
            status=st,
            responsavel=responsavel,
            inicio=inicio,
            termino=termino,
            localizacao=None
        ))

    db.commit()
    return {"status": "ok"}

@app.post("/veiculo_localizacao")
async def atualizar_localizacao(data: dict = Body(...), db: Session = Depends(database.get_db)):
    ch = str(data.get("chassi", "")).strip()
    localizacao = str(data.get("localizacao", "")).strip()
    if not ch:
        return {"status": "erro", "detail": "Chassi inválido"}

    db.query(models.Veiculo).filter(
        func.trim(cast(models.Veiculo.chassi, String)) == ch
    ).update({"localizacao": localizacao})
    db.commit()
    return {"status": "ok"}

@app.post("/veiculo_banco")
async def atualizar_banco(data: dict = Body(...), db: Session = Depends(database.get_db)):
    ch = str(data.get("chassi", "")).strip()
    banco_presente = str(data.get("banco_presente", "")).strip()
    banco_comentario = str(data.get("banco_comentario", "")).strip()
    if not ch:
        return {"status": "erro", "detail": "Chassi inválido"}

    db.query(models.Veiculo).filter(
        func.trim(cast(models.Veiculo.chassi, String)) == ch
    ).update({
        "banco_presente": banco_presente,
        "banco_comentario": banco_comentario
    })
    db.commit()
    return {"status": "ok"}

@app.get("/exportar_historico")
async def exportar(db: Session = Depends(database.get_db)):
    logs = db.query(models.Historico).all()
    if not logs:
        return {"message": "Sem dados"}

    veiculos = db.query(models.Veiculo).all()
    loc_map = {str(v.chassi).strip(): v.localizacao for v in veiculos}
    apont_map = {}
    aponts = db.query(models.Apontamento).all()
    for a in aponts:
        apont_map[(str(a.chassi).strip(), normalize_etapa(a.etapa))] = a

    df = pd.DataFrame([
        {
            "CHASSI": l.chassi,
            "MODELO": l.modelo,
            "ETAPA": normalize_etapa(l.etapa),
            "STATUS": l.status,
            "RESPONSAVEL": (apont_map.get((str(l.chassi).strip(), normalize_etapa(l.etapa))) or l).responsavel,
            "INICIO": to_excel_dt((apont_map.get((str(l.chassi).strip(), normalize_etapa(l.etapa))) or l).inicio),
            "TERMINO": to_excel_dt((apont_map.get((str(l.chassi).strip(), normalize_etapa(l.etapa))) or l).termino),
            "LOCALIZACAO": loc_map.get(str(l.chassi).strip()),
            "DATA": to_excel_dt(l.data_apontamento)
        }
        for l in logs
    ])

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False)

    out.seek(0)

    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=relatorio.xlsx"}
    )

@app.get("/exportar_tempos")
async def exportar_tempos(db: Session = Depends(database.get_db)):
    aponts = db.query(models.Apontamento).all()
    veiculos = db.query(models.Veiculo).all()
    loc_map = {str(v.chassi).strip(): v.localizacao for v in veiculos}
    modelo_map = {str(v.chassi).strip(): v.modelo for v in veiculos}

    df = pd.DataFrame([
        {
            "CHASSI": a.chassi,
            "MODELO": modelo_map.get(str(a.chassi).strip()),
            "ETAPA": normalize_etapa(a.etapa),
            "RESPONSAVEL": a.responsavel,
            "INICIO": to_excel_dt(a.inicio),
            "TERMINO": to_excel_dt(a.termino),
            "LOCALIZACAO": loc_map.get(str(a.chassi).strip())
        }
        for a in aponts
    ])

    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        df.to_excel(w, index=False)

    out.seek(0)

    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=tempos_localizacao.xlsx"}
    )

@app.get("/limpar_historico")
async def limpar_logs(db: Session = Depends(database.get_db)):
    db.query(models.Historico).delete()
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/importar")
async def pg_importar(request: Request):
    if not require_login(request):
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "importar.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request})

@app.post("/login")
async def login_post(request: Request, nome: str = Form(...)):
    nome_limpo = str(nome).strip()
    if not nome_limpo:
        return RedirectResponse(url="/login", status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("pcp_nome", nome_limpo, max_age=60 * 60 * 24 * 30)
    return resp

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8010))
    host = os.environ.get("HOST", "127.0.0.1")
    url = f"http://127.0.0.1:{port}"
    if os.environ.get("OPEN_BROWSER", "1") != "0":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port)
