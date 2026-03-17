import sys
import os
import pandas as pd
import io
from fastapi import FastAPI, Request, Depends, Body, UploadFile, File
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, cast, String, text, inspect
import uvicorn
from zoneinfo import ZoneInfo
import datetime

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
        if "localizacao" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS localizacao VARCHAR"))

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
    "SERRA",
    "EXPE.",
    "DESMONT",
    "ELETRICA",
    "REVEST",
    "BCO",
    "ACESSÓ.",
    "PLOTA.",
    "LIBERA"
]

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

def parse_local_dt(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)

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

# Define regras de filtragem por etapa
# Ajustado para validar contra "NÃO" e "SIM" conforme consta no banco de dados
ETAPA_REGRAS = {
    "VIDROS": lambda s: s.get("VIDROS") == "NÃO",
    "A/C": lambda s: s.get("A/C") == "NÃO",
    "PREP": lambda s: s.get("PREP") == "NÃO",
    "SERRA": lambda s: s.get("SERRA") == "NÃO",
    "EXPE.": lambda s: s.get("EXPE.") == "NÃO",
    "DESMONT": lambda s: s.get("VIDROS") in ["SIM", "N/A"] and s.get("A/C") in ["SIM", "N/A"] and s.get("DESMONT") == "NÃO",
    "ELETRICA": lambda s: s.get("DESMONT") in ["SIM", "N/A"] and s.get("ELETRICA") == "NÃO",
    "REVEST": lambda s: s.get("DESMONT") in ["SIM", "N/A"] and s.get("REVEST") == "NÃO",
    "BCO": lambda s: s.get("REVEST") in ["SIM", "N/A"] and s.get("BCO") == "NÃO",
    "ACESSÓ.": lambda s: s.get("ACESSÓ.") == "NÃO",
    "PLOTA.": lambda s: s.get("PLOTA.") == "NÃO",
    "LIBERA": lambda s: s.get("BCO") in ["SIM", "N/A"] and s.get("LIBERA") == "NÃO"
}

@app.get("/")
async def home(request: Request, db: Session = Depends(database.get_db), modelo: str = None, etapa: str = None):
    query = db.query(models.Veiculo)

    # Filtragem por texto (Modelo ou Chassi)
    # Adicionado func.coalesce para evitar que valores NULL quebrem a busca LIKE
    if modelo and modelo.strip():
        termo = f"%{modelo.strip().upper()}%"
        query = query.filter(
            or_(
                func.upper(func.coalesce(cast(models.Veiculo.modelo, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.chassi, String), "")).like(termo)
            )
        )

    veiculos_db = query.order_by(models.Veiculo.ordem.asc()).all()
    veiculos_exibicao = []

    for v in veiculos_db:
        chassi_key = str(v.chassi).strip()
        apontamentos = db.query(models.Apontamento).filter(
            func.trim(cast(models.Apontamento.chassi, String)) == chassi_key
        ).all()

        # Cria mapeamento de status atualizado para o veículo
        status_map = {
            str(a.etapa).strip().upper(): str(a.status).strip().upper()
            for a in apontamentos
        }

        # Cálculo de progresso
        concluidos = sum(
            1 for e in ETAPAS_PRODUCAO
            if status_map.get(e.upper()) in ["SIM", "S", "OK", "N/A"]
        )
        v.progresso = int((concluidos / len(ETAPAS_PRODUCAO)) * 100) if ETAPAS_PRODUCAO else 0

        # Determinação da etapa atual
        v.etapa_atual = "FINALIZADO"
        for e in ETAPAS_PRODUCAO:
            if status_map.get(e.upper()) not in ["SIM", "S", "OK", "N/A"]:
                v.etapa_atual = e
                break

        # FILTRAGEM POR ETAPA (Lógica de Negócio)
        if etapa and etapa.strip():
            filtro = etapa.strip().upper()
            # Normalização para garantir comparação correta
            status_map_s = {k.strip().upper(): v.strip().upper() for k, v in status_map.items()}
            
            if filtro in ETAPA_REGRAS:
                if ETAPA_REGRAS[filtro](status_map_s):
                    veiculos_exibicao.append(v)
        else:
            veiculos_exibicao.append(v)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "veiculos": veiculos_exibicao,
            "etapas": ETAPAS_PRODUCAO,
            "termo_busca": modelo or "",
            "etapa_selecionada": etapa or ""
        }
    )

@app.get("/veiculo/{chassi}")
async def detalhes(request: Request, chassi: str, db: Session = Depends(database.get_db)):
    c_limpo = chassi.strip()

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
        str(f.etapa).strip().upper(): f
        for f in feitos
    }

    return templates.TemplateResponse(
        "detalhes.html",
        {
            "request": request,
            "veiculo": veiculo,
            "etapas": ETAPAS_PRODUCAO,
            "apont_map": apont_map,
            "localizacoes": LOCALIZACOES
        }
    )

@app.post("/upload")
async def upload_base(file: UploadFile = File(...), db: Session = Depends(database.get_db)):
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
            localizacao = get_col(row, "LOCALIZACAO", "LOCALIZAÇÃO")

            db.add(models.Veiculo(
                chassi=ch_raw,
                modelo=modelo,
                ordem=int(idx) + 1,
                ar_condicionado=ar_cond,
                cj_bco=cj_bco,
                cliente=cliente,
                destino=destino,
                localizacao=localizacao
            ))

            for etapa in ETAPAS_PRODUCAO:
                if etapa in df.columns:
                    val = str(row[etapa]).strip().upper()
                    status = (
                        "SIM" if val in ["S", "SIM", "OK"]
                        else "NÃO" if val in ["N", "NÃO", "X"]
                        else "N/A"
                    )
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

@app.post("/apontar")
async def salvar(data: dict = Body(...), db: Session = Depends(database.get_db)):
    ch = str(data["chassi"]).strip()
    et = str(data["etapa"]).strip().upper()
    st = str(data.get("status", "")).strip().upper()
    if not st:
        st = "N/A"
    responsavel = str(data.get("responsavel", "")).strip()
    inicio = parse_local_dt(data.get("inicio"))
    termino = parse_local_dt(data.get("termino"))

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

    # Registra no histórico
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

@app.get("/exportar_historico")
async def exportar(db: Session = Depends(database.get_db)):
    logs = db.query(models.Historico).all()
    if not logs:
        return {"message": "Sem dados"}

    veiculos = db.query(models.Veiculo).all()
    loc_map = {str(v.chassi).strip(): v.localizacao for v in veiculos}

    df = pd.DataFrame([
        {
            "CHASSI": l.chassi,
            "MODELO": l.modelo,
            "ETAPA": l.etapa,
            "STATUS": l.status,
            "RESPONSAVEL": l.responsavel,
            "INICIO": to_excel_dt(l.inicio),
            "TERMINO": to_excel_dt(l.termino),
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

@app.get("/limpar_historico")
async def limpar_logs(db: Session = Depends(database.get_db)):
    db.query(models.Historico).delete()
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/importar")
async def pg_importar(request: Request):
    return templates.TemplateResponse("importar.html", {"request": request})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8001))
    uvicorn.run(app, host="0.0.0.0", port=port)
