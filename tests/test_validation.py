from __future__ import annotations

import csv
import gzip
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from src.capacity import CellMass
from src.validation import (
    CAPACITY_REQUIRED_COLUMNS,
    CycleRecord,
    Findings,
    scan_capacity_file,
    scan_state2_file,
)


class ValidationTests(unittest.TestCase):
    def test_state2_scan_detects_wrong_state_and_datetime(self) -> None:
        fieldnames = [
            "DateTime", "Line", "Command", "I[A]", "Ah-Step", "Cyc-Count",
            "State", "SourceFile", "SourceRow",
        ]
        row = {
            "DateTime": "kein Datum", "Line": "19", "Command": "Discharge",
            "I[A]": "-0.01", "Ah-Step": "-0.005", "Cyc-Count": "2",
            "State": "3", "SourceFile": "raw.txt", "SourceRow": "10",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cell_state2.tsv.gz"
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
                writer.writeheader()
                writer.writerow(row)
            findings = Findings()

            audit = scan_state2_file(path, findings)

        self.assertEqual(audit.rows, 1)
        codes = {item.code for item in findings.items()}
        self.assertIn("STATE_NOT_2", codes)
        self.assertIn("STATE2_BAD_DATETIME", codes)

    def test_capacity_scan_recomputes_values_and_detects_duplicate_key(self) -> None:
        mass = CellMass("A", "cell", "CR2032", Decimal("0.025"), None, None, 2)
        row = {column: "" for column in CAPACITY_REQUIRED_COLUMNS}
        row.update({
            "Excel-Cell-ID": "cell",
            "Cycle-Cell-ID": "cell",
            "Cathode-Active-Mass[g]": "0.025",
            "Counter-Segment": "1",
            "BaSyTec-Cycle": "6",
            "Cycle-Type": "C/2",
            "Lifetime-Cycle": "1",
            "Discharge-Capacity[Ah]": "0.005",
            "Discharge-Capacity[mAh]": "4",
            "Specific-Discharge-Capacity[mAh/g]": "100",
            "Classification-Status": "valid",
            "Capacity-Status": "valid",
            "Capacity-Note": "",
        })
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capacity.tsv.gz"
            fieldnames = sorted(CAPACITY_REQUIRED_COLUMNS)
            with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter="\t")
                writer.writeheader()
                writer.writerow(row)
                writer.writerow(row)
            findings = Findings()

            rows, _ = scan_capacity_file(
                path, [mass], {("cell", "1", "6"): CycleRecord("C/2", "valid")},
                findings,
            )

        self.assertEqual(rows, 2)
        codes = {item.code for item in findings.items()}
        self.assertIn("CAPACITY_DUPLICATE_KEY", codes)
        self.assertIn("CAPACITY_UNIT_MISMATCH", codes)
        self.assertIn("CAPACITY_SPECIFIC_MISMATCH", codes)


if __name__ == "__main__":
    unittest.main()
