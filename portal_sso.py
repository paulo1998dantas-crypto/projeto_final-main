"""Contrato de SSO do Portal Operacional."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode, urlsplit


def _enabled(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def enabled() -> bool:
    return _enabled("ERP_PORTAL_SSO_ENABLED")


def normalize_next(value: str | None, default: str = "/") -> str:
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if not candidate.startswith("/") or candidate.startswith("//") or parsed.scheme or parsed.netloc:
        return default
    return candidate


def _portal_url() -> str:
    return os.environ.get("ERP_PORTAL_URL", "https://ji-portal-operacional.onrender.com").strip().rstrip("/")


def portal_login_url(app_code: str, next_path: str | None) -> str:
    return f"{_portal_url()}/login?{urlencode({'app': app_code, 'next': normalize_next(next_path)})}"


def portal_logout_url(return_to: str = "/") -> str:
    return f"{_portal_url()}/logout?{urlencode({'return_to': normalize_next(return_to)})}"


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def consume_ticket(ticket: str | None, expected_app: str) -> dict:
    secret = os.environ.get("ERP_PORTAL_SSO_SECRET", "").encode("utf-8")
    if not secret:
        raise ValueError("SSO central nao configurado neste modulo.")
    raw_ticket = str(ticket or "")
    if not raw_ticket or len(raw_ticket) > 4096 or raw_ticket.count(".") != 1:
        raise ValueError("Comprovante de acesso invalido.")
    encoded, supplied_signature = raw_ticket.rsplit(".", 1)
    expected_signature = hmac.new(secret, encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ValueError("Comprovante de acesso invalido.")
    try:
        claims = json.loads(_b64decode(encoded).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Comprovante de acesso invalido.") from exc
    if not isinstance(claims, dict) or str(claims.get("app") or "").upper() != expected_app.upper():
        raise ValueError("Comprovante destinado a outro modulo.")
    now = int(time.time())
    try:
        issued_at, expires_at = int(claims["iat"]), int(claims["exp"])
        user_id, auth_version = int(claims["uid"]), int(claims["auth_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Comprovante de acesso incompleto.") from exc
    if user_id <= 0 or auth_version < 0 or issued_at > now + 30 or expires_at < now or expires_at - issued_at > 180:
        raise ValueError("Comprovante de acesso expirado.")
    username = str(claims.get("username") or "").strip()
    if not username or len(username) > 128:
        raise ValueError("Comprovante de acesso invalido.")
    claims.update({"uid": user_id, "auth_version": auth_version, "username": username, "next": normalize_next(claims.get("next"))})
    return claims
