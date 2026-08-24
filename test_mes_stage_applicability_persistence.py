import unittest

import erp_service


class StageApplicabilityPersistenceTests(unittest.TestCase):
    def test_completed_configuration_is_never_recalculated_from_os_fields(self):
        work = {"stage_configuration_status": "CONCLUIDA"}
        stage = {"parametrizado": True, "status": "NÃO_APLICÁVEL"}

        self.assertFalse(
            erp_service._can_recalculate_stage_applicability(work, stage)
        )

    def test_explicit_stage_choice_is_preserved_while_other_stages_are_pending(self):
        work = {"stage_configuration_status": "PENDENTE"}
        stage = {"parametrizado": True, "status": "NÃO_APLICÁVEL"}

        self.assertFalse(
            erp_service._can_recalculate_stage_applicability(work, stage)
        )

    def test_unparameterized_stage_can_follow_os_fields_during_initial_setup(self):
        work = {"stage_configuration_status": "PENDENTE"}
        stage = {"parametrizado": False, "status": "PENDENTE"}

        self.assertTrue(
            erp_service._can_recalculate_stage_applicability(work, stage)
        )


if __name__ == "__main__":
    unittest.main()
