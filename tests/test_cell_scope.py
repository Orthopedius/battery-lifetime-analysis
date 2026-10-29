from __future__ import annotations

import unittest
from pathlib import Path

from src.cell_scope import load_cell_scope


class CellScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.scope = load_cell_scope(
            root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx",
            root / "config" / "cells.csv",
        )

    def test_excel_target_and_35_degree_alias_are_enabled(self) -> None:
        policy = self.scope.policy("df_MH_VZ_3,8_10µm_48h_35°C_1")

        self.assertTrue(policy.enabled)
        self.assertTrue(policy.include_validation)
        self.assertEqual(policy.excel_cell_id, "df_MH_MA_VZ_3,8_10µm_48h_35°C_1")

    def test_historical_and_sio_cells_are_excluded(self) -> None:
        self.assertFalse(self.scope.policy("df_GB_MA_VZ_2,68_PreLi1_Vorversuchsreihe").enabled)
        self.assertFalse(self.scope.policy("df_MH_MA_VV_VZ_SiO_Ref_1").include_validation)

    def test_known_short_series_does_not_expect_longterm(self) -> None:
        policy = self.scope.policy("df_MH_MA_VZ_3,8_5µm_24h_4")

        self.assertTrue(policy.enabled)
        self.assertTrue(policy.include_validation)
        self.assertFalse(policy.expect_longterm)


if __name__ == "__main__":
    unittest.main()
