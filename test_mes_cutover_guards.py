import ast
from pathlib import Path
import unittest


SOURCE_PATH = Path(__file__).with_name("main.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def function_node(name):
    return next(
        node
        for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def function_source(name):
    node = function_node(name)
    return ast.get_source_segment(SOURCE, node) or ""


class MesCutoverGuardTests(unittest.TestCase):
    def test_internal_endpoints_honor_erp_feature_flag(self):
        endpoint_names = (
            "erp_internal_catalogs",
            "erp_internal_work_orders",
            "erp_internal_work_order_detail",
            "erp_internal_vehicle_entry",
            "erp_internal_work_order",
            "erp_internal_update_work_order",
            "erp_internal_activate",
            "erp_internal_technical_close",
            "erp_internal_technical_reopen",
            "erp_internal_schedule",
        )

        for name in endpoint_names:
            with self.subTest(endpoint=name):
                source = function_source(name)
                self.assertIn("if not erp_feature_enabled()", source)
                self.assertLess(
                    source.index("if not erp_feature_enabled()"),
                    source.index("erp_backend_actor(request)"),
                )

    def test_dashboard_can_run_without_legacy_operational_tables(self):
        for name in (
            "listar_semanas_producao",
            "carregar_veiculos_dashboard",
            "home",
            "detalhes",
        ):
            with self.subTest(function=name):
                self.assertIn(
                    "legacy_operational_schema_available()",
                    function_source(name),
                )

    def test_legacy_uploads_have_an_explicit_cutover_flag(self):
        self.assertIn("ERP_MES_LEGACY_UPLOAD_ENABLED", SOURCE)
        for name in ("upload_base", "upload_apontamentos", "limpar_logs", "pg_importar"):
            with self.subTest(function=name):
                self.assertIn("legacy_upload_enabled()", function_source(name))

    def test_legacy_upload_is_fail_closed_by_default(self):
        source = function_source("legacy_upload_enabled")
        self.assertIn('"ERP_MES_LEGACY_UPLOAD_ENABLED", "false"', source)


if __name__ == "__main__":
    unittest.main()
