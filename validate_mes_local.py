"""Validação local do ciclo entrada -> O.S. -> MES -> entrega.

Não remove histórico: termina as duas O.S. de teste como ENTREGUE, do mesmo
modo que a operação real preservaria uma passagem já encerrada.
"""
import json
from datetime import date, datetime
from uuid import uuid4
from sqlalchemy import text

import database
from erp_service import (
    STAGES, active_cards, activate_work_order, configure_stages, create_entry,
    create_work_order, finalize, reschedule, update_stage,
)


def open_and_activate(conn, chassis, actor):
    entry = create_entry(conn, {
        "chassi": chassis, "cliente_nome": "VALIDAÇÃO LOCAL",
        "marca": "MERCEDES-BENZ", "modelo": "SPRINTER 417", "versao": "10,5 M³",
    }, actor)
    work = create_work_order(conn, entry["id"], {
        "tipo_servico": "TRANSFORMAÇÃO",
        "proposta_numero": "VALIDAÇÃO-LOCAL",
        "data_aprovacao": date(2026, 7, 29),
        "vendedor": "JI",
        "mercado": "VAREJO",
        "cliente_nome": "VALIDAÇÃO LOCAL",
        "municipio": "CAXIAS DO SUL",
        "uf": "RS",
        "tipo_veiculo": "MICRO",
        "linha": "LB",
        "transformacao_codigo": "40340009",
        "transformacao": "JI CONFORT 417 10 M SELADO ESSENCIAL",
        "conjunto_bancos": "BANCO TESTE",
        "ar_condicionado": "GE",
        "tipo_sistema_ar": "COMPLEMENTO",
        "ar_quente": "NÃO",
        "data_comercial_prevista": date(2026, 8, 10),
    }, actor)
    try:
        activate_work_order(conn, work["id"], actor)
        raise AssertionError("O.S. foi ativada antes da parametrização das etapas.")
    except ValueError as exc:
        assert "parametrização" in str(exc)
    configured = configure_stages(
        conn,
        work["id"],
        {"stages": {code: "N" for code, _, _ in STAGES}, "complete": True},
        actor,
    )
    assert configured["complete"] is True
    activate_work_order(conn, work["id"], actor)
    return entry, work


def main():
    actor = "validacao-local"
    chassis = f"LOCAL{uuid4().hex[:11]}".upper()
    with database.engine.begin() as conn:
        before = len(active_cards(conn))
        entry, work = open_and_activate(conn, chassis, actor)
        try:
            update_stage(conn, work["id"], "DESMONT", {"status": "CONCLUÍDA", "idempotency_key": str(uuid4())}, actor)
            raise AssertionError("DESMONT foi liberada sem VIDROS e A/C.")
        except ValueError as exc:
            dependency_block = str(exc)
        for stage in ("VIDROS", "A/C"):
            update_stage(conn, work["id"], stage, {"status": "CONCLUÍDA", "responsavel": actor, "idempotency_key": str(uuid4())}, actor)
        stage_key = str(uuid4())
        first_stage = update_stage(conn, work["id"], "DESMONT", {"status": "EM_ANDAMENTO", "responsavel": actor, "idempotency_key": stage_key}, actor)
        replayed_stage = update_stage(conn, work["id"], "DESMONT", {"status": "EM_ANDAMENTO", "responsavel": actor, "idempotency_key": stage_key}, actor)
        reschedule(conn, work["id"], date(2026, 8, 15), "Teste de programação", actor)
        reschedule(conn, work["id"], date(2026, 8, 20), "Reprogramação de teste", actor)
        current_schedules = conn.execute(text("select count(*) from erp_work_order_schedules where work_order_id=:id and vigente"), {"id": work["id"]}).scalar_one()
        planned_date = conn.execute(text("select data_comercial_prevista from erp_work_orders where id=:id"), {"id": work["id"]}).scalar_one()
        active_during = len(active_cards(conn))
        finalized = finalize(conn, work["id"], actor, delivered=False, notes="Fim da produção", event_at=datetime(2026, 8, 19, 17, 0))
        active_after = len(active_cards(conn))
        delivered = finalize(conn, work["id"], actor, delivered=True, notes="Entrega da validação local", event_at=datetime(2026, 8, 20, 10, 0))
        second_entry, second_work = open_and_activate(conn, chassis, actor)
        returned = finalize(conn, second_work["id"], actor, notes="Retirada do retorno", target_status="RETIRADA")
        history_count = conn.execute(text("select count(*) from erp_vehicle_entries e join erp_vehicles v on v.id=e.vehicle_id where v.chassi=:chassi"), {"chassi": chassis}).scalar_one()
        status_history = conn.execute(text("select count(*) from erp_work_order_status_history where work_order_id=:id"), {"id": work["id"]}).scalar_one()
    assert current_schedules == 1
    assert active_during == before + 1 and active_after == before
    assert history_count == 2
    assert first_stage["replayed"] is False and replayed_stage["replayed"] is True
    assert finalized["status"] == "FINALIZADA" and delivered["status"] == "ENTREGUE" and returned["status"] == "RETIRADA"
    assert status_history >= 4
    print(json.dumps({"status": "PASS", "item": entry["item_number"], "os": work["numero_os"], "dependency": dependency_block, "active_before": before, "active_during": active_during, "active_after": active_after, "schedules_vigentes": current_schedules, "programacao_final": str(planned_date), "stage_idempotency": replayed_stage["replayed"], "final_status": delivered["status"], "return_status": returned["status"], "status_history": status_history, "same_chassis_entries": history_count}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
