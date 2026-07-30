import inspect
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy.exc import SQLAlchemyError

import authz
import erp_service
import main


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return iter(self.rows)

    def scalars(self):
        return iter(self.rows)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.params = {}
        self.engine = object()

    def execute(self, statement, params):
        self.sql = str(statement)
        self.params = params
        return _Result(self.rows)


class _FailingBeginEngine:
    def begin(self):
        raise SQLAlchemyError("database unavailable")


class MesSharedAuthRbacTests(unittest.TestCase):
    def test_auth_mode_is_legacy_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(authz.auth_mode(), "legacy")

    def test_invalid_auth_mode_fails_closed(self):
        with patch.dict(os.environ, {"MES_AUTH_MODE": "typo"}, clear=True):
            with self.assertRaises(RuntimeError):
                authz.auth_mode()

    def test_shared_session_database_failure_fails_closed(self):
        request = SimpleNamespace(cookies={authz.SHARED_SESSION_COOKIE: "opaque-token"})
        self.assertIsNone(authz.get_shared_current_user(request, _FailingBeginEngine()))

    def test_explicit_role_matrix(self):
        for role in ("OPERADOR", "COMPRADOR"):
            permissions = authz._default_permissions({role})
            self.assertIn(authz.MES_DASHBOARD_READ, permissions)
            self.assertIn(authz.MES_STAGE_WRITE, permissions)
            self.assertIn(authz.MES_EXPORTS_READ, permissions)
            self.assertNotIn(authz.MES_WORK_ORDERS_MANAGE, permissions)
        for role in ("PCP", "ENGENHARIA"):
            permissions = authz._default_permissions({role})
            self.assertIn(authz.MES_WORK_ORDERS_MANAGE, permissions)
            self.assertIn(authz.MES_FINALIZE, permissions)
            self.assertNotIn(authz.MES_LEGACY_IMPORT, permissions)
            self.assertNotIn(authz.MES_USERS_MANAGE, permissions)
        self.assertEqual(authz._default_permissions({"FINANCEIRO"}), frozenset())

    def test_multiple_roles_union_permissions(self):
        permissions = authz._default_permissions({"OPERADOR", "PCP"})
        self.assertIn(authz.MES_STAGE_WRITE, permissions)
        self.assertIn(authz.MES_WORK_ORDERS_MANAGE, permissions)
        self.assertIn(authz.MES_FINALIZE, permissions)

    def test_rbac_loader_ignores_inactive_roles_without_legacy_fallback(self):
        connection = _Connection([])
        with patch.object(authz, "_table_exists", return_value=True):
            roles = authz._load_roles(connection, {"id": 7, "role": "PCP"})

        self.assertEqual(roles, frozenset())
        self.assertIn("join public.erp_roles", connection.sql.lower())
        self.assertIn("r.active = true", connection.sql.lower())
        self.assertEqual(connection.params, {"user_id": 7})

    def test_rbac_loader_never_uses_legacy_fallback_in_shared_mode(self):
        connection = _Connection([])
        with patch.object(authz, "_table_exists", return_value=False):
            roles = authz._load_roles(connection, {"id": 7, "role": "PCP"})

        self.assertEqual(roles, frozenset())
        self.assertIn("erp_user_roles", connection.sql)

    def test_admin_is_wildcard(self):
        principal = authz.Principal(
            id=1,
            nome="admin",
            username="admin",
            active=True,
            auth_version=1,
            roles=frozenset({"ADMIN"}),
            permissions=frozenset(),
        )
        self.assertTrue(principal.can(authz.MES_USERS_MANAGE))
        self.assertTrue(principal.can("permissao.futura"))

    def test_session_implementation_never_stores_raw_token(self):
        source = inspect.getsource(authz.create_shared_session)
        self.assertIn('"token_hash": _token_hash(token)', source)
        self.assertNotIn('"token": token', source)
        current = inspect.getsource(authz.get_shared_current_user)
        self.assertIn("session_auth_version", current)
        self.assertIn('row["auth_version"]', current)

    def test_human_routes_enforce_permissions(self):
        expected = {
            main.home: "MES_DASHBOARD_READ",
            main.erp_work_order_screen: "MES_WORK_ORDERS_MANAGE",
            main.erp_vehicle_entry: "MES_VEHICLE_ENTRIES_CREATE",
            main.erp_stage: "MES_STAGE_WRITE",
            main.erp_location: "MES_STAGE_WRITE",
            main.erp_finalize: "MES_FINALIZE",
            main.erp_schedule: "MES_SCHEDULE_MANAGE",
            main.exportar_controle_producao: "MES_EXPORTS_READ",
            main.exportar: "MES_EXPORTS_READ",
            main.exportar_tempos: "MES_EXPORTS_READ",
            main.pg_importar: "MES_LEGACY_IMPORT",
            main.usuarios_page: "MES_USERS_MANAGE",
        }
        for endpoint, permission in expected.items():
            with self.subTest(endpoint=endpoint.__name__):
                self.assertIn(permission, inspect.getsource(endpoint))

    def test_complete_route_inventory_is_protected(self):
        public_endpoints = {
            main.healthz,
            main.login_page,
            main.login_post,
            main.logout,
        }
        for route in main.app.routes:
            endpoint = getattr(route, "endpoint", None)
            if not endpoint or getattr(endpoint, "__module__", "") != main.__name__:
                continue
            if endpoint in public_endpoints:
                continue
            source = inspect.getsource(endpoint)
            with self.subTest(path=getattr(route, "path", ""), endpoint=endpoint.__name__):
                if str(getattr(route, "path", "")).startswith("/api/erp/internal/"):
                    self.assertIn("erp_feature_enabled()", source)
                    self.assertIn("erp_backend_actor(request)", source)
                else:
                    self.assertIn("require_login(request, db)", source)
                    self.assertIn("has_permission(", source)

    def test_internal_active_options_remain_service_token_protected(self):
        source = inspect.getsource(main.erp_internal_work_order_options)
        self.assertIn("erp_feature_enabled()", source)
        self.assertIn("erp_backend_actor(request)", source)
        self.assertNotIn("require_login", source)

    def test_history_cleanup_is_post_only(self):
        source = inspect.getsource(main)
        self.assertIn('@app.post("/limpar_historico")', source)
        self.assertNotIn('@app.get("/limpar_historico")', source)
        self.assertIn('@app.post("/logout")', source)
        self.assertNotIn('@app.get("/logout")', source)
        with open("templates/index.html", encoding="utf-8") as template:
            html = template.read()
        self.assertIn('method="post" action="/limpar_historico"', html)
        self.assertNotIn('href="/limpar_historico"', html)
        self.assertIn('method="post" action="/logout"', html)
        self.assertNotIn('href="/logout"', html)

    def test_stage_controls_are_bound_to_stage_write_permission(self):
        with open("templates/detalhes.html", encoding="utf-8") as template:
            html = template.read()
        self.assertIn(
            'current_user.can("mes.stage.write")',
            html,
        )
        self.assertGreaterEqual(
            html.count("{% if not can_point %}disabled{% endif %}"),
            8,
        )

    def test_active_options_are_compact_filtered_and_limited(self):
        connection = _Connection([{
            "work_order_id": "00000000-0000-0000-0000-000000000001",
            "numero_os": "3112",
            "item_number": 3112,
            "chassi": "9V7VPFC38TA004249",
            "chassi_exibicao": "TA004249",
            "cliente": "CLIENTE",
            "veiculo": "CITROEN JUMPY",
        }])
        options = erp_service.active_work_order_options(connection, "TA00", 999)
        self.assertEqual(options[0]["label"], "O.S. 3112 · TA004249 · CLIENTE")
        self.assertEqual(connection.params["limit"], 100)
        self.assertIn("EM_PRODUÇÃO", connection.sql)
        self.assertIn("technical_status", connection.sql)
        self.assertIn("w.numero_os", connection.sql)
        self.assertIn("e.item_number", connection.sql)
        self.assertIn("v.chassi", connection.sql)
        self.assertIn("right(v.chassi,8)", connection.sql)


if __name__ == "__main__":
    unittest.main()
