from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from database import Base
import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Sao_Paulo")

class Veiculo(Base):
    __tablename__ = "veiculos"
    chassi = Column(String, primary_key=True, index=True)
    modelo = Column(String)
    linha = Column(String)
    ordem = Column(Integer)
    ar_condicionado = Column(String)
    cj_bco = Column(String)
    cliente = Column(String)
    destino = Column(String)
    data_entrega = Column(DateTime(timezone=True))
    localizacao = Column(String)
    banco_presente = Column(String)
    banco_comentario = Column(String)

class Apontamento(Base):
    __tablename__ = "apontamentos"
    id = Column(Integer, primary_key=True, index=True)
    chassi = Column(String, ForeignKey("veiculos.chassi"))
    etapa = Column(String)
    status = Column(String)
    responsavel = Column(String)
    inicio = Column(DateTime(timezone=True))
    termino = Column(DateTime(timezone=True))
    localizacao = Column(String)

class Historico(Base):
    __tablename__ = "historico"
    id = Column(Integer, primary_key=True, index=True)
    chassi = Column(String)
    modelo = Column(String)
    etapa = Column(String)
    status = Column(String)
    responsavel = Column(String)
    inicio = Column(DateTime(timezone=True))
    termino = Column(DateTime(timezone=True))
    localizacao = Column(String)
    data_apontamento = Column(DateTime(timezone=True), default=lambda: datetime.datetime.now(LOCAL_TZ))

class Usuario(Base):
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, unique=True, index=True)
    senha_hash = Column(String)
    is_admin = Column(Integer, default=0)

class SessaoUsuario(Base):
    __tablename__ = "sessoes_usuario"
    token = Column(String, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"))
    expira_em = Column(DateTime(timezone=True))
