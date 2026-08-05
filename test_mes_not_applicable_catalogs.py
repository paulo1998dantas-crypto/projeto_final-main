import unittest

from erp_catalogs import payload
from erp_service import _stage_applicable


class MesNotApplicableCatalogsTests(unittest.TestCase):
    def test_catalogs_expose_not_applicable_for_controlled_os_fields(self):
        catalogs = payload()
        for field in (
            "vendedores", "mercados", "tipos_veiculo", "linhas",
            "ar_fornecedores", "ar_tipos", "sim_nao",
        ):
            self.assertIn("N/A", catalogs[field], field)
        self.assertIn(
            {"codigo": "N/A", "descricao": "N/A"},
            catalogs["transformacoes"],
        )

    def test_not_applicable_disables_related_production_stage(self):
        self.assertFalse(_stage_applicable("A/C", {"ar_condicionado": "N/A"}))
        self.assertFalse(_stage_applicable("A/C", {"tipo_sistema_ar": "N/A"}))
        self.assertFalse(_stage_applicable("BCO", {"conjunto_bancos": "N/A"}))
        self.assertFalse(_stage_applicable("ACESSÓRIO", {"acessorio": "N/A"}))
        self.assertFalse(_stage_applicable("PLOTAGEM", {"plotagem": "N/A"}))


if __name__ == "__main__":
    unittest.main()
