import asyncio
from contextlib import contextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from sqlalchemy.exc import OperationalError

import main
from erp_stock_closure import NegativeStockConfirmationRequired


class TechnicalCloseApiTests(unittest.TestCase):
    def test_conflict_leaves_transaction_before_returning_409(self):
        events = []
        @contextmanager
        def begin():
            try:
                yield object()
                events.append("commit")
            except Exception:
                events.append("rollback")
                raise
        error = NegativeStockConfirmationRequired([
            {"codigo": "MP", "saldo_atual": "0", "quantidade_baixar": "2", "saldo_final": "-2"}
        ], "test-token")
        request = SimpleNamespace(headers={"X-ERP-Actor-ID": "7"})
        with (patch.object(main, "erp_feature_enabled", return_value=True),
              patch.object(main, "erp_backend_actor", return_value="pcp"),
              patch.object(main.database, "engine", SimpleNamespace(begin=begin)),
              patch.object(main.erp_service, "technical_close_work_order", side_effect=error) as close):
            response = asyncio.run(main.erp_internal_technical_close("work-id", request, {}))
        self.assertEqual(["rollback"], events)
        self.assertEqual(409, response.status_code)
        self.assertEqual("test-token", json.loads(response.body)["confirmation_token"])
        self.assertEqual("7", close.call_args.kwargs["actor_user_id"])

    def test_invalid_internal_token_never_touches_stock(self):
        with (patch.object(main, "erp_feature_enabled", return_value=True),
              patch.object(main, "erp_backend_actor", return_value=None),
              patch.object(main.erp_service, "technical_close_work_order") as close):
            response = asyncio.run(main.erp_internal_technical_close("work-id", SimpleNamespace(headers={}), {}))
        self.assertEqual(401, response.status_code)
        close.assert_not_called()

    def test_concurrent_conflict_is_reported_only_after_rollback(self):
        events = []
        @contextmanager
        def begin():
            try:
                yield object()
            except Exception:
                events.append("rollback")
                raise
        original = RuntimeError("deadlock")
        original.sqlstate = "40P01"
        failure = OperationalError("test", {}, original)
        with (patch.object(main, "erp_feature_enabled", return_value=True),
              patch.object(main, "erp_backend_actor", return_value="pcp"),
              patch.object(main.database, "engine", SimpleNamespace(begin=begin)),
              patch.object(main.erp_service, "technical_close_work_order", side_effect=failure)):
            response = asyncio.run(main.erp_internal_technical_close("work-id", SimpleNamespace(headers={}), {}))
        self.assertEqual(["rollback"], events)
        self.assertEqual(409, response.status_code)
        self.assertEqual("STOCK_OPERATION_CONFLICT", json.loads(response.body)["code"])


if __name__ == "__main__":
    unittest.main()
