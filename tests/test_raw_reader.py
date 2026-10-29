''' Diese Datei testet auf:
    deutsches Zahlenformat, korrekte Anordnung der Zyklen, 
    Semikolon-getrennte CSV-Dateien, Windows-1252-Encoding,
    Erkennung von ~Time[h], Bevorzugung von _retry_komplett (falls vorhanden),
    Ignorieren von _state2 -Dateien, Erkennung von Zeitstempeln,
'''

import csv
import gzip
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from src.raw_reader import canonical_cell_id, normalize_number, run


HEADER = "DateTime\t#Time[h]\tCommand\tU[V]\tI[A]\tAh[Ah]\tAh-Step\tCyc-Count\tState\n"


class RawReaderTests(unittest.TestCase):
    @staticmethod
    def read_output(path):
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle, delimiter="\t"))

    def test_number_and_cell_name_normalization(self):
        self.assertEqual(normalize_number("1.234,56"), "1234.56")
        self.assertEqual(
            canonical_cell_id(Path("cell_1_Lebensdauer_Langzeit.txt")), "cell_1"
        )
        self.assertEqual(
            canonical_cell_id(Path("cell_1_Lebensdauertest.csv")), "cell_1"
        )
        self.assertEqual(
            canonical_cell_id(Path("cell_1_Lebensdauer_Lebensdauer.txt")), "cell_1"
        )

    def test_streaming_filter_grouping_and_time_order(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw = root / "raw"
            output = root / "output"
            raw.mkdir()
            (raw / "cell_1_Lebensdauer.txt").write_text(
                HEADER
                + "02.01.2026 10:00:00\t2,0\tCharge\t4,1\t1,0\t2,0\t1,0\t2\t2\n"
                + "02.01.2026 10:00:01\t2,1\tCharge\t4,1\t1,0\t2,0\t1,0\t2\t1\n"
                + "unvollstaendig\n",
                encoding="utf-8",
            )
            (raw / "cell_1_Formierung.txt").write_text(
                HEADER
                + "01.01.2026 10:00:00\t1,0\tCharge\t4,0\t1,0\t1,0\t1,0\t1\t2\n",
                encoding="utf-8",
            )
            report = run(raw, output)
            destination = output / "cell_1_state2.tsv.gz"
            rows = self.read_output(destination)

            self.assertEqual([row["Cyc-Count"] for row in rows], ["1", "2"])
            self.assertEqual(rows[1]["U[V]"], "4.1")
            self.assertEqual(sum(item.malformed_rows for item in report.files), 1)

    def test_semicolon_csv_decimal_comma_and_cp1252_encoding(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw = root / "raw"
            output = root / "output"
            raw.mkdir()
            content = (
                "DateTime;~Time[h];Command;U[V];I[A];Ah[Ah];Ah-Step;"
                "T1[°C];Cyc-Count;State\n"
                "03.01.2026 12:30:00;1,5;Charge;4,25;0,50;0,75;0,25;"
                "24,8;3;2\n"
            )
            (raw / "cell_2_Lebensdauer.csv").write_bytes(content.encode("cp1252"))

            run(raw, output)
            rows = self.read_output(output / "cell_2_state2.tsv.gz")

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Time[h]"], "1.5")
            self.assertEqual(rows[0]["U[V]"], "4.25")
            self.assertEqual(rows[0]["T1[°C]"], "24.8")

    def test_complete_retry_replaces_original_and_state2_derivative_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw = root / "raw"
            output = root / "output"
            raw.mkdir()
            original = raw / "cell_3_Lebensdauer.txt"
            retry = raw / "cell_3_Lebensdauer_retry_komplett.txt"
            derivative = raw / "cell_3_Lebensdauer_state2.txt"
            original.write_text(
                HEADER + "01.01.2026 10:00:00\t1\tCharge\t3\t1\t1\t1\t1\t2\n",
                encoding="utf-8",
            )
            retry.write_text(
                HEADER + "01.01.2026 10:01:00\t2\tCharge\t4\t1\t1\t1\t2\t2\n",
                encoding="utf-8",
            )
            derivative.write_text(
                HEADER + "01.01.2026 10:02:00\t3\tCharge\t5\t1\t1\t1\t3\t2\n",
                encoding="utf-8",
            )

            report = run(raw, output)
            rows = self.read_output(output / "cell_3_state2.tsv.gz")

            self.assertEqual([row["Cyc-Count"] for row in rows], ["2"])
            self.assertEqual(report.discovered_files, 3)
            self.assertEqual(report.selected_files, 1)
            self.assertEqual(report.superseded_files, [str(original)])
            self.assertEqual(report.ignored_derived_files, [str(derivative)])

    def test_invalid_state2_timestamp_is_reported_and_skipped(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw = root / "raw"
            output = root / "output"
            raw.mkdir()
            (raw / "cell_4_Lebensdauer.txt").write_text(
                HEADER
                + "kein Zeitstempel\t1\tCharge\t4\t1\t1\t1\t1\t2\n"
                + "04.01.2026 10:00:00\t2\tCharge\t4\t1\t1\t1\t2\t2\n",
                encoding="utf-8",
            )

            report = run(raw, output)
            rows = self.read_output(output / "cell_4_state2.tsv.gz")

            self.assertEqual([row["Cyc-Count"] for row in rows], ["2"])
            self.assertEqual(report.files[0].invalid_datetime_rows, 1)
            self.assertIn("DateTime ist ungueltig", report.files[0].examples[0])

    def test_larger_state2_file_recovers_truncated_primary_export(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw = root / "raw"
            output = root / "output"
            raw.mkdir()
            primary = raw / "cell_5_Lebensdauer.txt"
            fallback = raw / "cell_5_Lebensdauer_state2.txt"
            primary.write_text(HEADER, encoding="utf-8")
            fallback.write_text(
                HEADER + "05.01.2026 10:00:00\t1\tCharge\t4\t1\t1\t1\t6\t2\n",
                encoding="utf-8",
            )

            report = run(raw, output)
            rows = self.read_output(output / "cell_5_state2.tsv.gz")

            self.assertEqual([row["Cyc-Count"] for row in rows], ["6"])
            self.assertEqual(report.recovered_state2_files, [str(fallback)])
            self.assertEqual(report.superseded_files, [str(primary)])
            self.assertEqual(report.ignored_derived_files, [])

    def test_cell_scope_excludes_files_before_processing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw = root / "raw"
            output = root / "output"
            raw.mkdir()
            for cell in ("keep", "skip"):
                (raw / f"{cell}_Lebensdauer.txt").write_text(
                    HEADER + "05.01.2026 10:00:00\t1\tCharge\t4\t1\t1\t1\t6\t2\n",
                    encoding="utf-8",
                )
            scope = SimpleNamespace(
                policy=lambda cell_id: SimpleNamespace(enabled=cell_id == "keep")
            )

            report = run(raw, output, cell_scope=scope)

            self.assertEqual(report.selected_files, 1)
            self.assertEqual(len(report.excluded_files), 1)
            self.assertTrue((output / "keep_state2.tsv.gz").is_file())
            self.assertFalse((output / "skip_state2.tsv.gz").exists())


if __name__ == "__main__":
    unittest.main()
