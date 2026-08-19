import copy
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import erp_service


WORK_ID = "00000000-0000-0000-0000-000000000111"
OTHER_WORK_ID = "00000000-0000-0000-0000-000000000222"
ENTRY_ID = "entry-3113"
FULL_CHASSIS = "9BWZZZ377VT004251"


class FakeRow:
    def __init__(self, value):
        self._mapping = value


class FakeResult:
    def __init__(self, row=None, rowcount=0):
        self._row = FakeRow(row) if row is not None else None
        self.rowcount = rowcount

    def first(self):
        return self._row


class StateConnection:
    """Small transactional double for the SQL touched by this contract."""

    def __init__(
        self,
        *,
        document_chassis=FULL_CHASSIS[-8:],
        document_link=None,
        document_status="emitido",
        document_type="os",
        document_number="JI - 3113",
    ):
        self.entries = {
            ENTRY_ID: {
                "item_number": 3113, "data_chegada": None, "vehicle_id": "vehicle-1",
                "status": "AGUARDANDO_O_S", "cliente_nome": "Cliente da entrada",
            }
        }
        self.vehicles = {"vehicle-1": {"chassi": FULL_CHASSIS}}
        self.work_orders = {}
        self.documents = {
            42: {
                "id": 42,
                "tipo": document_type,
                "numero": document_number,
                "status": document_status,
                "dados": {"chassis": document_chassis},
                "erp_work_order_id": document_link,
            }
        }
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split()).lower()
        params = params or {}
        self.calls.append((sql, dict(params)))

        if "from erp_vehicle_entries where id=:id for update" in sql:
            entry = self.entries.get(params["id"])
            return FakeResult(
                {
                    "item_number": entry["item_number"], "data_chegada": entry["data_chegada"],
                    "status": entry["status"], "cliente_nome": entry["cliente_nome"],
                }
                if entry else None
            )
        if "select id,numero_os from erp_work_orders where vehicle_entry_id=:id" in sql:
            work = next(
                (item for item in self.work_orders.values() if item["vehicle_entry_id"] == params["id"]),
                None,
            )
            return FakeResult(
                {"id": work["id"], "numero_os": work["numero_os"]} if work else None
            )
        if sql.startswith("insert into erp_work_orders"):
            self.work_orders[params["id"]] = {
                **params,
                "vehicle_entry_id": params["entry"],
                "numero_os": params["number"],
                "status": "RASCUNHO",
                "version": 1,
            }
            return FakeResult(rowcount=1)
        if "select w.*,e.data_chegada,e.cliente_nome as entry_client from erp_work_orders w" in sql:
            work = self.work_orders.get(params["id"])
            if not work:
                return FakeResult()
            return FakeResult({
                **work, "data_chegada": None,
                "entry_client": self.entries[work["vehicle_entry_id"]]["cliente_nome"],
            })
        if sql.startswith("update erp_work_orders set"):
            work = self.work_orders[params["id"]]
            for field in erp_service.WORK_ORDER_FIELDS:
                work[field] = params[field]
            work["version"] += 1
            return FakeResult(rowcount=1)
        if "select v.chassi from erp_vehicle_entries e" in sql:
            entry = self.entries.get(params["entry_id"])
            vehicle = self.vehicles.get(entry["vehicle_id"]) if entry else None
            return FakeResult(vehicle)
        if "from public.suprimentos_documentos" in sql and "where id<>:document_id" in sql:
            for document in self.documents.values():
                if document["id"] == params["document_id"]:
                    continue
                json_link = (document.get("dados") or {}).get("erp_work_order_id")
                if document.get("erp_work_order_id") == params["work_id"] or json_link == params["work_id"]:
                    return FakeResult({"id": document["id"], "numero": document["numero"]})
            return FakeResult()
        if "from public.suprimentos_documentos" in sql and "where id=:document_id" in sql:
            document = self.documents.get(params["document_id"])
            return FakeResult(copy.deepcopy(document) if document else None)
        if sql.startswith("update public.suprimentos_documentos"):
            document = self.documents.get(params["document_id"])
            if not document or document.get("erp_work_order_id") not in (None, params["work_id"]):
                return FakeResult(rowcount=0)
            document["erp_work_order_id"] = params["work_id"]
            document.setdefault("dados", {})["erp_work_order_id"] = params["work_id"]
            return FakeResult(rowcount=1)
        return FakeResult(rowcount=1)


class FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def begin(self):
        snapshot = copy.deepcopy(
            (self.conn.entries, self.conn.vehicles, self.conn.work_orders, self.conn.documents)
        )
        try:
            yield self.conn
        except Exception:
            (
                self.conn.entries,
                self.conn.vehicles,
                self.conn.work_orders,
                self.conn.documents,
            ) = snapshot
            raise


class MesDocumentWorkOrderLinkTests(unittest.TestCase):
    @patch("erp_service._ensure_stage_rows")
    @patch("erp_service._id", return_value=WORK_ID)
    def test_create_links_document_and_work_order_atomically(self, _new_id, _stages):
        conn = StateConnection()
        engine = FakeEngine(conn)

        with engine.begin() as tx:
            result = erp_service.create_work_order(
                tx,
                ENTRY_ID,
                {"numero_os": "O.S. 3113", "documento_os_id": "42"},
                "PCP",
            )

        self.assertEqual(result["documento_os_id"], 42)
        self.assertIn(WORK_ID, conn.work_orders)
        self.assertEqual(conn.documents[42]["erp_work_order_id"], WORK_ID)
        self.assertEqual(conn.documents[42]["dados"]["erp_work_order_id"], WORK_ID)

    def test_rejects_document_already_linked_to_another_work_order(self):
        conn = StateConnection(document_link=OTHER_WORK_ID)

        with self.assertRaisesRegex(ValueError, "outra O.S. operacional"):
            erp_service._link_suprimentos_os_document(
                conn, 42, WORK_ID, "3113", ENTRY_ID, "PCP"
            )

        self.assertEqual(conn.documents[42]["erp_work_order_id"], OTHER_WORK_ID)

    def test_rejects_concluded_document_before_linking(self):
        conn = StateConnection(document_status="concluido")

        with self.assertRaisesRegex(ValueError, "rascunho ou emitido"):
            erp_service._link_suprimentos_os_document(
                conn, 42, WORK_ID, "3113", ENTRY_ID, "PCP"
            )

        self.assertIsNone(conn.documents[42]["erp_work_order_id"])

    def test_rejects_document_with_incompatible_number(self):
        conn = StateConnection(document_number="O.S. 9999")

        with self.assertRaisesRegex(ValueError, "nao corresponde"):
            erp_service._link_suprimentos_os_document(
                conn, 42, WORK_ID, "3113", ENTRY_ID, "PCP"
            )

        self.assertIsNone(conn.documents[42]["erp_work_order_id"])

    @patch("erp_service._ensure_stage_rows")
    @patch("erp_service._id", return_value=WORK_ID)
    def test_create_without_document_keeps_legacy_contract(self, _new_id, _stages):
        conn = StateConnection()

        result = erp_service.create_work_order(
            conn, ENTRY_ID, {"numero_os": "3113"}, "PCP"
        )

        self.assertEqual(result["id"], WORK_ID)
        self.assertNotIn("documento_os_id", result)
        self.assertIsNone(conn.documents[42]["erp_work_order_id"])

    @patch("erp_service._ensure_stage_rows")
    @patch("erp_service._id", return_value=WORK_ID)
    def test_create_rolls_back_when_document_validation_fails(self, _new_id, _stages):
        conn = StateConnection(document_chassis="CHASSI99")
        engine = FakeEngine(conn)

        with self.assertRaisesRegex(ValueError, "chassi do documento"):
            with engine.begin() as tx:
                erp_service.create_work_order(
                    tx,
                    ENTRY_ID,
                    {"numero_os": "3113", "documento_os_id": 42},
                    "PCP",
                )

        self.assertEqual(conn.work_orders, {})
        self.assertIsNone(conn.documents[42]["erp_work_order_id"])
        self.assertNotIn("erp_work_order_id", conn.documents[42]["dados"])

    def test_update_can_fill_the_optional_document_link(self):
        conn = StateConnection()
        conn.work_orders[WORK_ID] = {
            "id": WORK_ID,
            "vehicle_entry_id": ENTRY_ID,
            "numero_os": "3113",
            "status": "RASCUNHO",
            "version": 1,
            **{field: None for field in erp_service.WORK_ORDER_FIELDS},
        }

        result = erp_service.update_work_order(
            conn,
            WORK_ID,
            {"cliente_nome": "Cliente divergente", "documento_os_id": 42},
            "PCP",
        )

        self.assertEqual(result["documento_os_id"], 42)
        self.assertEqual(conn.work_orders[WORK_ID]["cliente_nome"], "Cliente da entrada")
        self.assertEqual(conn.documents[42]["erp_work_order_id"], WORK_ID)


if __name__ == "__main__":
    unittest.main()
