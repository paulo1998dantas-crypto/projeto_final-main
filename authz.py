"""Autenticação compartilhada e autorização do MES.

O modo legado continua disponível para rollback. No modo ``shared_users`` as
credenciais vêm de ``public.users`` (fonte de verdade do Estoque), enquanto a
sessão do MES permanece isolada em ``public.erp_app_sessions``.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import os
import secrets

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.security import check_password_hash


SHARED_SESSION_COOKIE = "erp_mes_sessao"
APP_CODE = "MES"
SESSION_DAYS = 30

MES_DASHBOARD_READ = "mes.dashboard.read"
MES_STAGE_WRITE = "mes.stage.write"
MES_EXPORTS_READ = "mes.exports.read"
MES_WORK_ORDERS_MANAGE = "mes.work_orders.manage"
MES_VEHICLE_ENTRIES_CREATE = "mes.vehicle_entries.create"
MES_SCHEDULE_MANAGE = "mes.schedule.manage"
MES_FINALIZE = "mes.finalize"
MES_LEGACY_IMPORT = "mes.legacy.import"
MES_USERS_MANAGE = "mes.users.manage"

ALL_MES_PERMISSIONS = frozenset({
    MES_DASHBOARD_READ,
    MES_STAGE_WRITE,
    MES_EXPORTS_READ,
    MES_WORK_ORDERS_MANAGE,
    MES_VEHICLE_ENTRIES_CREATE,
    MES_SCHEDULE_MANAGE,
    MES_FINALIZE,
    MES_LEGACY_IMPORT,
    MES_USERS_MANAGE,
})

VIEW_ONLY_PERMISSIONS = frozenset({
    MES_DASHBOARD_READ,
    MES_EXPORTS_READ,
})

OPERATIONAL_MANAGEMENT_PERMISSIONS = frozenset({
    *VIEW_ONLY_PERMISSIONS,
    MES_STAGE_WRITE,
    MES_WORK_ORDERS_MANAGE,
    MES_VEHICLE_ENTRIES_CREATE,
    MES_SCHEDULE_MANAGE,
    MES_FINALIZE,
})

ROLE_DEFAULT_PERMISSIONS = {
    "ADMIN": ALL_MES_PERMISSIONS,
    "ADM": ALL_MES_PERMISSIONS,
    # O operador acompanha o MES e registra somente a execução das etapas.
    # Abertura/parametrização de O.S., sequenciamento, finalização e gestão de
    # usuários continuam restritos aos perfis de gestão.
    "OPERADOR": frozenset({
        *VIEW_ONLY_PERMISSIONS,
        MES_STAGE_WRITE,
    }),
    "PRODUCAO": frozenset({
        MES_DASHBOARD_READ,
        MES_STAGE_WRITE,
    }),
    "COMPRADOR": VIEW_ONLY_PERMISSIONS,
    "PCP": OPERATIONAL_MANAGEMENT_PERMISSIONS,
    "ENGENHARIA": VIEW_ONLY_PERMISSIONS,
    "FINANCEIRO": frozenset({MES_DASHBOARD_READ}),
}


def auth_mode():
    value = os.environ.get("MES_AUTH_MODE", "legacy").strip().lower()
    if value not in {"legacy", "shared_users"}:
        raise RuntimeError(
            "MES_AUTH_MODE inválido. Use 'legacy' ou 'shared_users'; "
            "a autenticação não fará fallback silencioso."
        )
    return value


def shared_auth_enabled():
    return auth_mode() == "shared_users"


def normalize_role(value):
    role = str(value or "").strip().upper()
    return "ADMIN" if role == "ADM" else role


@dataclass(frozen=True)
class Principal:
    id: int
    nome: str
    username: str
    active: bool
    auth_version: int
    roles: frozenset[str]
    permissions: frozenset[str]
    legacy: bool = False

    @property
    def is_admin(self):
        return "ADMIN" in self.roles

    def can(self, permission):
        return self.is_admin or permission in self.permissions


def principal_from_legacy(usuario):
    roles = frozenset({"ADMIN"} if bool(usuario.is_admin) else {"OPERADOR"})
    permissions = _default_permissions(roles)
    return Principal(
        id=int(usuario.id),
        nome=str(usuario.nome),
        username=str(usuario.nome),
        active=True,
        auth_version=1,
        roles=roles,
        permissions=permissions,
        legacy=True,
    )


@lru_cache(maxsize=32)
def _table_exists(engine, table_name):
    return inspect(engine).has_table(table_name)


def _column_exists(engine, table_name, column_name):
    inspector = inspect(engine)
    if not inspector.has_table(table_name):
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def shared_schema_status(engine):
    required_tables = (
        "users",
        "erp_roles",
        "erp_permissions",
        "erp_role_permissions",
        "erp_user_roles",
        "erp_user_permission_overrides",
        "erp_app_sessions",
    )
    missing_tables = [name for name in required_tables if not _table_exists(engine, name)]
    missing_columns = []
    for table_name, column_name in (
        ("users", "id"),
        ("users", "username"),
        ("users", "password_hash"),
        ("users", "role"),
        ("users", "active"),
        ("users", "auth_version"),
        ("erp_roles", "code"),
        ("erp_roles", "active"),
        ("erp_permissions", "code"),
        ("erp_role_permissions", "role_code"),
        ("erp_role_permissions", "permission_code"),
        ("erp_user_roles", "user_id"),
        ("erp_user_roles", "role_code"),
        ("erp_user_permission_overrides", "user_id"),
        ("erp_user_permission_overrides", "permission_code"),
        ("erp_user_permission_overrides", "allowed"),
        ("erp_app_sessions", "id"),
        ("erp_app_sessions", "token_hash"),
        ("erp_app_sessions", "user_id"),
        ("erp_app_sessions", "app_code"),
        ("erp_app_sessions", "auth_version"),
        ("erp_app_sessions", "expires_at"),
        ("erp_app_sessions", "revoked_at"),
        ("erp_app_sessions", "created_at"),
        ("erp_app_sessions", "last_seen_at"),
    ):
        if table_name not in missing_tables and not _column_exists(engine, table_name, column_name):
            missing_columns.append(f"{table_name}.{column_name}")
    return {
        "ready": not missing_tables and not missing_columns,
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
    }


def _default_permissions(roles):
    permissions = set()
    for role in roles:
        permissions.update(ROLE_DEFAULT_PERMISSIONS.get(normalize_role(role), ()))
    return frozenset(permissions)


def _load_roles(conn, user_row):
    rows = conn.execute(text("""
        select ur.role_code
          from public.erp_user_roles ur
          join public.erp_roles r on r.code = ur.role_code
         where ur.user_id=:user_id
           and r.active = true
    """), {"user_id": user_row["id"]}).scalars()
    return frozenset(
        normalize_role(row)
        for row in rows
        if normalize_role(row)
    )


def _load_permissions(conn, user_id, roles):
    mapped = set()
    if roles:
        rows = list(conn.execute(text("""
            select permission_code
              from public.erp_role_permissions
             where role_code = any(:roles)
        """), {"roles": list(roles)}).scalars())
        mapped.update(str(row).strip() for row in rows if str(row).strip())

    overrides = conn.execute(text("""
        select permission_code,allowed
          from public.erp_user_permission_overrides
         where user_id=:user_id
    """), {"user_id": user_id}).mappings()
    for override in overrides:
        code = str(override["permission_code"] or "").strip()
        if not code:
            continue
        if bool(override["allowed"]):
            mapped.add(code)
        else:
            mapped.discard(code)
    return frozenset(mapped)


def _principal_from_shared_row(conn, row):
    row = dict(row)
    roles = _load_roles(conn, row)
    return Principal(
        id=int(row["id"]),
        nome=str(row["username"]),
        username=str(row["username"]),
        active=bool(row["active"]),
        auth_version=int(row.get("auth_version") or 1),
        roles=roles,
        permissions=_load_permissions(conn, row["id"], roles),
        legacy=False,
    )


def authenticate_shared_user(engine, username, password):
    status = shared_schema_status(engine)
    if not status["ready"]:
        raise RuntimeError(
            "Autenticação compartilhada ainda não está preparada: "
            + ", ".join(status["missing_tables"] + status["missing_columns"])
        )
    with engine.connect() as conn:
        row = conn.execute(text("""
            select id,username,password_hash,role,active,auth_version
              from public.users
             where upper(username)=upper(:username)
             limit 1
        """), {"username": str(username or "").strip()}).mappings().first()
        if not row or not bool(row["active"]):
            return None
        if not check_password_hash(str(row["password_hash"] or ""), str(password or "")):
            return None
        return _principal_from_shared_row(conn, row)


def load_shared_principal(engine, user_id):
    """Load an active principal after a Portal SSO assertion was verified."""
    status = shared_schema_status(engine)
    if not status["ready"]:
        raise RuntimeError(
            "Autenticacao compartilhada ainda nao esta preparada: "
            + ", ".join(status["missing_tables"] + status["missing_columns"])
        )
    with engine.connect() as conn:
        row = conn.execute(text("""
            select id,username,password_hash,role,active,auth_version
              from public.users
             where id=:user_id
             limit 1
        """), {"user_id": int(user_id)}).mappings().first()
        if not row or not bool(row["active"]):
            return None
        return _principal_from_shared_row(conn, row)


def _token_hash(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def create_shared_session(engine, principal):
    token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=SESSION_DAYS)
    with engine.begin() as conn:
        conn.execute(text("""
            insert into public.erp_app_sessions(
                token_hash,user_id,app_code,auth_version,expires_at,
                revoked_at,created_at,last_seen_at
            ) values(
                :token_hash,:user_id,:app_code,:auth_version,:expires_at,
                null,:now,:now
            )
        """), {
            "token_hash": _token_hash(token),
            "user_id": principal.id,
            "app_code": APP_CODE,
            "auth_version": principal.auth_version,
            "expires_at": expires_at,
            "now": now,
        })
    return token


def get_shared_current_user(request, engine):
    token = (request.cookies.get(SHARED_SESSION_COOKIE) or "").strip()
    if not token:
        return None
    token_hash = _token_hash(token)
    try:
        with engine.begin() as conn:
            row = conn.execute(text("""
                select u.id,u.username,u.password_hash,u.role,u.active,u.auth_version,
                       s.id as session_id,s.auth_version as session_auth_version
                  from public.erp_app_sessions s
                  join public.users u on u.id=s.user_id
                 where s.token_hash=:token_hash
                   and s.app_code=:app_code
                   and s.revoked_at is null
                   and s.expires_at>now()
                 for update of s
            """), {"token_hash": token_hash, "app_code": APP_CODE}).mappings().first()
            if not row:
                return None
            if not bool(row["active"]) or int(row["session_auth_version"]) != int(row["auth_version"]):
                conn.execute(text("""
                    update public.erp_app_sessions
                       set revoked_at=coalesce(revoked_at,now())
                     where id=:session_id
                """), {"session_id": row["session_id"]})
                return None
            conn.execute(text("""
                update public.erp_app_sessions
                   set last_seen_at=now()
                 where id=:session_id
                   and (last_seen_at is null or last_seen_at < now() - interval '5 minutes')
            """), {"session_id": row["session_id"]})
            return _principal_from_shared_row(conn, row)
    except SQLAlchemyError:
        # O modo compartilhado é fail-closed: schema indisponível nunca
        # reabilita silenciosamente a autenticação legada.
        return None


def revoke_shared_session(engine, token):
    token = str(token or "").strip()
    if not token:
        return
    try:
        with engine.begin() as conn:
            conn.execute(text("""
                update public.erp_app_sessions
                   set revoked_at=coalesce(revoked_at,now())
                 where token_hash=:token_hash
                   and app_code=:app_code
            """), {"token_hash": _token_hash(token), "app_code": APP_CODE})
    except SQLAlchemyError:
        return


def can(principal, permission):
    return bool(principal and principal.can(permission))
