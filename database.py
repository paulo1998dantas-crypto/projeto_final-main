from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# ======================================================
# CONFIGURAÇÃO DE BANCO DE DADOS – SUPABASE (POSTGRESQL)
# ======================================================
# Prioriza variável de ambiente (Render / Produção)
# Mantém fallback explícito para uso local controlado
# Estrutura idêntica ao SQLite original (mínima alteração)

SQLALCHEMY_DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres.rfjgwjtykqmriijsqlac:Pn5833313000@aws-1-us-east-1.pooler.supabase.com:5432/postgres"
)

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
