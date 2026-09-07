"""Atomic material settlement inside the MES technical-close transaction."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import MetaData, and_, func, or_, select, text


ZERO = Decimal("0")
SCALE = Decimal("0.001")
COMMITMENTS = ("EMPENHO", "SAIDA")


class NegativeStockConfirmationRequired(ValueError):
    def __init__(self, negative_stock, token):
        super().__init__("A conclusão deixará saldo físico negativo. Confirme para continuar.")
        self.payload = {
            "ok": False, "code": "CONFIRM_NEGATIVE_STOCK", "error": str(self),
            "negative_stock": negative_stock, "confirmation_token": token,
        }


def quantity(value):
    try:
        result = Decimal(str(value or 0).replace(",", "."))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Quantidade inválida na composição ou no estoque. Revise antes de concluir.") from exc


def code(value):
    return str(value or "").strip().upper()


def same_uuid(left, right):
    return str(left or "").replace("-", "").lower() == str(right or "").replace("-", "").lower()


def _tables(conn):
    metadata = MetaData()
    names = (
        "movements", "stock_balances", "skus", "bom_components", "users",
        "suprimentos_documentos", "dashboard_movement_cache",
    )
    metadata.reflect(bind=conn, only=names, resolve_fks=False)
    return {name: metadata.tables[name] for name in names}


def _stored_uuid(conn, value):
    return UUID(str(value)).hex if conn.dialect.name == "sqlite" else str(value)


def _coverage(source, children):
    result = defaultdict(lambda: ZERO)

    def visit(current, factor, ancestry):
        if current in ancestry:
            raise ValueError(f"B.O.M. cíclica em {current}. Revise antes de concluir a O.S.")
        result[current] += factor
        for child, amount in children.get(current, []):
            if amount > 0:
                visit(child, factor * amount, ancestry | {current})

    visit(source, Decimal("1"), set())
    return dict(result)


def _document(conn, table, work):
    # A document explicitly linked to another revision must never be borrowed.
    candidates = list(conn.execute(select(table).where(
        table.c.tipo == "os",
        or_(table.c.erp_work_order_id == work["id"], and_(
            table.c.erp_work_order_id.is_(None), table.c.numero == work["numero_os"],
        )),
    ).order_by(table.c.updated_at.desc(), table.c.id.desc()).with_for_update()).mappings())
    direct = [row for row in candidates if same_uuid(row["erp_work_order_id"], work["id"])]
    selected = direct[0] if direct else (candidates[0] if candidates else None)
    if selected and str(selected["status"] or "").lower() == "cancelado":
        raise ValueError("O documento da O.S. está cancelado. Revise o vínculo antes de concluir.")
    return selected


def settle_work_order_materials(conn, work, actor, reason="", *, actor_user_id=None,
                                confirm_negative_stock=False, confirmation_token=None):
    """Caller holds the work-order NO KEY UPDATE lock and commits closure with this.

    Coverage follows the Estoque report: the O.S. composition is the demand;
    a commitment covers itself and its BOM; its child BAIXA is not counted twice.
    Shared parents remain unlinked, with only the consumed BAIXA assigned here.
    BOM traversal is coverage ONLY: never post backflush or component debits.
    """
    tables = _tables(conn)
    movements, balances = tables["movements"], tables["stock_balances"]
    work_id = _stored_uuid(conn, work["id"])
    document = _document(conn, tables["suprimentos_documentos"], work)
    raw_composition = document["composicao"] if document else []
    try:
        composition = json.loads(raw_composition) if isinstance(raw_composition, str) else raw_composition
    except (ValueError, TypeError) as exc:
        raise ValueError("Composição da O.S. inválida; não foi possível apurar os candidatos.") from exc
    if not isinstance(composition, list):
        raise ValueError("Composição da O.S. inválida; revise antes de concluir.")
    required = defaultdict(lambda: ZERO)
    for line in composition:
        if not isinstance(line, dict):
            raise ValueError("Linha inválida na composição da O.S.")
        item, amount = code(line.get("codigo")), quantity(line.get("qtd", line.get("quantidade")))
        if item and amount > 0:
            required[item] += amount

    skus = {row["id"]: row for row in conn.execute(select(tables["skus"])).mappings()}
    children = defaultdict(list)
    for row in conn.execute(select(tables["bom_components"])).mappings():
        parent, child = skus.get(row["item_sku_id"]), skus.get(row["component_sku_id"])
        if parent and child:
            children[code(parent["sku"])].append((code(child["sku"]), quantity(row["quantidade"])))
    coverage_cache = {}

    def coverage(sku_id):
        if sku_id not in coverage_cache:
            coverage_cache[sku_id] = _coverage(code(skus[sku_id]["sku"]), children)
        return coverage_cache[sku_id]

    parent_filter = and_(movements.c.tipo.in_(COMMITMENTS), movements.c.movement_status == "ATIVA",
                         or_(movements.c.work_order_id == work_id, and_(
                             movements.c.work_order_id.is_(None),
                             func.coalesce(movements.c.source_type, "") != "PRODUCTION_ORDER",
                         )))
    potential = list(conn.execute(select(movements).where(parent_filter)).mappings())
    ids = [m["id"] for m in potential if m["work_order_id"] is not None
           or (skus[m["sku_id"]]["active"] and set(coverage(m["sku_id"])) & set(required))]
    # Calculate sums AFTER parent locks, in a fresh READ COMMITTED statement.
    # This also serializes against manual consumption and competing O.S.
    parents = list(conn.execute(select(movements).where(parent_filter, movements.c.id.in_(ids))
                               .order_by(movements.c.id).with_for_update()).mappings()) if ids else []
    consumed = dict(conn.execute(select(movements.c.related_movement_id, func.sum(movements.c.quantidade))
                                .where(movements.c.related_movement_id.in_(ids), movements.c.tipo == "BAIXA",
                                       movements.c.movement_status == "ATIVA")
                                .group_by(movements.c.related_movement_id)).all()) if ids else {}
    allocated = list(conn.execute(select(movements).where(
        movements.c.work_order_id == work_id, movements.c.movement_status == "ATIVA",
        movements.c.tipo.in_((*COMMITMENTS, "BAIXA")),
    )).mappings())
    linked_ids = {m["id"] for m in allocated if m["tipo"] in COMMITMENTS}
    remaining = dict(required)

    def cover(sku_id, amount):
        for item, factor in coverage(sku_id).items():
            if item in remaining:
                remaining[item] = max(ZERO, remaining[item] - amount * factor)

    for movement in allocated:
        if movement["tipo"] == "BAIXA" and movement["related_movement_id"] in linked_ids:
            continue
        cover(movement["sku_id"], quantity(movement["quantidade"]))

    commands = []
    for parent in parents:
        if parent["work_order_id"] is None:
            continue
        pending = max(ZERO, quantity(parent["quantidade"]) - quantity(consumed.get(parent["id"])))
        if pending:
            if parent["source_type"] == "PRODUCTION_ORDER":
                raise ValueError(f"Empenho {parent['id']} reservado a uma O.P. interna. Revise o vínculo; seu backflush não pode ser antecipado pela O.S.")
            commands.append({"parent": parent, "quantity": pending, "kind": "VINCULADO"})

    # A direct debit without an empenho ID is valid coverage, but cannot be
    # silently matched to a different open commitment for the same SKU.
    # Fail closed instead of debiting twice or rewriting historical movements.
    pending_skus = {c["parent"]["sku_id"] for c in commands}
    ambiguous = [m for m in allocated if m["tipo"] == "BAIXA"
                 and m["related_movement_id"] is None and m["sku_id"] in pending_skus
                 and m["source_type"] != "BACKFLUSH_CONSUMPTION"]
    if ambiguous:
        details = "; ".join(f"{skus[m['sku_id']]['sku']} (baixa ID {m['id']})" for m in ambiguous)
        raise ValueError(
            "Há baixa direta da O.S. e empenho ainda aberto para o mesmo SKU: " + details + ". "
            "Confira o vínculo entre essas baixas e os empenhos antes de concluir, "
            "para evitar duplicidade. Nenhuma baixa automática foi gravada."
        )

    # Ancestors before children; FIFO within the same source SKU. Reusing a
    # single remaining-demand map prevents one kit from being consumed again
    # for every child row displayed by the material report.
    shared = [p for p in parents if p["work_order_id"] is None and skus[p["sku_id"]]["active"]]
    shared.sort(key=lambda p: (-len(coverage(p["sku_id"])), p["created_at"], p["id"]))
    for parent in shared:
        pending = max(ZERO, quantity(parent["quantidade"]) - quantity(consumed.get(parent["id"])))
        ratios = [remaining[item] / factor for item, factor in coverage(parent["sku_id"]).items()
                  if item in remaining and factor > 0]
        amount = min([pending, *ratios]).quantize(SCALE, rounding=ROUND_DOWN) if ratios else ZERO
        if amount > 0:
            commands.append({"parent": parent, "quantity": amount, "kind": "COMPARTILHADO"})
            cover(parent["sku_id"], amount)

    residual = [{"codigo": item, "quantidade_pendente": str(amount),
                 "status": "ENCERRADA_TECNICAMENTE"}
                for item, amount in sorted(remaining.items()) if amount > 0]
    totals = defaultdict(lambda: ZERO)
    for command in commands:
        totals[command["parent"]["sku_id"]] += command["quantity"]
    locked_balances = {row["sku_id"]: row for row in conn.execute(select(balances)
                       .where(balances.c.sku_id.in_(sorted(totals)))
                       .order_by(balances.c.sku_id).with_for_update()).mappings()} if totals else {}
    # Missing balance rows are invalid stock data, not permission for a negative balance.
    missing = set(totals) - set(locked_balances)
    if missing:
        raise ValueError("SKU sem registro de saldo: " + ", ".join(str(skus[i]["sku"]) for i in sorted(missing)))
    negative = []
    for sku_id, amount in sorted(totals.items()):
        before = quantity(locked_balances[sku_id]["saldo_atual"])
        if before - amount < 0:
            negative.append({"codigo": skus[sku_id]["sku"], "saldo_atual": str(before),
                             "quantidade_baixar": str(amount), "saldo_final": str(before - amount)})
    fingerprint = sha256(json.dumps({
        "work": str(work["id"]), "actor": actor,
        "commands": [(c["parent"]["id"], str(c["quantity"]), c["kind"]) for c in commands],
        "negative": negative, "residual": residual,
    }, sort_keys=True).encode()).hexdigest()
    if negative and (confirm_negative_stock is not True or confirmation_token != fingerprint):
        raise NegativeStockConfirmationRequired(negative, fingerprint)

    user = None
    if commands:
        users = tables["users"]
        predicate = users.c.id == int(actor_user_id) if actor_user_id is not None else users.c.username == actor
        user = conn.execute(select(users).where(predicate, users.c.active.is_(True))).mappings().first()
        if not user:
            raise ValueError("Usuário responsável pela conclusão não foi localizado ou está inativo.")

    movement_ids = []
    operation_id = _stored_uuid(conn, uuid4())
    current_balances = {key: quantity(row["saldo_atual"]) for key, row in locked_balances.items()}
    for command in commands:
        parent, amount = command["parent"], command["quantity"]
        sku_id = parent["sku_id"]
        before = current_balances[sku_id]
        after = before - amount
        kind = command["kind"]
        values = dict(sku_id=sku_id, tipo="BAIXA", quantidade=amount, saldo_anterior=before,
                      saldo_posterior=after, usuario_id=user["id"], related_movement_id=parent["id"],
                      documento=f"OS {work['numero_os']}",
                      observacao=f"Conclusão técnica da O.S. {work['numero_os']}. Empenho {parent['id']} ({kind}). {reason}",
                      work_order_id=work_id, context_kind="WORK_ORDER", setor=parent["setor"],
                      reference_text=parent["reference_text"], link_updated_at=func.now(), link_updated_by=user["id"],
                      source_type="TECHNICAL_CLOSE_AUTO_BAIXA", source_id=work_id,
                      idempotency_key=f"technical-close:{operation_id}:{parent['id']}",
                      operation_id=operation_id, movement_status="ATIVA", created_at=func.now())
        new_id = conn.execute(movements.insert().values(**values).returning(movements.c.id)).scalar_one()
        conn.execute(balances.update().where(balances.c.sku_id == sku_id)
                     .values(saldo_atual=after, updated_at=func.now()))
        cache = tables["dashboard_movement_cache"]
        conn.execute(cache.insert().values(
            movement_id=new_id, created_at=func.now(), cached_at=func.now(), usuario_id=user["id"],
            usuario_nome=user["username"], sku_id=sku_id, sku_codigo=skus[sku_id]["sku"],
            descricao=skus[sku_id]["descricao"], tipo="BAIXA", quantidade=amount,
            saldo_anterior=before, saldo_posterior=after, documento=values["documento"], observacao=values["observacao"],
        ))
        current_balances[sku_id] = after
        movement_ids.append(new_id)

    if movement_ids:
        cache = tables["dashboard_movement_cache"]
        older = select(cache.c.id).order_by(cache.c.created_at.desc(), cache.c.id.desc()).offset(10)
        conn.execute(cache.delete().where(cache.c.id.in_(older)))

    return {
        "operation_id": str(operation_id), "document_id": str(document["id"]) if document else None,
        "document_previous_status": document["status"] if document else None,
        "baixas_criadas": len(commands),
        "empenhos_vinculados_baixados": sum(c["kind"] == "VINCULADO" for c in commands),
        "candidatos_consumidos": sum(c["kind"] == "COMPARTILHADO" for c in commands),
        "quantidade_baixada": str(sum((c["quantity"] for c in commands), ZERO)),
        "pendencias_encerradas": residual, "negative_stock": negative,
        "saldo_negativo_confirmado": bool(negative), "movement_ids": movement_ids,
    }


def reopen_settled_document(conn, work_id):
    """Restore only the document changed by this settlement, never undo stock."""
    event = conn.execute(text("""
        select after_data from erp_audit_events
        where entity_type='WORK_ORDER' and entity_id=:id and action='CONCLUSAO_TECNICA'
        order by created_at desc,id desc limit 1
    """), {"id": work_id}).mappings().first()
    if not event:
        return
    data = event["after_data"]
    if isinstance(data, str):
        data = json.loads(data)
    settlement = (data or {}).get("auto_baixas") or {}
    previous = settlement.get("document_previous_status")
    if settlement.get("document_id") and previous:
        conn.execute(text("""
            update suprimentos_documentos set status=:previous,updated_at=now()
            where id=:id and tipo='os' and status='concluido'
        """), {"id": int(settlement["document_id"]), "previous": previous})
