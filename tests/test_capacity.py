from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from src.capacity import (
    CellMass,
    calculate_capacity_row,
    cycle_aliases,
    load_cell_masses,
    select_discharge_endpoint,
)


class CapacityTests(unittest.TestCase):
    def test_reference_workbook_contains_65_unique_positive_masses(self) -> None:
        workbook = (
            Path(__file__).resolve().parents[1]
            / "template"
            / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx"
        )
        masses = load_cell_masses(workbook)

        self.assertEqual(len(masses), 65)
        self.assertEqual(len({item.excel_cell_id for item in masses}), 65)
        self.assertTrue(all(item.active_mass_g > 0 for item in masses))
        first = masses[0]
        self.assertEqual(first.active_mass_g, Decimal("0.030111065519999997"))
        self.assertAlmostEqual(
            float(first.formation_capacity_mah / first.active_mass_g),
            float(first.excel_specific_capacity_mah_g),
            places=10,
        )

    def test_discharge_endpoint_uses_largest_negative_discharge(self) -> None:
        rows = [
            {"Command": "Charge", "I[A]": "0.01", "Ah-Step": "0.010"},
            {"Command": "Discharge", "I[A]": "-0.01", "Ah-Step": "-0.003"},
            {"Command": "Discharge", "I[A]": "-0.01", "Ah-Step": "-0.005"},
            {"Command": "Discharge", "I[A]": "0.01", "Ah-Step": "-0.020"},
        ]

        self.assertIs(select_discharge_endpoint(rows), rows[2])

    def test_capacity_is_normalized_by_cathode_active_mass(self) -> None:
        mass = CellMass("A", "cell", "CR2032", Decimal("0.025"), None, None, 2)
        endpoint = {
            "Ah-Step": "-0.005",
            "Cycle-Type": "C/2",
            "Command": "Discharge",
            "I[A]": "-0.01",
        }

        result = calculate_capacity_row(endpoint, mass, "cell")

        self.assertEqual(result["Discharge-Capacity[mAh]"], "5.000")
        self.assertEqual(result["Specific-Discharge-Capacity[mAh/g]"], "200")
        self.assertEqual(result["Capacity-Status"], "valid")

    def test_invalid_cycle_classification_is_not_accepted_as_capacity(self) -> None:
        mass = CellMass("A", "cell", "CR2032", Decimal("0.025"), None, None, 2)
        endpoint = {
            "Ah-Step": "-0.005",
            "Cycle-Type": "C/2",
            "Command": "Discharge",
            "I[A]": "-0.01",
            "Classification-Status": "invalid",
        }

        result = calculate_capacity_row(endpoint, mass, "cell")

        self.assertEqual(result["Capacity-Status"], "invalid")
        self.assertIn("Zyklusklassifikation", result["Capacity-Note"])

    def test_35_degree_excel_ids_map_to_raw_filename_without_ma(self) -> None:
        excel_id = "df_MH_MA_VZ_3,8_10µm_48h_35°C_1"

        self.assertIn("df_MH_VZ_3,8_10µm_48h_35°C_1", cycle_aliases(excel_id))


if __name__ == "__main__":
    unittest.main()
