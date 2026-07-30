from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os


def load_local_env():
    """Load optional developer-only configuration without overriding real env vars.

    `.env.local` is ignored by Git and is deliberately not used in Render.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()

# ======================================================
# CONFIGURAÇÃO DE BANCO DE DADOS – SUPABASE (POSTGRESQL)
# ======================================================
# Prioriza variável de ambiente (Render / Produção)
# Mantém fallback explícito para uso local controlado
# Estrutura idêntica ao SQLite original (mínima alteração)

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    ""
)

if not SQLALCHEMY_DATABASE_URL:
    raise RuntimeError("DATABASE_URL e obrigatoria. Configure-a no ambiente; nunca use credenciais no codigo.")

# Engine configurada para PostgreSQL com pool estável
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,     # evita conexões mortas
    pool_size=5,            # conexões persistentes
    max_overflow=10         # pico de conexões
)

# Session padrão (mantida exatamente como no projeto original)
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Base declarativa (inalterada)
Base = declarative_base()

# ======================================================
# DEPENDÊNCIA DE SESSÃO PARA FASTAPI
# ======================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
