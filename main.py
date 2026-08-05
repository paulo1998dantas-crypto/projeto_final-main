import sys
import os
import pandas as pd
import io
import threading
import webbrowser
import secrets
import hashlib
import hmac
from fastapi import FastAPI, Request, Depends, Body, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse, RedirectResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import or_, func, cast, String, text, inspect
import uvicorn
from zoneinfo import ZoneInfo
import datetime
import unicodedata
import re
from types import SimpleNamespace

# Configuração de diretórios e templates
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

import database, models
import erp_service
import erp_catalogs
import erp_report
import authz
import portal_sso

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

def legacy_schema_auto_migrate_enabled():
    """Allow legacy auto-DDL only in an explicitly controlled environment."""
    return os.environ.get(
        "MES_LEGACY_SCHEMA_AUTO_MIGRATE", "false"
    ).strip().lower() in {"1", "true", "yes", "sim", "on"}


if legacy_schema_auto_migrate_enabled():
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
        if "linha" not in veiculo_cols:
            conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS linha VARCHAR"))
        if "semana_producao" not in veiculo_cols:
            if dialect == "postgresql":
                conn.execute(text("ALTER TABLE veiculos ADD COLUMN IF NOT EXISTS semana_producao VARCHAR"))
            else:
                conn.execute(text("ALTER TABLE veiculos ADD COLUMN semana_producao VARCHAR"))
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
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_veiculos_semana_producao ON veiculos (semana_producao)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_veiculos_data_entrega ON veiculos (data_entrega)"))

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

if legacy_schema_auto_migrate_enabled():
    ensure_columns()

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def erp_feature_enabled():
    return os.environ.get("ERP_FEATURE_FLAG", "false").strip().lower() in {"1", "true", "yes", "sim", "on"}


def legacy_operational_schema_available():
    inspector = inspect(database.engine)
    return all(
        inspector.has_table(table_name)
        for table_name in ("veiculos", "apontamentos", "historico")
    )


def legacy_upload_enabled():
    return os.environ.get(
        "ERP_MES_LEGACY_UPLOAD_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "sim", "on"}


def legacy_disabled_response():
    return JSONResponse(
        {
            "status": "erro",
            "detail": "Fluxo legado do MES desativado neste ambiente.",
        },
        status_code=404,
    )


def erp_backend_actor(request: Request):
    supplied = request.headers.get("X-ERP-Backend-Token", "").strip()
    expected = os.environ.get("ERP_BACKEND_TOKEN", "").strip()
    client_host = request.client.host if request.client else ""
    local_fallback = (
        not expected
        and not os.environ.get("RENDER")
        and client_host in {"127.0.0.1", "::1", "localhost"}
        and bool(supplied)
    )
    if not local_fallback and (not expected or not supplied or not hmac.compare_digest(expected, supplied)):
        return None
    return request.headers.get("X-ERP-Actor", "").strip() or "integração-suprimentos"


def erp_disabled_response():
    return JSONResponse({"ok": False, "error": "Integração ERP desativada pela feature flag."}, status_code=404)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Render health check without exposing credentials or operational rows."""
    try:
        with database.engine.connect() as conn:
            conn.execute(text("select 1"))
        inspector = inspect(database.engine)
        auth_schema_ready = (
            authz.shared_schema_status(database.engine)["ready"]
            if authz.shared_auth_enabled()
            else (
                inspector.has_table("usuarios")
                and inspector.has_table("sessoes_usuario")
            )
        )
        payload = {
            "ok": True,
            "database": True,
            "erp_feature": erp_feature_enabled(),
            "erp_schema": inspector.has_table("erp_work_orders"),
            "auth_mode": authz.auth_mode(),
            "legacy_schema": legacy_operational_schema_available(),
            "auth_schema": auth_schema_ready,
        }
        if authz.shared_auth_enabled() and not auth_schema_ready:
            payload["ok"] = False
            return JSONResponse(payload, status_code=503)
        return payload
    except Exception:
        return JSONResponse(
            {
                "ok": False,
                "database": False,
                "erp_feature": erp_feature_enabled(),
            },
            status_code=503,
        )


@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.middleware("http")
async def central_portal_login_gate(request: Request, call_next):
    """Keep direct MES links behind the central Portal when the flag is on."""
    if not portal_sso.enabled():
        return await call_next(request)
    path = request.url.path
    public_paths = {"/login", "/logout", "/healthz", "/_sso/consume", "/favicon.ico"}
    if (
        path in public_paths
        or path.startswith("/static/")
        or path.startswith("/api/erp/internal/")
    ):
        return await call_next(request)
    db = database.SessionLocal()
    try:
        current = get_current_user(request, db)
    finally:
        db.close()
    if current:
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "Login obrigatorio."}, status_code=401)
    target = path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(url=portal_sso.portal_login_url("MES", target), status_code=303)

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
    "LIBERA.",
]

ETAPAS_STATUS_ATUAL = ETAPAS_PRODUCAO

ETAPAS_FILTRO = [e for e in ETAPAS_PRODUCAO if e != "A/C"] + ["GE", "CLIM", "ENTREGAS"]

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
STATUS_PENDENTE = ["NÃO", "PARCIAL"]

def normalize_status(value) -> str:
    val = safe_str(value).upper()
    if val in ["S", "SIM", "OK"]:
        return "SIM"
    if val in ["N", "NAO", "NÃO", "NOK", "X"]:
        return "NÃO"
    if val in ["?", "P", "PARCIAL"]:
        return "PARCIAL"
    return "N/A"

def status_upload_valido(value) -> bool:
    return normalize_filter(value) in ["S", "SIM", "OK", "N", "NAO", "NOK", "X", "?", "P", "PARCIAL", "N/A", "NA"]

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
        texto = safe_str(value)
        if re.fullmatch(r"\d+(?:[.,]\d+)?", texto):
            serial = float(texto.replace(",", "."))
            if 20000 <= serial <= 80000:
                dt = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=serial)
                return dt.replace(tzinfo=LOCAL_TZ)
        if re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\D|$)", texto):
            parsed = pd.to_datetime(texto, yearfirst=True, dayfirst=False, errors="coerce")
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

SESSION_COOKIE = "pcp_sessao"

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"{salt}${digest.hex()}"

def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt, digest_hex = password_hash.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(digest.hex(), digest_hex)

def bootstrap_admin_if_configured():
    if authz.shared_auth_enabled():
        return
    username = os.environ.get("MES_BOOTSTRAP_ADMIN_USER", "").strip()
    password = os.environ.get("MES_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not username or not password:
        return

    db = database.SessionLocal()
    try:
        usuario = db.query(models.Usuario).filter(
            func.upper(models.Usuario.nome) == username.upper()
        ).first()
        if not usuario:
            db.add(models.Usuario(
                nome=username,
                senha_hash=hash_password(password),
                is_admin=1,
            ))
            db.commit()
    finally:
        db.close()

def get_current_user(request: Request, db: Session):
    # Uma única tela pode passar pelo middleware, por uma dependência de rota e
    # pela renderização. Reutilize somente nesta requisição o principal já
    # validado: a próxima requisição continua consultando sessão/usuário e
    # portanto revogações e mudanças de perfil seguem imediatas.
    if hasattr(request.state, "mes_current_user"):
        return request.state.mes_current_user
    if authz.shared_auth_enabled():
        usuario = authz.get_shared_current_user(request, database.engine)
        request.state.mes_current_user = usuario
        return usuario
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if not token:
        request.state.mes_current_user = None
        return None
    sessao = db.query(models.SessaoUsuario).filter(models.SessaoUsuario.token == token).first()
    if not sessao:
        request.state.mes_current_user = None
        return None
    expira_em = sessao.expira_em
    if expira_em and expira_em.tzinfo is None:
        expira_em = expira_em.replace(tzinfo=LOCAL_TZ)
    if expira_em and expira_em < datetime.datetime.now(LOCAL_TZ):
        db.delete(sessao)
        db.commit()
        request.state.mes_current_user = None
        return None
    usuario = db.query(models.Usuario).filter(models.Usuario.id == sessao.usuario_id).first()
    principal = authz.principal_from_legacy(usuario) if usuario else None
    request.state.mes_current_user = principal
    return principal

def get_user_name(request: Request, db: Session):
    usuario = get_current_user(request, db)
    return usuario.nome if usuario else ""

def require_login(request: Request, db: Session):
    return get_current_user(request, db)

def create_session(db: Session, usuario):
    token = secrets.token_urlsafe(32)
    expira_em = datetime.datetime.now(LOCAL_TZ) + datetime.timedelta(days=30)
    db.add(models.SessaoUsuario(token=token, usuario_id=usuario.id, expira_em=expira_em))
    db.commit()
    return token

def set_session_cookie(response, token: str):
    if authz.shared_auth_enabled():
        response.set_cookie(
            authz.SHARED_SESSION_COOKIE,
            token,
            max_age=60 * 60 * 24 * authz.SESSION_DAYS,
            httponly=True,
            secure=bool(os.environ.get("RENDER")),
            samesite="lax",
        )
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie("pcp_nome")
        return
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=bool(os.environ.get("RENDER")),
        samesite="lax",
    )
    response.delete_cookie("pcp_nome")

def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(authz.SHARED_SESSION_COOKIE)
    response.delete_cookie("pcp_nome")


def permission_denied(api=False):
    payload = {"ok": False, "error": "Acesso não autorizado para este perfil."}
    return JSONResponse(payload, status_code=403) if api else HTMLResponse(payload["error"], status_code=403)


def has_permission(user, permission):
    return authz.can(user, permission)

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

def normalize_chassi(value) -> str:
    return "".join(safe_str(value).upper().split())

def chassi_exibicao(value) -> str:
    """Chassi abreviado para os quadros; a consulta continua usando o valor completo."""
    normalized = normalize_chassi(value)
    return normalized[-8:] if len(normalized) > 8 else normalized

MARCAS_MMV = {
    "MARCA",
    "MERCEDES",
    "MERCEDESBENZ",
    "BENZ",
    "MB",
    "MBB",
    "VOLKSWAGEN",
    "VW",
    "PEUGEOT",
    "CITROEN",
    "FIAT",
    "IVECO",
    "RENAULT",
    "FORD",
    "CHEVROLET",
    "GM",
    "TOYOTA",
    "HYUNDAI",
}

MODELOS_MMV_PRIORITARIOS = {
    "SPRINTER",
    "EXPERT",
    "DUCATO",
    "BOXER",
    "JUMPER",
    "MASTER",
    "DAILY",
    "TRANSIT",
    "CRAFTER",
    "PARTNER",
    "KANGOO",
    "SCUDO",
    "JUMPY",
}

def normalizar_token_mmv(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", normalize_filter(value))

def resumir_mmv(value) -> str:
    texto = re.sub(r"\s+", " ", safe_str(value)).strip(" -/")
    if not texto:
        return "-"

    tokens = [token.strip(" ,;:()[]{}") for token in texto.split()]
    tokens = [token for token in tokens if token]
    while tokens and normalizar_token_mmv(tokens[0]) in MARCAS_MMV:
        tokens.pop(0)
    if not tokens:
        return texto

    normalizados = [normalizar_token_mmv(t) for t in tokens]
    inicio = next((i for i, token in enumerate(normalizados) if token in MODELOS_MMV_PRIORITARIOS), 0)
    resumo = tokens[inicio:inicio + 2]
    return " ".join(resumo).title()

def normalize_linha(value) -> str:
    linha = normalize_filter(value)
    if linha in ["LB", "LAB", "LE", "LAE"]:
        return linha
    if linha in ["BASICA", "LINHA BASICA", "BASIC"]:
        return "BÁSICA"
    if linha in ["EXECUTIVA", "LINHA EXECUTIVA", "EXEC"]:
        return "EXECUTIVA"
    return ""

def normalize_semana_producao(value) -> str:
    semana = safe_str(value).upper()
    if not semana:
        return ""

    semana_sem_acento = normalize_filter(semana)
    numero_simples = re.fullmatch(r"0*(\d{1,2})(?:[.,]0+)?", semana_sem_acento)
    if numero_simples:
        numero = int(numero_simples.group(1))
        return str(numero) if 1 <= numero <= 53 else semana

    prefixada = re.fullmatch(r"(?:SEMANA|SEM\.?|S|W)\s*[-:]?\s*0*(\d{1,2})", semana_sem_acento)
    if prefixada:
        numero = int(prefixada.group(1))
        return str(numero) if 1 <= numero <= 53 else semana

    ano_primeiro = re.fullmatch(r"(\d{4})\s*[-/]?\s*(?:SEMANA|SEM\.?|S|W)?\s*0*(\d{1,2})", semana_sem_acento)
    if ano_primeiro:
        numero = int(ano_primeiro.group(2))
        return f"{ano_primeiro.group(1)}-S{numero:02d}" if 1 <= numero <= 53 else semana

    semana_primeiro = re.fullmatch(r"(?:SEMANA|SEM\.?|S|W)?\s*0*(\d{1,2})\s*[-/]\s*(\d{4})", semana_sem_acento)
    if semana_primeiro:
        numero = int(semana_primeiro.group(1))
        return f"{semana_primeiro.group(2)}-S{numero:02d}" if 1 <= numero <= 53 else semana

    return semana

def normalize_semanas_producao(value) -> list[str]:
    if value is None:
        valores = []
    elif isinstance(value, (list, tuple, set)):
        valores = value
    else:
        valores = [value]

    semanas = []
    vistas = set()
    for item in valores:
        semana = normalize_semana_producao(item)
        if semana and semana not in vistas:
            semanas.append(semana)
            vistas.add(semana)
    return semanas

def parse_data_filtro(value):
    try:
        return datetime.date.fromisoformat(safe_str(value))
    except ValueError:
        return None

def intervalo_entrega_normalizado(data_inicio=None, data_fim=None):
    inicio = parse_data_filtro(data_inicio)
    fim = parse_data_filtro(data_fim)
    if inicio and fim and inicio > fim:
        inicio, fim = fim, inicio
    return inicio, fim

def semana_ordenacao(value):
    semana = safe_str(value)
    numeros = [int(numero) for numero in re.findall(r"\d+", semana)]
    if len(numeros) >= 2 and numeros[0] >= 2000:
        return (numeros[0], numeros[1], semana)
    if len(numeros) >= 2 and numeros[1] >= 2000:
        return (numeros[1], numeros[0], semana)
    if numeros:
        return (0, numeros[0], semana)
    return (9999, 99, semana)

def listar_semanas_producao(db: Session):
    valores = []
    if legacy_operational_schema_available():
        valores = db.query(models.Veiculo.semana_producao).filter(
            func.trim(func.coalesce(models.Veiculo.semana_producao, "")) != ""
        ).distinct().all()
    semanas = {normalize_semana_producao(valor[0]) for valor in valores if normalize_semana_producao(valor[0])}
    if erp_feature_enabled() and inspect(database.engine).has_table("erp_work_orders"):
        with database.engine.connect() as conn:
            datas_erp = conn.execute(text("""
                select data_comercial_prevista from erp_work_orders
                where data_comercial_prevista is not null
            """)).scalars().all()
        semanas.update(
            str((value.date() if isinstance(value, datetime.datetime) else value).isocalendar().week)
            for value in datas_erp
        )
    return sorted(semanas, key=semana_ordenacao)

def is_liberacao_filter(value: str) -> bool:
    return normalize_filter(value) in ["LIBERACAO", "ENTREGAS"]

def normalize_etapa(value: str) -> str:
    if not value:
        return ""
    v = str(value).strip().upper()
    v = v.replace("  ", " ")
    sem_acento = normalize_filter(v)

    if sem_acento in ["AC", "A/C"]:
        return "A/C"
    if sem_acento in ["LIBERA", "LIBERA.", "LIBERACAO"]:
        return "LIBERA."
    if sem_acento in ["ACESSO", "ACESSO.", "ACESSORIO"]:
        return "ACESSÓ."
    if sem_acento in ["SERRA", "SERRA."]:
        return "SERRA."
    if sem_acento in ["EXPE", "EXPE.", "EXPEDICAO"]:
        return "EXPE."
    if sem_acento in ["PLOTA", "PLOTA.", "PLOTAGEM"]:
        return "PLOTA."
    if sem_acento in ["DESMON", "DESMONT"]:
        return "DESMONT"
    if sem_acento in ["ELETRICA", "ELETRIC", "ELETRIC."]:
        return "ELÉTRICA"
    return v

# Define regras de filtragem por etapa
# Ajustado para validar contra pendências reais da produção
def etapa_pendente(status_map, etapa):
    return status_map.get(etapa) in STATUS_PENDENTE

def revestimento_libera_banco(status_map):
    return status_map.get("REVEST") in ["SIM", "PARCIAL"]

ETAPA_REGRAS = {
    "VIDROS": lambda s: etapa_pendente(s, "VIDROS"),
    "A/C": lambda s: s.get("VIDROS") in STATUS_CONCLUIDO and etapa_pendente(s, "A/C"),
    "PREP": lambda s: etapa_pendente(s, "PREP"),
    "SERRA.": lambda s: etapa_pendente(s, "SERRA."),
    "EXPE.": lambda s: etapa_pendente(s, "EXPE."),
    "DESMONT": lambda s: s.get("VIDROS") in ["SIM", "N/A"] and s.get("A/C") in ["SIM", "N/A"] and etapa_pendente(s, "DESMONT"),
    "REVEST": lambda s: s.get("DESMONT") in STATUS_CONCLUIDO and etapa_pendente(s, "REVEST"),
    "ELÉTRICA": lambda s: s.get("REVEST") in STATUS_CONCLUIDO and etapa_pendente(s, "ELÉTRICA"),
    "BCO": lambda s: revestimento_libera_banco(s) and etapa_pendente(s, "BCO"),
    "ACESSÓ.": lambda s: s.get("BCO") == "SIM" and etapa_pendente(s, "ACESSÓ."),
    "PLOTA.": lambda s: etapa_pendente(s, "PLOTA."),
    "LIBERA.": lambda s: s.get("BCO") == "SIM" and etapa_pendente(s, "LIBERA."),
}

KANBAN_COLUNAS = [
    {"id": "prep", "titulo": "PREPARAÇÃO", "etapa": "PREP", "grupo": "Independente"},
    {"id": "expe", "titulo": "EXPEDIÇÃO", "etapa": "EXPE.", "grupo": "Independente"},
    {"id": "serra", "titulo": "SERRALHERIA", "etapa": "SERRA.", "grupo": "Independente"},
    {"id": "vidros", "titulo": "VIDROS", "etapa": "VIDROS", "grupo": "Produtiva"},
    {"id": "ac", "titulo": "AR COND.", "etapa": "A/C", "grupo": "Produtiva"},
    {"id": "desmont", "titulo": "DESMONTAGEM", "etapa": "DESMONT", "grupo": "Produtiva"},
    {"id": "revest", "titulo": "REVESTIMENTO", "etapa": "REVEST", "grupo": "Produtiva"},
    {"id": "eletrica", "titulo": "ELÉTRICA", "etapa": "ELÉTRICA", "grupo": "Produtiva"},
    {"id": "bco", "titulo": "BANCO", "etapa": "BCO", "grupo": "Produtiva"},
    {"id": "acesso", "titulo": "ACESSÓRIO", "etapa": "ACESSÓ.", "grupo": "Produtiva"},
    {"id": "entregas", "titulo": "ENTREGAS", "etapa": None, "grupo": "Por data"},
]

def veiculo_tem_ar_condicionado(veiculo) -> bool:
    return normalize_filter(veiculo.ar_condicionado) in ["GE", "CLIM"]

def veiculo_tem_banco(veiculo) -> bool:
    banco_flag = normalize_filter(veiculo.banco_presente)
    return banco_flag not in ["N", "NAO", "NAO TEM", "SEM", "0"]

def status_etapa(status_map, etapa):
    return status_map.get(normalize_etapa(etapa), "N/A")

def etapa_concluida_ou_na(veiculo, status_map, etapa):
    etapa_norm = normalize_etapa(etapa)
    if etapa_norm == "A/C" and not veiculo_tem_ar_condicionado(veiculo):
        return True
    if etapa_norm == "BCO" and not veiculo_tem_banco(veiculo):
        return True
    return status_etapa(status_map, etapa_norm) in STATUS_CONCLUIDO

def etapa_pendente_kanban(status_map, etapa):
    return status_etapa(status_map, etapa) in STATUS_PENDENTE

def veiculo_atende_filtro_etapa(veiculo, status_map, etapa):
    filtro = normalize_etapa(etapa)
    if filtro in ["GE", "CLIM"]:
        return (
            normalize_filter(veiculo.ar_condicionado) == filtro
            and ETAPA_REGRAS["A/C"](status_map)
        )
    if filtro == "BCO":
        banco_flag = normalize_filter(getattr(veiculo, "banco_presente", ""))
        if banco_flag in ["N", "NAO", "NAO TEM", "SEM", "0"]:
            return False
    return filtro in ETAPA_REGRAS and ETAPA_REGRAS[filtro](status_map)

def deve_exibir_no_kanban(veiculo, status_map, coluna):
    etapa = coluna["etapa"]
    coluna_id = coluna["id"]

    if coluna_id == "entregas":
        return True
    if coluna_id in ["prep", "expe", "serra"]:
        return etapa_pendente_kanban(status_map, etapa)
    if coluna_id == "vidros":
        return etapa_pendente_kanban(status_map, etapa)
    if coluna_id == "ac":
        return (
            veiculo_tem_ar_condicionado(veiculo)
            and etapa_concluida_ou_na(veiculo, status_map, "VIDROS")
            and etapa_pendente_kanban(status_map, etapa)
        )
    if coluna_id == "desmont":
        return etapa_concluida_ou_na(veiculo, status_map, "VIDROS") and etapa_concluida_ou_na(veiculo, status_map, "A/C") and etapa_pendente_kanban(status_map, etapa)
    if coluna_id == "revest":
        return etapa_concluida_ou_na(veiculo, status_map, "DESMONT") and etapa_pendente_kanban(status_map, etapa)
    if coluna_id == "eletrica":
        return etapa_concluida_ou_na(veiculo, status_map, "DESMONT") and etapa_pendente_kanban(status_map, etapa)
    if coluna_id == "bco":
        return (
            veiculo_tem_banco(veiculo)
            and revestimento_libera_banco(status_map)
            and etapa_pendente_kanban(status_map, etapa)
        )
    if coluna_id == "acesso":
        return etapa_pendente_kanban(status_map, etapa)
    return False

def montar_card_kanban(veiculo, status_map, etapa):
    return {
        "dashboard_key": getattr(veiculo, "dashboard_key", str(veiculo.chassi).strip()),
        "source": getattr(veiculo, "source", "LEGADO"),
        "work_order_id": getattr(veiculo, "work_order_id", None),
        "numero_os": getattr(veiculo, "numero_os", None),
        "item_number": getattr(veiculo, "item_number", None),
        "sequencia": getattr(veiculo, "sequencia", None),
        "tipo_servico": getattr(veiculo, "tipo_servico", ""),
        "tipo_servico_grupo": getattr(veiculo, "tipo_servico_grupo", ""),
        "situacao": getattr(veiculo, "situacao", ""),
        "detail_url": getattr(veiculo, "detail_url", f"/veiculo/{veiculo.chassi}"),
        "chassi": veiculo.chassi,
        "chassi_exibicao": getattr(veiculo, "chassi_exibicao", chassi_exibicao(veiculo.chassi)),
        "modelo": veiculo.modelo or "-",
        "modelo_resumido": resumir_mmv(veiculo.modelo),
        "linha": veiculo.linha or "NÃO INFORMADA",
        "semana_producao": veiculo.semana_producao or "-",
        "cliente": veiculo.cliente or "-",
        "destino": veiculo.destino or "-",
        "data_entrega": veiculo.data_entrega_fmt or "-",
        "localizacao": veiculo.localizacao or "-",
        "ar_condicionado": veiculo.ar_condicionado or "-",
        "cj_bco": veiculo.cj_bco or "-",
        "progresso": veiculo.progresso,
        "status": "ENTREGA" if etapa is None else status_etapa(status_map, etapa),
    }

def data_entrega_ordenacao(veiculo):
    sequencia = getattr(veiculo, "sequencia", None)
    if sequencia is not None:
        return (0, int(sequencia), veiculo.ordem or 0)
    data = veiculo.data_entrega
    if not data:
        return (1, datetime.datetime.max, veiculo.ordem or 0)
    if data.tzinfo is not None:
        data = data.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return (0, data, veiculo.ordem or 0)

def montar_kanban(veiculos, status_maps, incluir_plotagem=True):
    colunas = []
    for coluna in KANBAN_COLUNAS:
        cards = []
        # A mesma sequência vigente deve guiar todas as etapas do quadro; não
        # apenas a coluna de entrega. Isso substitui a antiga posição do upload.
        veiculos_coluna = sorted(veiculos, key=data_entrega_ordenacao)
        for veiculo in veiculos_coluna:
            dashboard_key = getattr(veiculo, "dashboard_key", str(veiculo.chassi).strip())
            status_map = status_maps.get(dashboard_key, {})
            if deve_exibir_no_kanban(veiculo, status_map, coluna):
                cards.append(montar_card_kanban(veiculo, status_map, coluna["etapa"]))
        colunas.append({
            **coluna,
            "cards": cards,
            "total": len(cards),
        })
    return colunas

def contar_chassis_ativos_kanban(colunas):
    return len({
        card.get("dashboard_key") or normalize_chassi(card.get("chassi"))
        for coluna in colunas
        if coluna.get("id") != "entregas"
        for card in coluna.get("cards", [])
        if card.get("dashboard_key") or normalize_chassi(card.get("chassi"))
    })

def aplicar_filtros_veiculos(
    query,
    modelo: str = None,
    linha: str = None,
    semana=None,
    entrega_inicio: str = None,
    entrega_fim: str = None,
):
    if modelo and modelo.strip():
        termo = f"%{modelo.strip().upper()}%"
        query = query.filter(
            or_(
                func.upper(func.coalesce(cast(models.Veiculo.modelo, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.chassi, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.ar_condicionado, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.cj_bco, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.cliente, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.destino, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.data_entrega, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.localizacao, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.linha, String), "")).like(termo),
                func.upper(func.coalesce(cast(models.Veiculo.semana_producao, String), "")).like(termo)
            )
        )

    linha_normalizada = normalize_linha(linha)
    if linha_normalizada:
        query = query.filter(func.upper(func.trim(models.Veiculo.linha)) == linha_normalizada)

    semanas_normalizadas = normalize_semanas_producao(semana)
    if semanas_normalizadas:
        query = query.filter(
            func.upper(func.trim(models.Veiculo.semana_producao)).in_([s.upper() for s in semanas_normalizadas])
        )

    inicio, fim = intervalo_entrega_normalizado(entrega_inicio, entrega_fim)
    if inicio:
        inicio_dt = datetime.datetime.combine(inicio, datetime.time.min, tzinfo=LOCAL_TZ)
        query = query.filter(models.Veiculo.data_entrega >= inicio_dt)
    if fim:
        fim_exclusivo = datetime.datetime.combine(
            fim + datetime.timedelta(days=1),
            datetime.time.min,
            tzinfo=LOCAL_TZ,
        )
        query = query.filter(models.Veiculo.data_entrega < fim_exclusivo)

    return query

def _erp_stage_dashboard_status(value):
    status = normalize_filter(value)
    if status == "CONCLUIDA":
        return "SIM"
    if status == "NAO_APLICAVEL":
        return "N/A"
    if status == "EM_ANDAMENTO":
        return "PARCIAL"
    return "NÃO"

def carregar_erp_dashboard(
    modelo: str = None,
    linha: str = None,
    semana=None,
    entrega_inicio: str = None,
    entrega_fim: str = None,
):
    """Adapta O.S. ERP ao Kanban legado sem copiar ou duplicar dados."""
    if not erp_feature_enabled() or not inspect(database.engine).has_table("erp_work_orders"):
        return [], {}

    sequence_schema = inspect(database.engine).has_table("erp_work_order_sequences")
    sequence_fields = "seq.sequencia,seq.semana_planejada" if sequence_schema else "null::integer as sequencia,null::text as semana_planejada"
    sequence_join = "left join erp_work_order_sequences seq on seq.work_order_id=w.id and seq.ativo=true" if sequence_schema else ""
    with database.engine.connect() as conn:
        rows = conn.execute(text(f"""
            select w.id as work_order_id,w.numero_os,w.status,w.tipo_servico,w.linha,
                   w.cliente_nome,w.municipio,w.uf,w.transformacao,
                   w.ar_condicionado,w.conjunto_bancos,w.data_comercial_prevista,
                   e.item_number,v.chassi,v.marca,v.modelo,v.versao,{sequence_fields}
            from erp_work_orders w
            join erp_vehicle_entries e on e.id=w.vehicle_entry_id
            join erp_vehicles v on v.id=e.vehicle_id
            {sequence_join}
        """)).mappings().all()
        stage_rows = conn.execute(text("""
            select s.work_order_id,s.stage_code,s.status,s.aplicavel,s.localizacao,s.ordem
            from erp_work_order_stages s
        """)).mappings().all()

    stages_by_work = {}
    for stage in stage_rows:
        stages_by_work.setdefault(str(stage["work_order_id"]), []).append(stage)

    termo = normalize_filter(modelo)
    linha_filtro = normalize_linha(linha)
    semanas_filtro = set(normalize_semanas_producao(semana))
    inicio, fim = intervalo_entrega_normalizado(entrega_inicio, entrega_fim)
    vehicles, status_maps = [], {}

    for row in rows:
        if normalize_filter(row["status"]) not in {"ATIVA", "EM_PRODUCAO"}:
            continue
        planned = row["data_comercial_prevista"]
        planned_date = planned.date() if isinstance(planned, datetime.datetime) else planned
        week = str(row.get("semana_planejada") or (planned_date.isocalendar().week if planned_date else ""))
        searchable = normalize_filter(" ".join(str(row.get(name) or "") for name in (
            "item_number", "numero_os", "chassi", "marca", "modelo", "versao",
            "cliente_nome", "municipio", "uf", "linha", "transformacao",
        )))
        if termo and termo not in searchable:
            continue
        if linha_filtro and normalize_linha(row["linha"]) != linha_filtro:
            continue
        if semanas_filtro and normalize_semana_producao(week) not in semanas_filtro:
            continue
        if inicio and (not planned_date or planned_date < inicio):
            continue
        if fim and (not planned_date or planned_date > fim):
            continue

        work_id = str(row["work_order_id"])
        dashboard_key = f"ERP:{work_id}"
        work_stages = sorted(stages_by_work.get(work_id, []), key=lambda item: item["ordem"] or 0)
        status_map = {
            normalize_etapa(stage["stage_code"]): _erp_stage_dashboard_status(stage["status"])
            for stage in work_stages
        }
        applicable = [stage for stage in work_stages if stage["aplicavel"]]
        concluded = [stage for stage in applicable if normalize_filter(stage["status"]) == "CONCLUIDA"]
        current_stage = next(
            (stage["stage_code"] for stage in work_stages
             if stage["aplicavel"] and normalize_filter(stage["status"]) not in {"CONCLUIDA", "NAO_APLICAVEL"}),
            "FINALIZADO",
        )
        location = next(
            (str(stage["localizacao"]).strip() for stage in reversed(work_stages)
             if str(stage["localizacao"] or "").strip()),
            "",
        )
        delivery_dt = (
            datetime.datetime.combine(planned_date, datetime.time.min, tzinfo=LOCAL_TZ)
            if planned_date else None
        )
        vehicle = SimpleNamespace(
            dashboard_key=dashboard_key,
            source="ERP",
            work_order_id=work_id,
            numero_os=row["numero_os"],
            item_number=row["item_number"],
            tipo_servico=str(row.get("tipo_servico") or ""),
            tipo_servico_grupo=erp_service.service_type_group(row.get("tipo_servico")),
            situacao=erp_service.work_order_situation(row.get("status"), row.get("tipo_servico")),
            detail_url=f"/veiculo/{str(row['chassi'] or '').strip()}?work_order_id={work_id}",
            chassi=str(row["chassi"] or ""),
            chassi_exibicao=chassi_exibicao(row["chassi"]),
            modelo=" ".join(str(row.get(name) or "").strip() for name in ("marca", "modelo", "versao")).strip(),
            linha=str(row["linha"] or ""),
            semana_producao=week,
            sequencia=row.get("sequencia"),
            ordem=row.get("sequencia") or row["item_number"],
            ar_condicionado=str(row["ar_condicionado"] or ""),
            cj_bco=str(row["conjunto_bancos"] or ""),
            cliente=str(row["cliente_nome"] or ""),
            destino=" / ".join(value for value in (str(row["municipio"] or "").strip(), str(row["uf"] or "").strip()) if value),
            data_entrega=delivery_dt,
            data_entrega_fmt=format_data_entrega(delivery_dt),
            localizacao=location,
            banco_presente="SIM" if str(row["conjunto_bancos"] or "").strip() else "NÃO",
            progresso=int(len(concluded) * 100 / len(applicable)) if applicable else 0,
            etapa_atual=current_stage,
        )
        vehicles.append(vehicle)
        status_maps[dashboard_key] = status_map

    return sorted(vehicles, key=data_entrega_ordenacao), status_maps

def carregar_veiculos_dashboard(
    db: Session,
    modelo: str = None,
    linha: str = None,
    semana=None,
    entrega_inicio: str = None,
    entrega_fim: str = None,
):
    veiculos_db = []
    if legacy_operational_schema_available():
        query = aplicar_filtros_veiculos(
            db.query(models.Veiculo),
            modelo,
            linha,
            semana,
            entrega_inicio,
            entrega_fim,
        )
        veiculos_db = query.order_by(
            models.Veiculo.ordem.asc(),
            models.Veiculo.chassi.asc(),
        ).all()

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

    status_maps = {}
    for v in veiculos_db:
        v.data_entrega_fmt = format_data_entrega(v.data_entrega)
        v.chassi_exibicao = chassi_exibicao(v.chassi)
        chassi_key = str(v.chassi).strip()
        aponts = apont_por_chassi.get(chassi_key, [])

        status_map = {
            normalize_etapa(a.etapa): normalize_status(a.status)
            for a in aponts
        }
        status_maps[chassi_key] = status_map

        concluidos = sum(
            1 for e in ETAPAS_PRODUCAO
            if status_map.get(normalize_etapa(e)) in STATUS_CONCLUIDO
        )
        v.progresso = int((concluidos / len(ETAPAS_PRODUCAO)) * 100) if ETAPAS_PRODUCAO else 0

        v.etapa_atual = "FINALIZADO"
        for e in ETAPAS_STATUS_ATUAL:
            if status_map.get(normalize_etapa(e)) not in STATUS_CONCLUIDO:
                v.etapa_atual = e
                break

    erp_vehicles, erp_status_maps = carregar_erp_dashboard(
        modelo, linha, semana, entrega_inicio, entrega_fim
    )
    erp_chassis = {normalize_chassi(item.chassi) for item in erp_vehicles}
    veiculos_db = [
        item for item in veiculos_db
        if normalize_chassi(item.chassi) not in erp_chassis
    ]
    veiculos_db.extend(erp_vehicles)
    status_maps.update(erp_status_maps)
    return veiculos_db, status_maps

bootstrap_admin_if_configured()

@app.get("/")
async def home(
    request: Request,
    db: Session = Depends(database.get_db),
    modelo: str = None,
    etapa: str = None,
    visao: str = "geral",
    linha: str = None,
    semana: list[str] | None = Query(None),
    entrega_inicio: str = None,
    entrega_fim: str = None,
):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_DASHBOARD_READ):
        return permission_denied()
    data_inicio_atual, data_fim_atual = intervalo_entrega_normalizado(entrega_inicio, entrega_fim)
    visao_param = safe_str(visao).lower()
    visao_atual = visao_param if visao_param in ["resumida", "completa", "gerencial", "geral"] else "resumida"
    modo_geral = visao_atual == "geral"
    modo_gerencial = visao_atual in ["gerencial", "geral"]
    modo_liberacao = is_liberacao_filter(etapa) and not modo_gerencial
    if modo_liberacao:
        visao_atual = "resumida"
    modo_resumido = modo_liberacao or visao_atual == "resumida"

    veiculos_db = []
    if legacy_operational_schema_available():
        query = aplicar_filtros_veiculos(
            db.query(models.Veiculo),
            modelo,
            linha,
            semana,
            entrega_inicio,
            entrega_fim,
        )
        veiculos_db = query.order_by(
            models.Veiculo.ordem.asc(),
            models.Veiculo.chassi.asc(),
        ).all()
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

    status_maps = {}
    for v in veiculos_db:
        v.data_entrega_fmt = format_data_entrega(v.data_entrega)
        v.chassi_exibicao = chassi_exibicao(v.chassi)
        chassi_key = str(v.chassi).strip()
        aponts = apont_por_chassi.get(chassi_key, [])

        # Cria mapeamento de status atualizado para o veículo
        status_map = {
            normalize_etapa(a.etapa): normalize_status(a.status)
            for a in aponts
        }
        status_maps[chassi_key] = status_map

        # Cálculo de progresso
        concluidos = sum(
            1 for e in ETAPAS_PRODUCAO
            if status_map.get(normalize_etapa(e)) in STATUS_CONCLUIDO
        )
        v.progresso = int((concluidos / len(ETAPAS_PRODUCAO)) * 100) if ETAPAS_PRODUCAO else 0

        # Determinação da etapa atual
        v.etapa_atual = "FINALIZADO"
        for e in ETAPAS_STATUS_ATUAL:
            if status_map.get(normalize_etapa(e)) not in STATUS_CONCLUIDO:
                v.etapa_atual = e
                break

        # FILTRAGEM POR ETAPA (Lógica de Negócio)
        if etapa and etapa.strip() and not modo_liberacao and not modo_gerencial:
            if veiculo_atende_filtro_etapa(v, status_map, etapa):
                veiculos_exibicao.append(v)
        else:
            veiculos_exibicao.append(v)

    # Todas as visões compartilham a mesma fonte operacional. Antes deste
    # bloco apenas Geral/Gerencial recebiam as O.S. abertas em Suprimentos.
    erp_vehicles, erp_status_maps = carregar_erp_dashboard(
        modelo, linha, semana, entrega_inicio, entrega_fim
    )
    if etapa and etapa.strip() and not modo_liberacao and not modo_gerencial:
        erp_vehicles = [
            item for item in erp_vehicles
            if veiculo_atende_filtro_etapa(
                item,
                erp_status_maps.get(item.dashboard_key, {}),
                etapa,
            )
        ]
        erp_status_maps = {
            item.dashboard_key: erp_status_maps.get(item.dashboard_key, {})
            for item in erp_vehicles
        }
    erp_chassis = {normalize_chassi(item.chassi) for item in erp_vehicles}
    veiculos_exibicao = [
        item for item in veiculos_exibicao
        if normalize_chassi(item.chassi) not in erp_chassis
    ]
    veiculos_exibicao.extend(erp_vehicles)
    status_maps.update(erp_status_maps)

    kanban_colunas = montar_kanban(veiculos_exibicao, status_maps, incluir_plotagem=not modo_geral) if modo_gerencial else []

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
            "modo_gerencial": modo_gerencial,
            "modo_geral": modo_geral,
            "visao_atual": visao_atual,
            "linha_atual": normalize_linha(linha),
            "semanas_atuais": normalize_semanas_producao(semana),
            "semanas_disponiveis": listar_semanas_producao(db),
            "entrega_inicio_atual": data_inicio_atual.isoformat() if data_inicio_atual else "",
            "entrega_fim_atual": data_fim_atual.isoformat() if data_fim_atual else "",
            "filtros_gerais_ativos": bool(safe_str(modelo) or normalize_linha(linha) or normalize_semanas_producao(semana) or data_inicio_atual or data_fim_atual),
            "total_veiculos": contar_chassis_ativos_kanban(kanban_colunas) if modo_gerencial else len({normalize_chassi(v.chassi) for v in veiculos_exibicao}),
            "kanban_colunas": kanban_colunas,
            "current_user": current_user
        }
    )

@app.get("/kanban_dados")
async def kanban_dados(
    request: Request,
    db: Session = Depends(database.get_db),
    modelo: str = None,
    visao: str = None,
    linha: str = None,
    semana: list[str] | None = Query(None),
    entrega_inicio: str = None,
    entrega_fim: str = None,
):
    current_user = require_login(request, db)
    if not current_user:
        return JSONResponse({"status": "erro", "detail": "Login necessário"}, status_code=401)
    if not has_permission(current_user, authz.MES_DASHBOARD_READ):
        return permission_denied(api=True)

    veiculos, status_maps = carregar_veiculos_dashboard(
        db,
        modelo,
        linha,
        semana,
        entrega_inicio,
        entrega_fim,
    )
    modo_geral = safe_str(visao).lower() == "geral"
    colunas = montar_kanban(veiculos, status_maps, incluir_plotagem=not modo_geral)
    return {
        "status": "ok",
        "atualizado_em": datetime.datetime.now(LOCAL_TZ).strftime("%H:%M:%S"),
        "total_veiculos": contar_chassis_ativos_kanban(colunas),
        "colunas": colunas,
    }

@app.get("/veiculo/{chassi}")
async def detalhes(
    request: Request,
    chassi: str,
    work_order_id: str = None,
    db: Session = Depends(database.get_db),
):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_DASHBOARD_READ):
        return permission_denied()
    c_limpo = chassi.strip()
    user_name = get_user_name(request, db)

    if work_order_id and erp_feature_enabled():
        try:
            with database.engine.begin() as conn:
                detail = erp_service.work_order_detail(conn, work_order_id)
        except ValueError as exc:
            return HTMLResponse(str(exc), status_code=404)
        work = detail["work_order"]
        if normalize_chassi(work.get("chassi")) != normalize_chassi(c_limpo):
            return HTMLResponse(
                "O.S. não corresponde ao chassi informado.",
                status_code=404,
            )
        display_stage_status = {
            "S": "SIM",
            "P": "PARCIAL",
            "N": "NÃO",
            "N/A": "N/A",
            "?": "",
        }
        apont_map = {}
        for stage in detail["stages"]:
            stage["inicio_str"] = to_input_dt(stage.get("inicio"))
            stage["termino_str"] = to_input_dt(stage.get("termino"))
            stage["status"] = display_stage_status.get(
                stage.get("input_code"),
                "",
            )
            apont_map[stage["stage_code"]] = SimpleNamespace(**stage)
        current_stage = next(
            (
                stage for stage in detail["stages"]
                if stage.get("aplicavel")
                and stage.get("input_code") not in {"S", "N/A"}
            ),
            detail["stages"][-1] if detail["stages"] else None,
        )
        planned = work.get("data_comercial_prevista")
        planned_display = (
            datetime.datetime.combine(planned, datetime.time.min, tzinfo=LOCAL_TZ)
            if isinstance(planned, datetime.date)
            and not isinstance(planned, datetime.datetime)
            else planned
        )
        vehicle_name = " ".join(
            str(work.get(name) or "").strip()
            for name in ("marca", "modelo", "versao")
        ).strip()
        veiculo = SimpleNamespace(
            modelo=vehicle_name or "-",
            chassi=work.get("chassi"),
            linha=work.get("linha"),
            cliente=work.get("cliente_nome"),
            destino=" / ".join(
                value for value in (
                    str(work.get("municipio") or "").strip(),
                    str(work.get("uf") or "").strip(),
                ) if value
            ),
            cj_bco=work.get("conjunto_bancos"),
            banco_presente=(
                "SIM" if str(work.get("conjunto_bancos") or "").strip()
                else "NÃO"
            ),
            banco_comentario="",
            ar_condicionado=work.get("ar_condicionado"),
            semana_producao=str(planned.isocalendar().week) if planned else "",
            data_entrega_fmt=format_data_entrega(planned_display),
            localizacao=(current_stage or {}).get("localizacao") or "",
            item_number=work.get("item_number"),
            numero_os=work.get("numero_os"),
            status=work.get("status"),
        )
        return templates.TemplateResponse(
            request,
            "detalhes.html",
            {
                "request": request,
                "veiculo": veiculo,
                "etapas": [stage["stage_code"] for stage in detail["stages"]],
                "apont_map": apont_map,
                "localizacoes": LOCALIZACOES,
                "user_name": user_name,
                "erp_mode": True,
                "work_order_id": work_order_id,
                "current_stage": (current_stage or {}).get("stage_code") or "",
                "current_user": current_user,
            },
        )

    legacy_available = legacy_operational_schema_available()
    veiculo = None
    if legacy_available:
        veiculo = db.query(models.Veiculo).filter(
            func.trim(cast(models.Veiculo.chassi, String)) == c_limpo
        ).first()

    if (
        not veiculo
        and erp_feature_enabled()
        and inspect(database.engine).has_table("erp_work_orders")
    ):
        with database.engine.connect() as conn:
            current_work = conn.execute(text("""
                select w.id
                from erp_work_orders w
                join erp_vehicle_entries e on e.id=w.vehicle_entry_id
                join erp_vehicles v on v.id=e.vehicle_id
                where trim(v.chassi)=:chassi
                  and w.status in ('ATIVA','EM_PRODUÇÃO')
                order by e.item_number desc
                limit 1
            """), {"chassi": c_limpo}).scalar()
        if current_work:
            return RedirectResponse(
                url=f"/veiculo/{c_limpo}?work_order_id={current_work}",
                status_code=303,
            )

    feitos = []
    if legacy_available:
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
            "user_name": user_name,
            "erp_mode": False,
            "work_order_id": "",
            "current_stage": "",
            "current_user": current_user,
        }
    )

@app.post("/upload")
async def upload_base(request: Request, file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return {"status": "erro", "detail": "Login necessário"}
    if not has_permission(current_user, authz.MES_LEGACY_IMPORT):
        return permission_denied(api=True)
    if not legacy_upload_enabled() or not legacy_operational_schema_available():
        return legacy_disabled_response()
    try:
        content = await file.read()

        df = (
            pd.read_excel(io.BytesIO(content), keep_default_na=False)
            if file.filename.endswith(".xlsx")
            else pd.read_csv(io.BytesIO(content), keep_default_na=False)
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

        def get_col_raw(row, *names):
            for n in names:
                if n in df.columns:
                    val = row.get(n, "")
                    if pd.isna(val):
                        return ""
                    return val.strip() if isinstance(val, str) else val
            return ""

        etapas_col = {normalize_etapa(c): c for c in df.columns}
        colunas_obrigatorias = ["CHASSI", "MMMV", "LINHA"]
        faltantes = [coluna for coluna in colunas_obrigatorias if coluna not in df.columns]
        if not any(coluna in df.columns for coluna in ["DATA DE ENTREGA", "DATA_ENTREGA", "DT ENTREGA", "ENTREGA"]):
            faltantes.append("DATA DE ENTREGA")
        etapas_faltantes = [etapa for etapa in ETAPAS_PRODUCAO if normalize_etapa(etapa) not in etapas_col]
        if faltantes or etapas_faltantes:
            detalhes = faltantes + etapas_faltantes
            raise ValueError(f"Colunas obrigatórias ausentes: {', '.join(detalhes)}")

        registros = []
        chassis_vistos = set()

        for numero_linha, (_, row) in enumerate(df.iterrows(), start=2):
            ch_raw = normalize_chassi(safe_str(row.get("CHASSI", "")).split(".")[0])
            if not ch_raw or ch_raw.lower() == "nan":
                continue
            if ch_raw in chassis_vistos:
                raise ValueError(f"Chassi duplicado na linha {numero_linha}: {ch_raw}")
            chassis_vistos.add(ch_raw)

            modelo = safe_str(row.get("MMMV", "")).upper()
            if not modelo:
                raise ValueError(f"MMMV não informado na linha {numero_linha} ({ch_raw})")

            linha = normalize_linha(get_col(row, "LINHA", "TIPO DE LINHA", "LINHA DE PRODUÇÃO", "LINHA DE PRODUCAO"))
            if not linha:
                raise ValueError(f"LINHA inválida na linha {numero_linha} ({ch_raw}). Use BÁSICA ou EXECUTIVA.")

            semana_producao = normalize_semana_producao(get_col(
                row,
                "SEMANA DE PRODUÇÃO",
                "SEMANA DE PRODUCAO",
                "SEMANA PRODUÇÃO",
                "SEMANA PRODUCAO",
                "SEMANA",
                "SEM. PRODUÇÃO",
                "SEM. PRODUCAO",
            ))
            ar_cond = get_col(row, "AR CONDICIONADO", "AR_CONDICIONADO", "AR-CONDICIONADO", "ARCONDICIONADO")
            cj_bco = get_col(row, "CJ. BCO", "CJ BCO", "CJ_BCO", "CJ-BCO")
            cliente = get_col(row, "CLIENTE")
            destino = get_col(row, "DESTINO")
            data_entrega = parse_data_entrega(get_col_raw(row, "DATA DE ENTREGA", "DATA_ENTREGA", "DT ENTREGA", "ENTREGA"))
            if not data_entrega:
                raise ValueError(f"DATA DE ENTREGA inválida na linha {numero_linha} ({ch_raw})")
            localizacao = get_col(row, "LOCALIZACAO", "LOCALIZAÇÃO")
            banco_presente = get_col(row, "BANCO", "BANCO_PRESENTE", "POSSUI BANCO", "TEM BANCO")
            banco_comentario = get_col(row, "COMENTARIO BANCO", "COMENTARIO_BANCO", "BANCO OBS", "OBS BANCO")

            status_etapas = {}
            for etapa in ETAPAS_PRODUCAO:
                col_name = etapas_col.get(normalize_etapa(etapa))
                status_original = safe_str(row[col_name])
                if not status_original:
                    raise ValueError(f"Status de {etapa} não informado na linha {numero_linha} ({ch_raw})")
                if not status_upload_valido(status_original):
                    raise ValueError(f"Status inválido em {etapa}, linha {numero_linha} ({ch_raw}): {status_original}")
                status_etapas[etapa] = normalize_status(status_original)

            registros.append({
                "veiculo": {
                    "chassi": ch_raw,
                    "modelo": modelo,
                    "linha": linha,
                    "semana_producao": semana_producao,
                    "ordem": len(registros) + 1,
                    "ar_condicionado": ar_cond,
                    "cj_bco": cj_bco,
                    "cliente": cliente,
                    "destino": destino,
                    "data_entrega": data_entrega,
                    "localizacao": localizacao,
                    "banco_presente": banco_presente,
                    "banco_comentario": banco_comentario,
                },
                "status_etapas": status_etapas,
            })

        if not registros:
            raise ValueError("Nenhum veículo válido foi encontrado na planilha.")

        # Valida toda a planilha antes de substituir a carga atual e mantém a troca atômica.
        db.query(models.Apontamento).delete(synchronize_session=False)
        db.query(models.Veiculo).delete(synchronize_session=False)

        for registro in registros:
            veiculo = models.Veiculo(**registro["veiculo"])
            db.add(veiculo)
            for etapa, status in registro["status_etapas"].items():
                db.add(models.Apontamento(
                    chassi=veiculo.chassi,
                    etapa=etapa,
                    status=status
                ))

        db.commit()
        return {"status": "sucesso", "total_veiculos": len(registros)}

    except ValueError as e:
        db.rollback()
        return JSONResponse({"status": "erro", "detail": str(e)}, status_code=400)
    except Exception as e:
        db.rollback()
        return JSONResponse({"status": "erro", "detail": str(e)}, status_code=500)

@app.post("/upload_apontamentos")
async def upload_apontamentos(request: Request, file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return {"status": "erro", "detail": "Login necessário"}
    if not has_permission(current_user, authz.MES_LEGACY_IMPORT):
        return permission_denied(api=True)
    if not legacy_upload_enabled() or not legacy_operational_schema_available():
        return legacy_disabled_response()
    try:
        content = await file.read()

        df = (
            pd.read_excel(io.BytesIO(content), keep_default_na=False)
            if file.filename.endswith(".xlsx")
            else pd.read_csv(io.BytesIO(content), keep_default_na=False)
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
    current_user = require_login(request, db)
    if not current_user:
        return JSONResponse({"status": "erro", "detail": "Login necessário"}, status_code=401)
    if not has_permission(current_user, authz.MES_STAGE_WRITE):
        return permission_denied(api=True)
    if not legacy_operational_schema_available():
        return legacy_disabled_response()
    ch = str(data["chassi"]).strip()
    et = normalize_etapa(data["etapa"])
    responsavel = str(data.get("responsavel", "")).strip()
    inicio = parse_local_dt(data.get("inicio"))
    termino = parse_local_dt(data.get("termino"))

    registrar_historico = bool(data.get("registrar_historico", True))
    metadata_only = bool(data.get("metadata_only")) or data.get("registrar_historico") is False

    existing = db.query(models.Apontamento).filter(
        func.trim(cast(models.Apontamento.chassi, String)) == ch,
        func.trim(cast(models.Apontamento.etapa, String)) == et
    ).with_for_update().first()

    # A mobile field autosave must not reuse a stale visual status.  Preserve
    # legacy data as well, even though the shared ERP is the active path.
    if metadata_only:
        if existing:
            existing.responsavel = responsavel
            existing.inicio = inicio or existing.inicio
            existing.termino = termino or existing.termino
            if "localizacao" in data:
                existing.localizacao = str(data.get("localizacao") or "").strip()
            status_atual = existing.status
        else:
            # Preserve the former ability to save fields for a newly rendered
            # legacy stage, without inventing a completed production status.
            status_atual = "N/A"
            db.add(models.Apontamento(
                chassi=ch,
                etapa=et,
                status=status_atual,
                responsavel=responsavel,
                inicio=inicio,
                termino=termino,
                localizacao=str(data.get("localizacao") or "").strip() or None,
            ))
        db.commit()
        return {"status": "ok", "metadata_only": True, "stage_status": status_atual}

    raw_status = data.get("status") or data.get("input_code")
    if raw_status in (None, ""):
        return JSONResponse(
            {"status": "erro", "detail": "Informe explicitamente o status da etapa."},
            status_code=400,
        )
    st = normalize_status(raw_status)

    # Falha fechada para uma tela legada aberta antes da alteração mais recente.
    # Assim como no ERP compartilhado, ela deve ser atualizada antes de alterar
    # um apontamento já existente.
    expected_raw = data.get("expected_status")
    if existing and expected_raw in (None, ""):
        return JSONResponse({
            "status": "erro",
            "detail": "Esta tela esta desatualizada. Atualize a pagina antes de registrar o status da etapa.",
        }, status_code=409)
    if existing and expected_raw not in (None, ""):
        expected = normalize_status(expected_raw)
        if expected != existing.status:
            return JSONResponse({
                "status": "erro",
                "detail": "A etapa foi alterada por outro apontamento. Atualize a tela antes de salvar.",
            }, status_code=409)

    reopening = bool(existing and existing.status in {"SIM", "N/A"} and st != existing.status)
    reopen_reason = str(data.get("reopen_reason") or "").strip()
    if reopening and not reopen_reason:
        return JSONResponse({
            "status": "erro",
            "detail": "Para reabrir uma etapa concluida ou nao aplicavel, informe o motivo.",
        }, status_code=400)

    # Atualiza a linha existente em vez de apagá-la e recriá-la. Além de manter
    # a rastreabilidade, isso impede um clique de status de apagar dados de
    # responsável, data ou localização gravados pelo autosave.
    if existing:
        existing.status = st
        if "responsavel" in data:
            existing.responsavel = responsavel
        if inicio:
            existing.inicio = inicio
        if termino:
            existing.termino = termino
        if "localizacao" in data:
            existing.localizacao = str(data.get("localizacao") or "").strip() or None
    else:
        existing = models.Apontamento(
            chassi=ch,
            etapa=et,
            status=st,
            responsavel=responsavel,
            inicio=inicio,
            termino=termino,
            localizacao=str(data.get("localizacao") or "").strip() or None,
        )
        db.add(existing)

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
            responsavel=existing.responsavel,
            inicio=existing.inicio,
            termino=existing.termino,
            localizacao=existing.localizacao
        ))

    db.commit()
    return {"status": "ok", "stage_status": st}

@app.post("/veiculo_localizacao")
async def atualizar_localizacao(request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return JSONResponse({"status": "erro", "detail": "Login necessário"}, status_code=401)
    if not has_permission(current_user, authz.MES_STAGE_WRITE):
        return permission_denied(api=True)
    if not legacy_operational_schema_available():
        return legacy_disabled_response()
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
async def atualizar_banco(request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return JSONResponse({"status": "erro", "detail": "Login necessário"}, status_code=401)
    if not has_permission(current_user, authz.MES_STAGE_WRITE):
        return permission_denied(api=True)
    if not legacy_operational_schema_available():
        return legacy_disabled_response()
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

def _xlsx_response(rows, columns, filename):
    """Return a stable XLSX even when the selected report has no rows."""
    frame = pd.DataFrame(rows, columns=columns)
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False)
    out.seek(0)
    return StreamingResponse(
        out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _erp_history_export_rows():
    with database.engine.connect() as conn:
        rows = conn.execute(text("""
            select e.item_number,w.numero_os,v.chassi,
                   concat_ws(' ',nullif(trim(v.marca),''),nullif(trim(v.modelo),''),
                             nullif(trim(v.versao),'')) as modelo,
                   s.stage_code,ev.action,ev.status_anterior,ev.novo_status,
                   ev.operador,ev.inicio,ev.termino,ev.localizacao,ev.created_at
              from erp_work_order_stage_events ev
              join erp_work_order_stages s on s.id=ev.work_order_stage_id
              join erp_work_orders w on w.id=s.work_order_id
              join erp_vehicle_entries e on e.id=w.vehicle_entry_id
              join erp_vehicles v on v.id=e.vehicle_id
             order by ev.created_at,e.item_number,s.ordem,ev.id
        """)).mappings()
        return [
            {
                "ITEM": row["item_number"],
                "O.S.": row["numero_os"],
                "CHASSI": row["chassi"],
                "MODELO": row["modelo"],
                "ETAPA": row["stage_code"],
                "AÇÃO": row["action"],
                "STATUS ANTERIOR": row["status_anterior"],
                "STATUS": row["novo_status"],
                "RESPONSAVEL": row["operador"],
                "INICIO": to_excel_dt(row["inicio"]),
                "TERMINO": to_excel_dt(row["termino"]),
                "LOCALIZACAO": row["localizacao"],
                "DATA": to_excel_dt(row["created_at"]),
                "ORIGEM": "ERP",
            }
            for row in rows
        ]


def _erp_time_export_rows():
    with database.engine.connect() as conn:
        rows = conn.execute(text("""
            select e.item_number,w.numero_os,v.chassi,
                   concat_ws(' ',nullif(trim(v.marca),''),nullif(trim(v.modelo),''),
                             nullif(trim(v.versao),'')) as modelo,
                   s.stage_code,s.status,s.responsavel,s.inicio,s.termino,s.localizacao
              from erp_work_order_stages s
              join erp_work_orders w on w.id=s.work_order_id
              join erp_vehicle_entries e on e.id=w.vehicle_entry_id
              join erp_vehicles v on v.id=e.vehicle_id
             order by e.item_number,s.ordem,s.id
        """)).mappings()
        return [
            {
                "ITEM": row["item_number"],
                "O.S.": row["numero_os"],
                "CHASSI": row["chassi"],
                "MODELO": row["modelo"],
                "ETAPA": row["stage_code"],
                "STATUS": row["status"],
                "RESPONSAVEL": row["responsavel"],
                "INICIO": to_excel_dt(row["inicio"]),
                "TERMINO": to_excel_dt(row["termino"]),
                "LOCALIZACAO": row["localizacao"],
                "ORIGEM": "ERP",
            }
            for row in rows
        ]


@app.get("/exportar_historico")
async def exportar(request: Request, db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_EXPORTS_READ):
        return permission_denied()
    if erp_feature_enabled() and inspect(database.engine).has_table("erp_work_order_stage_events"):
        columns = [
            "ITEM", "O.S.", "CHASSI", "MODELO", "ETAPA", "AÇÃO",
            "STATUS ANTERIOR", "STATUS", "RESPONSAVEL", "INICIO",
            "TERMINO", "LOCALIZACAO", "DATA", "ORIGEM",
        ]
        return _xlsx_response(
            _erp_history_export_rows(),
            columns,
            "logs_apontamentos_mes.xlsx",
        )
    if not legacy_operational_schema_available():
        return legacy_disabled_response()
    logs = db.query(models.Historico).all()

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

    return _xlsx_response(
        df.to_dict(orient="records"),
        list(df.columns),
        "relatorio.xlsx",
    )

@app.get("/exportar_tempos")
async def exportar_tempos(request: Request, db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_EXPORTS_READ):
        return permission_denied()
    if erp_feature_enabled() and inspect(database.engine).has_table("erp_work_order_stages"):
        columns = [
            "ITEM", "O.S.", "CHASSI", "MODELO", "ETAPA", "STATUS",
            "RESPONSAVEL", "INICIO", "TERMINO", "LOCALIZACAO", "ORIGEM",
        ]
        return _xlsx_response(
            _erp_time_export_rows(),
            columns,
            "tempos_localizacao.xlsx",
        )
    if not legacy_operational_schema_available():
        return legacy_disabled_response()
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

    return _xlsx_response(
        df.to_dict(orient="records"),
        list(df.columns),
        "tempos_localizacao.xlsx",
    )

@app.post("/limpar_historico")
async def limpar_logs(request: Request, db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_LEGACY_IMPORT):
        return permission_denied()
    if not legacy_upload_enabled() or not legacy_operational_schema_available():
        return legacy_disabled_response()
    db.query(models.Historico).delete()
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/importar")
async def pg_importar(request: Request, db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_LEGACY_IMPORT):
        return permission_denied()
    if not legacy_upload_enabled() or not legacy_operational_schema_available():
        return HTMLResponse(
            "Importação legada desativada neste ambiente.",
            status_code=404,
        )
    return templates.TemplateResponse(request, "importar.html", {"request": request})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: Session = Depends(database.get_db)):
    if portal_sso.enabled():
        return RedirectResponse(
            url=portal_sso.portal_login_url("MES", request.query_params.get("next") or "/?visao=geral"),
            status_code=303,
        )
    if require_login(request, db):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"request": request, "erro": ""})

@app.post("/login")
async def login_post(request: Request, nome: str = Form(...), senha: str = Form(...), db: Session = Depends(database.get_db)):
    if portal_sso.enabled():
        return RedirectResponse(url=portal_sso.portal_login_url("MES", "/?visao=geral"), status_code=303)
    nome_limpo = str(nome).strip()
    if authz.shared_auth_enabled():
        try:
            principal = authz.authenticate_shared_user(database.engine, nome_limpo, str(senha))
        except RuntimeError as exc:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "erro": str(exc)},
                status_code=503,
            )
        if not principal:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "erro": "Usuário ou senha inválidos."},
                status_code=401,
            )
        if not has_permission(principal, authz.MES_DASHBOARD_READ):
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "erro": "Este usuário não possui acesso ao MES."},
                status_code=403,
            )
        token = authz.create_shared_session(database.engine, principal)
        resp = RedirectResponse(url="/?visao=geral", status_code=303)
        set_session_cookie(resp, token)
        return resp

    usuario = db.query(models.Usuario).filter(func.upper(models.Usuario.nome) == nome_limpo.upper()).first()
    if not usuario or not verify_password(str(senha), usuario.senha_hash or ""):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"request": request, "erro": "Usuário ou senha inválidos."},
            status_code=401,
        )
    token = create_session(db, usuario)
    resp = RedirectResponse(url="/?visao=geral", status_code=303)
    set_session_cookie(resp, token)
    return resp


@app.get("/_sso/consume")
async def portal_sso_consume(ticket: str, request: Request):
    """Exchange the Portal's short assertion for the MES isolated session."""
    if not portal_sso.enabled():
        return HTMLResponse("SSO central desativado.", status_code=404)
    if not authz.shared_auth_enabled():
        return HTMLResponse("O MES precisa usar a autenticacao compartilhada para o login central.", status_code=503)
    try:
        claims = portal_sso.consume_ticket(ticket, "MES")
        principal = authz.load_shared_principal(database.engine, claims["uid"])
        if (
            not principal
            or principal.username.casefold() != claims["username"].casefold()
            or principal.auth_version != claims["auth_version"]
        ):
            raise ValueError("Usuario sem sessao valida para este modulo.")
        if not has_permission(principal, authz.MES_DASHBOARD_READ):
            return HTMLResponse("Este usuario nao possui acesso ao MES.", status_code=403)
        token = authz.create_shared_session(database.engine, principal)
        response = RedirectResponse(
            url=portal_sso.normalize_next(claims.get("next"), "/?visao=geral"),
            status_code=303,
        )
        set_session_cookie(response, token)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
    except (ValueError, RuntimeError):
        return HTMLResponse("Nao foi possivel validar o acesso centralizado.", status_code=401)

@app.post("/logout")
async def logout(request: Request, db: Session = Depends(database.get_db)):
    if authz.shared_auth_enabled():
        authz.revoke_shared_session(
            database.engine,
            request.cookies.get(authz.SHARED_SESSION_COOKIE),
        )
        resp = RedirectResponse(
            url=portal_sso.portal_logout_url() if portal_sso.enabled() else "/login",
            status_code=303,
        )
        clear_session_cookie(resp)
        return resp
    token = (request.cookies.get(SESSION_COOKIE) or "").strip()
    if token:
        db.query(models.SessaoUsuario).filter(models.SessaoUsuario.token == token).delete()
        db.commit()
    resp = RedirectResponse(
        url=portal_sso.portal_logout_url() if portal_sso.enabled() else "/login",
        status_code=303,
    )
    clear_session_cookie(resp)
    return resp

@app.get("/usuarios")
async def usuarios_page(request: Request, db: Session = Depends(database.get_db)):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_USERS_MANAGE):
        return permission_denied()
    if authz.shared_auth_enabled():
        return RedirectResponse(
            url=os.environ.get(
                "MES_USER_MANAGEMENT_URL",
                "https://moduloestoque-cni2.onrender.com/usuarios",
            ),
            status_code=303,
        )
    usuarios = db.query(models.Usuario).order_by(models.Usuario.nome.asc()).all()
    return templates.TemplateResponse(
        request,
        "usuarios.html",
        {"request": request, "usuarios": usuarios, "erro": "", "sucesso": "", "current_user": current_user},
    )

@app.post("/usuarios")
async def usuarios_create(
    request: Request,
    nome: str = Form(...),
    senha: str = Form(...),
    is_admin: str = Form("0"),
    db: Session = Depends(database.get_db),
):
    current_user = require_login(request, db)
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(current_user, authz.MES_USERS_MANAGE):
        return permission_denied()
    if authz.shared_auth_enabled():
        return RedirectResponse(
            url=os.environ.get(
                "MES_USER_MANAGEMENT_URL",
                "https://moduloestoque-cni2.onrender.com/usuarios",
            ),
            status_code=303,
        )

    nome_limpo = str(nome).strip()
    senha_limpa = str(senha).strip()
    usuarios = db.query(models.Usuario).order_by(models.Usuario.nome.asc()).all()
    if not nome_limpo or not senha_limpa:
        return templates.TemplateResponse(
            request,
            "usuarios.html",
            {"request": request, "usuarios": usuarios, "erro": "Informe nome e senha.", "sucesso": "", "current_user": current_user},
            status_code=400,
        )
    existente = db.query(models.Usuario).filter(func.upper(models.Usuario.nome) == nome_limpo.upper()).first()
    if existente:
        return templates.TemplateResponse(
            request,
            "usuarios.html",
            {"request": request, "usuarios": usuarios, "erro": "Usuário já existe.", "sucesso": "", "current_user": current_user},
            status_code=400,
        )

    db.add(models.Usuario(nome=nome_limpo, senha_hash=hash_password(senha_limpa), is_admin=1 if is_admin == "1" else 0))
    db.commit()
    usuarios = db.query(models.Usuario).order_by(models.Usuario.nome.asc()).all()
    return templates.TemplateResponse(
        request,
        "usuarios.html",
        {"request": request, "usuarios": usuarios, "erro": "", "sucesso": "Usuário criado.", "current_user": current_user},
    )


# New ERP domain API. Legacy upload endpoints remain available during transition.
@app.get("/api/erp/catalogs")
async def erp_catalogs_api(request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    return {"ok": True, **erp_catalogs.payload()}

@app.post("/api/erp/vehicle-entries")
async def erp_vehicle_entry(request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not (
        has_permission(user, authz.MES_WORK_ORDERS_MANAGE)
        and has_permission(user, authz.MES_VEHICLE_ENTRIES_CREATE)
    ):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn: result = erp_service.create_entry(conn, data, user.nome)
        return {"ok": True, **result}
    except ValueError as exc: return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.get("/gestao-os", response_class=HTMLResponse)
async def erp_work_order_screen(request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return HTMLResponse("Integração ERP desativada pela feature flag.", status_code=404)
    user = require_login(request, db)
    if not user: return RedirectResponse(url="/login", status_code=303)
    if not has_permission(user, authz.MES_DASHBOARD_READ):
        return permission_denied()
    return templates.TemplateResponse(request, "gestao_os.html", {"request": request, "current_user": user})

@app.get("/sequenciamento", response_class=HTMLResponse)
async def erp_sequencing_screen(request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled():
        return HTMLResponse("Integração ERP desativada pela feature flag.", status_code=404)
    user = require_login(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied()
    return templates.TemplateResponse(request, "sequenciamento.html", {"request": request, "current_user": user})

@app.get("/exportar_controle_producao")
async def exportar_controle_producao(request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled():
        return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    if not has_permission(user, authz.MES_EXPORTS_READ):
        return permission_denied()
    with database.engine.connect() as conn:
        output, _, _ = erp_report.build_work_order_report(conn)
    filename = f"Controle_Producao_MES_{datetime.datetime.now(LOCAL_TZ):%Y%m%d_%H%M}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.post("/api/erp/vehicle-entries/{entry_id}/work-orders")
async def erp_work_order(entry_id: str, request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn: result = erp_service.create_work_order(conn, entry_id, data, user.nome)
        return {"ok": True, **result}
    except ValueError as exc: return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/work-orders/{work_id}/activate")
async def erp_activate(work_id: str, request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn: result = erp_service.activate_work_order(conn, work_id, user.nome)
        return {"ok": True, **result}
    except ValueError as exc: return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.put("/api/erp/work-orders/{work_id}/stage-configuration")
async def erp_stage_configuration(
    work_id: str,
    request: Request,
    data: dict = Body(...),
    db: Session = Depends(database.get_db),
):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            if data.get("activate"):
                result = erp_service.configure_and_activate(conn, work_id, data, user.nome)
            else:
                result = erp_service.configure_stages(conn, work_id, data, user.nome)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.get("/api/erp/kanban")
async def erp_kanban(request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_DASHBOARD_READ):
        return permission_denied(api=True)
    with database.engine.connect() as conn: return {"ok": True, "cards": erp_service.active_cards(conn)}

@app.post("/api/erp/work-orders/{work_id}/stages/{stage_code:path}")
async def erp_stage(work_id: str, stage_code: str, request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not (
        has_permission(user, authz.MES_WORK_ORDERS_MANAGE)
        and has_permission(user, authz.MES_STAGE_WRITE)
    ):
        return permission_denied(api=True)
    if (
        not erp_service._is_metadata_only_stage_update(data)
        and data.get("expected_status") in (None, "")
    ):
        return JSONResponse({
            "ok": False,
            "error": "Esta tela esta desatualizada. Atualize a pagina antes de registrar o status da etapa.",
        }, status_code=409)
    try:
        with database.engine.begin() as conn: result = erp_service.update_stage(conn, work_id, stage_code, data, user.nome)
        return {"ok": True, **result}
    except erp_service.StageConflictError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=409)
    except ValueError as exc: return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.patch("/api/erp/work-orders/{work_id}/stage-details/{stage_code:path}")
async def erp_stage_details(
    work_id: str,
    stage_code: str,
    request: Request,
    data: dict = Body(...),
    db: Session = Depends(database.get_db),
):
    """Autosave metadata without exposing the stage status to a stale form."""
    if not erp_feature_enabled():
        return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not (
        has_permission(user, authz.MES_WORK_ORDERS_MANAGE)
        and has_permission(user, authz.MES_STAGE_WRITE)
    ):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            result = erp_service.update_stage_metadata(conn, work_id, stage_code, data, user.nome)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/work-orders/{work_id}/location")
async def erp_location(
    work_id: str,
    request: Request,
    data: dict = Body(...),
    db: Session = Depends(database.get_db),
):
    if not erp_feature_enabled():
        return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return JSONResponse(
            {"ok": False, "error": "Login necessario."},
            status_code=401,
        )
    if not (
        has_permission(user, authz.MES_WORK_ORDERS_MANAGE)
        and has_permission(user, authz.MES_STAGE_WRITE)
    ):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            result = erp_service.update_work_order_location(
                conn,
                work_id,
                data.get("localizacao"),
                user.nome,
                data.get("idempotency_key"),
            )
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/work-orders/{work_id}/finalize")
async def erp_finalize(work_id: str, request: Request, data: dict = Body(default={}), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not (
        has_permission(user, authz.MES_WORK_ORDERS_MANAGE)
        and has_permission(user, authz.MES_FINALIZE)
    ):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            result = erp_service.finalize(
                conn,
                work_id,
                user.nome,
                bool(data.get("delivered")),
                str(data.get("observacoes") or ""),
                data.get("status"),
                data.get("data_evento"),
            )
        return {"ok": True, **result}
    except ValueError as exc: return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/work-orders/{work_id}/schedules")
async def erp_schedule(work_id: str, request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not (
        has_permission(user, authz.MES_WORK_ORDERS_MANAGE)
        and has_permission(user, authz.MES_SCHEDULE_MANAGE)
    ):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn: erp_service.reschedule(conn, work_id, data.get("nova_data"), str(data.get("motivo") or ""), user.nome)
        return {"ok": True}
    except ValueError as exc: return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.get("/api/erp/sequencing")
async def erp_sequencing_overview(request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled():
        return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    with database.engine.connect() as conn:
        return {"ok": True, **erp_service.sequence_overview(conn)}

@app.put("/api/erp/sequencing/profile")
async def erp_update_sequence_profile(request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled():
        return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            result = erp_service.update_sequence_profile(conn, data, user.nome)
        return {"ok": True, **result}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.put("/api/erp/work-orders/{work_id}/sequence")
async def erp_update_sequence_priority(work_id: str, request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled():
        return erp_disabled_response()
    user = require_login(request, db)
    if not user:
        return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            result = erp_service.update_manual_sequence_priority(conn, work_id, data.get("prioridade_manual"), user.nome)
        return {"ok": True, **result}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.get("/api/erp/work-orders")
async def erp_work_orders(
    request: Request,
    search: str = "",
    status: str = "",
    db: Session = Depends(database.get_db),
):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_DASHBOARD_READ):
        return permission_denied(api=True)
    with database.engine.connect() as conn:
        return {"ok": True, "orders": erp_service.list_work_orders(conn, search, status)}

@app.get("/api/erp/work-orders/{work_id}")
async def erp_work_order_detail(work_id: str, request: Request, db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_DASHBOARD_READ):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            return {"ok": True, **erp_service.work_order_detail(conn, work_id)}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

@app.put("/api/erp/work-orders/{work_id}")
async def erp_update_work_order(work_id: str, request: Request, data: dict = Body(...), db: Session = Depends(database.get_db)):
    if not erp_feature_enabled(): return erp_disabled_response()
    user = require_login(request, db)
    if not user: return JSONResponse({"ok": False, "error": "Login necessario."}, status_code=401)
    if not has_permission(user, authz.MES_WORK_ORDERS_MANAGE):
        return permission_denied(api=True)
    try:
        with database.engine.begin() as conn:
            result = erp_service.update_work_order(conn, work_id, data, user.nome)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

# Backend contract used by Suprimentos. It does not depend on a MES browser
# session and is protected by the same service token pattern used by Estoque.
@app.get("/api/erp/internal/catalogs")
async def erp_internal_catalogs(request: Request):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor:
        return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    return {"ok": True, **erp_catalogs.payload()}

@app.get("/api/erp/internal/work-orders")
async def erp_internal_work_orders(request: Request, search: str = "", status: str = ""):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    with database.engine.connect() as conn:
        return {"ok": True, "orders": erp_service.list_work_orders(conn, search, status)}


@app.get("/api/erp/internal/work-order-options")
async def erp_internal_work_order_options(
    request: Request,
    q: str = "",
    limit: int = 20,
):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor:
        return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    with database.engine.connect() as conn:
        return {
            "ok": True,
            "options": erp_service.active_work_order_options(conn, q, limit),
        }


@app.get("/api/erp/internal/work-orders/{work_id}")
async def erp_internal_work_order_detail(work_id: str, request: Request):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            return {"ok": True, **erp_service.work_order_detail(conn, work_id)}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)

@app.post("/api/erp/internal/vehicle-entries")
async def erp_internal_vehicle_entry(request: Request, data: dict = Body(...)):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            result = erp_service.create_entry(conn, data, actor)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/internal/vehicle-entries/{entry_id}/work-orders")
async def erp_internal_work_order(entry_id: str, request: Request, data: dict = Body(...)):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            result = erp_service.create_work_order(conn, entry_id, data, actor)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.put("/api/erp/internal/work-orders/{work_id}")
async def erp_internal_update_work_order(work_id: str, request: Request, data: dict = Body(...)):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            result = erp_service.update_work_order(conn, work_id, data, actor)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/internal/work-orders/{work_id}/activate")
async def erp_internal_activate(work_id: str, request: Request):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            result = erp_service.activate_work_order(conn, work_id, actor)
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/internal/work-orders/{work_id}/technical-close")
async def erp_internal_technical_close(
    work_id: str,
    request: Request,
    data: dict = Body(default={}),
):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor:
        return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            result = erp_service.technical_close_work_order(
                conn,
                work_id,
                actor,
                str(data.get("motivo") or data.get("reason") or ""),
            )
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/internal/work-orders/{work_id}/technical-reopen")
async def erp_internal_technical_reopen(
    work_id: str,
    request: Request,
    data: dict = Body(default={}),
):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor:
        return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            result = erp_service.technical_reopen_work_order(
                conn,
                work_id,
                actor,
                str(data.get("motivo") or data.get("reason") or ""),
            )
        return {"ok": True, **result}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

@app.post("/api/erp/internal/work-orders/{work_id}/schedules")
async def erp_internal_schedule(work_id: str, request: Request, data: dict = Body(...)):
    if not erp_feature_enabled(): return erp_disabled_response()
    actor = erp_backend_actor(request)
    if not actor: return JSONResponse({"ok": False, "error": "Token interno invalido."}, status_code=401)
    try:
        with database.engine.begin() as conn:
            erp_service.reschedule(conn, work_id, data.get("nova_data"), str(data.get("motivo") or ""), actor)
        return {"ok": True}
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8010))
    host = os.environ.get("HOST", "0.0.0.0")
    url = f"http://127.0.0.1:{port}"
    if not os.environ.get("RENDER") and os.environ.get("OPEN_BROWSER", "1") != "0":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port)
