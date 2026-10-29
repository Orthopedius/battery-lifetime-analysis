from __future__ import annotations

import unittest
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

from src.capacity import CellMass
from src.excel_export import (
    MAIN_NS,
    NO_LONGTERM_CELL,
    CapacityPoint,
    build_sheet_xml,
    column_name,
    summarize,
    write_workbook,
)


class ExcelExportTests(unittest.TestCase):
    def test_column_names_cross_excel_boundaries(self) -> None:
        self.assertEqual(column_name(1), "A")
        self.assertEqual(column_name(26), "Z")
        self.assertEqual(column_name(27), "AA")
        self.assertEqual(column_name(52), "AZ")

    def test_summary_uses_cell_specific_baselines_and_sample_std(self) -> None:
        points = {
            "a": {"C/2": [CapacityPoint(6, 200, 0.005), CapacityPoint(7, 180, 0.0045)]},
            "b": {"C/2": [CapacityPoint(6, 100, 0.0025), CapacityPoint(7, 80, 0.002)]},
        }
        result = summarize(["a", "b"], "C/2", points)

        self.assertEqual([item.cycle for item in result], [6, 7])
        self.assertEqual(result[0].mean_capacity, 150)
        self.assertAlmostEqual(result[0].std_capacity or 0, 70.71067811865476)
        self.assertEqual(result[0].mean_soh, 100)
        self.assertEqual(result[1].mean_soh, 85)
        self.assertAlmostEqual(result[1].std_soh or 0, 7.0710678118654755)

    def test_no_longterm_cell_is_labelled_and_has_no_time_axis(self) -> None:
        mass = CellMass("CR2032 5µm 24h", NO_LONGTERM_CELL, "CR2032",
                        Decimal("0.025"), None, None, 2)
        xml, _, _ = build_sheet_xml("5um_24h", mass.group, [mass], {})
        root = ET.fromstring(xml)
        texts = [node.text or "" for node in root.findall(f".//{{{MAIN_NS}}}t")]

        self.assertTrue(any("keine Langzeitdaten" in text for text in texts))
        self.assertFalse(any("Zeit" in text or "DateTime" in text for text in texts))

    def test_workbook_has_clean_namespaces_and_current_sheet_metadata(self) -> None:
        mass = CellMass("CR2032 5µm 24h", NO_LONGTERM_CELL, "CR2032",
                        Decimal("0.025"), None, None, 2)
        sheet_xml, _, _ = build_sheet_xml("5um_24h", mass.group, [mass], {})
        template = (
            Path(__file__).resolve().parents[1]
            / "template" / "Zelldaten - Vollzellen - Zeit.xlsx"
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "test.xlsx"
            sheets = [("Test_A", sheet_xml), ("Test_B", sheet_xml)]
            write_workbook(template, destination, sheets)
            with zipfile.ZipFile(destination) as archive:
                workbook_raw = archive.read("xl/workbook.xml").decode("utf-8")
                workbook = ET.fromstring(workbook_raw)
                app = ET.fromstring(archive.read("docProps/app.xml"))

                self.assertNotIn("Ignorable", workbook_raw)
                self.assertNotIn("AlternateContent", workbook_raw)
                self.assertEqual(
                    len(workbook.findall(f".//{{{MAIN_NS}}}sheet")), 2
                )
                counts = [node.text for node in app.iter() if node.tag.endswith("}i4")]
                titles = [
                    node.text for node in app.iter()
                    if node.tag.endswith("}lpstr") and node.text in {"Test_A", "Test_B"}
                ]
                self.assertEqual(counts, ["2"])
                self.assertEqual(titles, ["Test_A", "Test_B"])
                self.assertNotIn("xl/sharedStrings.xml", archive.namelist())


if __name__ == "__main__":
    unittest.main()
