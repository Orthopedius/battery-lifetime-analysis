"""Durchgaengiger Kommandozeilen-Einstieg fuer die Lebensdauer-Auswertung.

Standardmaessig laeuft die komplette Kette Rohdaten -> State 2 -> echte
Protokollzyklen -> Kapazitaet -> Validierung -> Excel. Fuer wiederholte
Exporte kann ``--skip-raw`` genutzt werden; die Rohdaten selbst werden nie
veraendert.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

from src.capacity import run as run_capacity
from src.cell_scope import load_cell_scope
from src.cycle_reconstruction import run as run_cycles
from src.excel_export import run as run_excel
from src.raw_reader import run as run_raw_reader
from src.validation import validate_pipeline, write_reports


LOGGER = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-raw", action="store_true",
                        help="Vorhandene State-2-Dateien verwenden")
    parser.add_argument("--raw-dir", type=Path, default=root / "data" / "raw")
    parser.add_argument("--state2-dir", type=Path, default=root / "data" / "processed" / "state2")
    parser.add_argument("--cycles-dir", type=Path, default=root / "data" / "processed" / "cycles")
    parser.add_argument("--capacity-dir", type=Path, default=root / "data" / "processed" / "capacity")
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "output")
    parser.add_argument("--cells-config", type=Path, default=root / "config" / "cells.csv")
    parser.add_argument("--mass-workbook", type=Path,
                        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx")
    parser.add_argument("--excel-template", type=Path,
                        default=root / "template" / "Zelldaten - Vollzellen - Zeit.xlsx")
    parser.add_argument("--excel-output", type=Path,
                        default=root / "data" / "output" / "Zelldaten_Lebensdauer_Auswertung.xlsx")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    capacity_file = args.capacity_dir / "capacity_by_cycle.tsv.gz"
    capacity_validation = args.capacity_dir / "capacity_validation.csv"
    raw_report = args.state2_dir / "raw_reader_report.json"
    try:
        scope = load_cell_scope(args.mass_workbook, args.cells_config)
        if not args.skip_raw:
            LOGGER.info("1/5 Rohdaten streamend einlesen und auf State 2 filtern")
            run_raw_reader(args.raw_dir, args.state2_dir, raw_report, scope)
        else:
            LOGGER.info("1/5 Rohdatenimport uebersprungen; vorhandene State-2-Dateien werden verwendet")

        LOGGER.info("2/5 Teilprogrammzaehler zu Protokollzyklen rekonstruieren")
        cycle_result = run_cycles(args.state2_dir, args.cycles_dir, scope)
        invalid_cycles = sum(item["invalid"] for item in cycle_result.values())

        LOGGER.info("3/5 Gueltige Kapazitaeten mit Kathodenaktivmasse berechnen")
        capacity_result = run_capacity(args.cycles_dir, args.mass_workbook, args.capacity_dir)

        LOGGER.info("4/5 Pipeline pruefen und lesbaren Bericht schreiben")
        validation = validate_pipeline(
            args.state2_dir, args.cycles_dir, capacity_file, capacity_validation,
            args.mass_workbook, raw_report, args.cells_config,
        )
        write_reports(validation, args.output_dir)

        LOGGER.info("5/5 Excel-Arbeitsmappe ohne Zeitachse erzeugen")
        excel_result = run_excel(
            capacity_file, args.mass_workbook, args.excel_template, args.excel_output,
            args.output_dir / "excel_export_report.txt",
        )
    except (OSError, ValueError) as error:
        LOGGER.error("Auswertung abgebrochen: %s", error)
        return 1

    LOGGER.info(
        "Fertig: %d Zielzellen, %d Excel-Blaetter, %d gueltige Kapazitaeten; "
        "%d ungueltige Zyklusabschnitte sind nur im Validierungsbericht enthalten.",
        capacity_result["matched_cells"], excel_result["sheets"],
        capacity_result["exported_cycles"], invalid_cycles,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
