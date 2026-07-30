"""Read-only reconciliation between the legacy MES Supabase and shared ERP.

This program deliberately has no write path.  Its output is the approval
matrix required before a staged MES migration can create ERP work orders or
bring across stage events.  It never reads the local Docker database.

Required environment variables (set only in the shell/session that runs it):
    MES_LEGACY_DATABASE_URL  Connection to the legacy MES project.
    ERP_TARGET_DATABASE_URL  Connection to the shared ERP project.

The URLs are intentionally not accepted as command-line arguments: command
history is not an appropriate place for database credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


LEGACY_URL_ENV = "MES_LEGACY_DATABASE_URL"
TARGET_URL_ENV = "ERP_TARGET_DATABASE_URL"
LEGACY_TABLES = {"veiculos", "apontamentos", "historico"}
TARGET_TABLES = {"suprimentos_documentos"}


def normalize_chassis(value: Any) -> str:
    """Return a chassis comparison key without treating a short value as valid."""
    if value is None:
        return ""
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    return normalized if len(normalized) >= 8 else ""


def json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def first_populated(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def stage_status(status: Any) -> str:
    """Translate the old card state without inventing a productive event."""
    source = str(status or "").strip().upper()
    return {
        "SIM": "CONCLUIDA",
        "NÃO": "PENDENTE",
        "NAO": "PENDENTE",
        "N/A": "NAO_APLICAVEL",
        "P": "EM_ANDAMENTO",
    }.get(source, "DESCONHECIDO")


def safe_url_label(url: str) -> str:
    """Return only a safe connection description for reports and logs."""
    try:
        # Covers postgresql://user:password@host/db and SQLAlchemy dialect URLs.
        without_scheme = url.split("://", 1)[-1]
        host_part = without_scheme.rsplit("@", 1)[-1]
        return host_part.split("?", 1)[0]
    except Exception:  # pragma: no cover - defensive reporting only
        return "configured"


def required_tables(engine: Engine) -> set[str]:
    # Use a connection so this works with SQLAlchemy 1.4 and 2.x.
    with engine.connect() as connection:
        result = connection.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
        return {str(row[0]) for row in result}


def require_schema(engine: Engine, expected: set[str], label: str) -> None:
    missing = expected - required_tables(engine)
    if missing:
        formatted = ", ".join(sorted(missing))
        raise RuntimeError(f"Banco {label} não possui as tabelas esperadas: {formatted}.")


def fetch_legacy(engine: Engine) -> tuple[list[dict[str, Any]], Counter[str], Counter[str]]:
    with engine.connect() as connection:
        vehicles = [dict(row) for row in connection.execute(text("""
            SELECT id, chassi, modelo, cliente, destino, data_entrega, linha,
                   ar_condicionado, cj_bco, localizacao, created_at
            FROM public.veiculos
            ORDER BY chassi
        """)).mappings()]
        stage_rows = connection.execute(text("""
            SELECT chassi, etapa, status, count(*) AS quantidade
            FROM public.apontamentos
            GROUP BY chassi, etapa, status
        """)).mappings()
        event_rows = connection.execute(text("""
            SELECT chassi, etapa, status, count(*) AS quantidade
            FROM public.historico
            GROUP BY chassi, etapa, status
        """)).mappings()

    stages_by_chassis: Counter[str] = Counter()
    events_by_chassis: Counter[str] = Counter()
    stage_distribution: Counter[str] = Counter()
    for row in stage_rows:
        key = normalize_chassis(row["chassi"])
        if key:
            stages_by_chassis[key] += int(row["quantidade"])
        stage_distribution[f"{row['etapa']}|{stage_status(row['status'])}"] += int(row["quantidade"])
    for row in event_rows:
        key = normalize_chassis(row["chassi"])
        if key:
            events_by_chassis[key] += int(row["quantidade"])

    for vehicle in vehicles:
        key = normalize_chassis(vehicle.get("chassi"))
        vehicle["comparison_chassi"] = key
        vehicle["legacy_stage_rows"] = stages_by_chassis[key]
        vehicle["legacy_event_rows"] = events_by_chassis[key]
    return vehicles, stage_distribution, events_by_chassis


def fetch_target_orders(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT id, numero, status, dados, updated_at
            FROM public.suprimentos_documentos
            WHERE tipo = 'os'
            ORDER BY id
        """)).mappings()

        orders: list[dict[str, Any]] = []
        for row in rows:
            data = json_value(row["dados"])
            chassis = first_populated(data, ("chassi", "chassis", "CHASSI"))
            orders.append(
                {
                    "id": row["id"],
                    "numero": row["numero"],
                    "status": row["status"],
                    "updated_at": row["updated_at"],
                    "chassi": chassis,
                    "comparison_chassi": normalize_chassis(chassis),
                    "cliente": first_populated(data, ("cliente", "Cliente", "CLIENTE")),
                    "linha": first_populated(data, ("linha", "Linha", "LINHA")),
                }
            )
    return orders


def classify_candidates(
    legacy_vehicles: Iterable[dict[str, Any]], target_orders: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build an approval matrix. A match is only a suggestion, never an import."""
    indexed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in target_orders:
        key = order.get("comparison_chassi") or normalize_chassis(order.get("chassi"))
        if key:
            indexed[key].append(order)

    matrix: list[dict[str, Any]] = []
    for vehicle in legacy_vehicles:
        key = vehicle.get("comparison_chassi") or normalize_chassis(vehicle.get("chassi"))
        candidates = indexed.get(key, []) if key else []
        if not key:
            classification = "INVALID_LEGACY_CHASSI"
        elif not candidates:
            classification = "UNMATCHED"
        elif len(candidates) == 1:
            classification = "UNIQUE_CANDIDATE_REQUIRES_APPROVAL"
        else:
            classification = "AMBIGUOUS_MULTIPLE_TARGET_ORDERS"

        matrix.append(
            {
                "classification": classification,
                "legacy_vehicle_id": vehicle.get("id"),
                "legacy_chassi": vehicle.get("chassi"),
                "legacy_modelo": vehicle.get("modelo"),
                "legacy_cliente": vehicle.get("cliente"),
                "legacy_linha": vehicle.get("linha"),
                "legacy_stage_rows": vehicle.get("legacy_stage_rows", 0),
                "legacy_event_rows": vehicle.get("legacy_event_rows", 0),
                "candidate_orders": [
                    {
                        "document_id": candidate.get("id"),
                        "numero_os": candidate.get("numero"),
                        "status": candidate.get("status"),
                        "chassi": candidate.get("chassi"),
                        "cliente": candidate.get("cliente"),
                        "linha": candidate.get("linha"),
                    }
                    for candidate in candidates
                ],
            }
        )
    return matrix


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def build_report(legacy_engine: Engine, target_engine: Engine) -> dict[str, Any]:
    require_schema(legacy_engine, LEGACY_TABLES, "MES legado")
    require_schema(target_engine, TARGET_TABLES, "ERP compartilhado")
    legacy_vehicles, stage_distribution, _events = fetch_legacy(legacy_engine)
    target_orders = fetch_target_orders(target_engine)
    matrix = classify_candidates(legacy_vehicles, target_orders)
    classes = Counter(row["classification"] for row in matrix)

    return {
        "report_type": "MES_LEGACY_TO_SHARED_ERP_RECONCILIATION",
        "mode": "READ_ONLY_DRY_RUN",
        "generated_at": datetime.now().astimezone().isoformat(),
        "connections": {
            "legacy_mes": safe_url_label(os.environ[LEGACY_URL_ENV]),
            "shared_erp": safe_url_label(os.environ[TARGET_URL_ENV]),
        },
        "summary": {
            "legacy_vehicles": len(legacy_vehicles),
            "legacy_stage_rows": sum(item.get("legacy_stage_rows", 0) for item in legacy_vehicles),
            "legacy_event_rows": sum(item.get("legacy_event_rows", 0) for item in legacy_vehicles),
            "target_work_order_documents": len(target_orders),
            "candidate_classifications": dict(sorted(classes.items())),
        },
        "legacy_stage_distribution": dict(sorted(stage_distribution.items())),
        "approval_matrix": matrix,
        "safety": {
            "writes_performed": False,
            "automatic_matches_approved": False,
            "rule": (
                "Mesmo uma correspondência única exige aprovação explícita antes "
                "de qualquer migração. Chassi é somente critério de sugestão, "
                "pois pode existir retorno/pós-venda."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Concilia MES legado e ERP compartilhado em modo somente leitura.")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("mes_legacy_reconciliation_report.json"),
        help="Caminho do relatório JSON de saída (padrão: diretório atual).",
    )
    args = parser.parse_args()

    legacy_url = os.getenv(LEGACY_URL_ENV)
    target_url = os.getenv(TARGET_URL_ENV)
    missing = [name for name, value in ((LEGACY_URL_ENV, legacy_url), (TARGET_URL_ENV, target_url)) if not value]
    if missing:
        print("Variáveis obrigatórias ausentes: " + ", ".join(missing), file=sys.stderr)
        return 2

    legacy_engine = create_engine(legacy_url, pool_pre_ping=True)
    target_engine = create_engine(target_url, pool_pre_ping=True)
    try:
        report = build_report(legacy_engine, target_engine)
    finally:
        legacy_engine.dispose()
        target_engine.dispose()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8")
    print("Relatório somente leitura criado: " + str(args.report.resolve()))
    print("Resumo: " + json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
