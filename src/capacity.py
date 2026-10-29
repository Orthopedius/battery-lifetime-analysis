"""Entladekapazitaeten aus rekonstruierten BaSyTec-Zyklen berechnen.

Die Kathodenaktivmassen werden direkt aus dem Arbeitsblatt
``Spez. Kapazitaet`` der Referenz-Exceldatei gelesen. Diagnosezyklen werden
nicht als Lebensdauerkapazitaeten exportiert. Die Implementierung liest jede
komprimierte Zyklusdatei streamend und benoetigt daher auch bei grossen
Datensaetzen nur wenig Arbeitsspeicher.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from xml.etree import ElementTree as ET


LOGGER = logging.getLogger(__name__)
SHEET_NAME = "Spez. Kapazität"
MAX_PLAUSIBLE_SPECIFIC_CAPACITY = Decimal("350")
FORMATION_ABS_TOLERANCE_MAH = Decimal("0.01")
FORMATION_REL_TOLERANCE = Decimal("0.005")

CAPACITY_COLUMNS = (
    "Group",
    "Excel-Cell-ID",
    "Cycle-Cell-ID",
    "Geometry",
    "Cathode-Active-Mass[g]",
    "Counter-Segment",
    "BaSyTec-Cycle",
    "Protocol-Block",
    "Cycle-In-Block",
    "Protocol-Cycle",
    "Cycle-Type",
    "Lifetime-Cycle",
    "Checkpoint-Cycle",
    "Discharge-Capacity[Ah]",
    "Discharge-Capacity[mAh]",
    "Specific-Discharge-Capacity[mAh/g]",
    "DateTime",
    "Line",
    "Command",
    "I[A]",
    "SourceFile",
    "SourceRow",
    "Classification-Status",
    "Capacity-Status",
    "Capacity-Note",
)


@dataclass(frozen=True)
class CellMass:
    group: str
    excel_cell_id: str
    geometry: str
    active_mass_g: Decimal
    formation_capacity_mah: Decimal | None
    excel_specific_capacity_mah_g: Decimal | None
    source_row: int


@dataclass(frozen=True)
class CellValidation:
    excel_cell_id: str
    cycle_cell_id: str
    active_mass_g: str
    excel_formation_capacity_mah: str
    calculated_excel_specific_capacity_mah_g: str
    excel_specific_capacity_mah_g: str
    excel_formula_status: str
    basytec_cycle_2_capacity_mah: str
    difference_mah: str
    relative_difference_percent: str
    formation_comparison_status: str
    note: str


def parse_decimal(value: object) -> Decimal | None:
    """Parse Excel/TSV numbers, including German decimal commas."""
    if value is None:
        return None
    text = str(value).strip().replace("\u00a0", "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _column_number(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        raise ValueError(f"Ungueltige Excel-Zellreferenz: {reference!r}")
    result = 0
    for character in letters.group(0):
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in archive.namelist():
        return []
    root = ET.fromstring(archive.read(path))
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return ["".join(node.text or "" for node in item.findall(".//x:t", namespace))
            for item in root.findall("x:si", namespace)]


def _worksheet_path(archive: zipfile.ZipFile, sheet_name: str) -> str:
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationship_id = None
    for sheet in workbook.findall(f"{{{main}}}sheets/{{{main}}}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            relationship_id = sheet.attrib.get(f"{{{rel}}}id")
            break
    if not relationship_id:
        raise ValueError(f"Arbeitsblatt {sheet_name!r} nicht gefunden")

    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    for relationship in relationships:
        if relationship.attrib.get("Id") == relationship_id:
            target = relationship.attrib["Target"].lstrip("/")
            return target if target.startswith("xl/") else f"xl/{target}"
    raise ValueError(f"XML-Beziehung fuer Arbeitsblatt {sheet_name!r} fehlt")


def _cell_text(cell: ET.Element, shared: Sequence[str], namespace: str) -> str:
    value = cell.find(f"{{{namespace}}}v")
    if cell.attrib.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(f".//{{{namespace}}}t"))
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared[int(value.text)]
    return value.text


def load_cell_masses(workbook: Path, sheet_name: str = SHEET_NAME) -> list[CellMass]:
    """Read cathode active masses and reference capacities from an XLSX file."""
    if not workbook.is_file():
        raise FileNotFoundError(f"Exceldatei nicht gefunden: {workbook}")
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with zipfile.ZipFile(workbook) as archive:
        shared = _shared_strings(archive)
        root = ET.fromstring(archive.read(_worksheet_path(archive, sheet_name)))

    rows: list[dict[int, str]] = []
    for row in root.findall(f".//{{{namespace}}}sheetData/{{{namespace}}}row"):
        values = {
            _column_number(cell.attrib["r"]): _cell_text(cell, shared, namespace)
            for cell in row.findall(f"{{{namespace}}}c")
            if "r" in cell.attrib
        }
        rows.append(values)
    if not rows:
        raise ValueError(f"Arbeitsblatt {sheet_name!r} ist leer")

    headers = {value.strip(): column for column, value in rows[0].items()}
    required = ("Gruppe", "Zell-ID", "Geometrie", "Aktivmaterialmasse [g]")
    missing = [header for header in required if header not in headers]
    if missing:
        raise ValueError(f"Fehlende Excel-Spalten: {', '.join(missing)}")

    result: list[CellMass] = []
    seen: set[str] = set()
    for excel_row, values in enumerate(rows[1:], start=2):
        cell_id = values.get(headers["Zell-ID"], "").strip()
        if not cell_id:
            continue
        mass = parse_decimal(values.get(headers["Aktivmaterialmasse [g]"]))
        if mass is None or mass <= 0:
            raise ValueError(f"Ungueltige Aktivmaterialmasse in Excel-Zeile {excel_row}: {cell_id}")
        if cell_id in seen:
            raise ValueError(f"Doppelte Zell-ID in der Exceldatei: {cell_id}")
        seen.add(cell_id)
        result.append(CellMass(
            group=values.get(headers["Gruppe"], "").strip(),
            excel_cell_id=cell_id,
            geometry=values.get(headers["Geometrie"], "").strip(),
            active_mass_g=mass,
            formation_capacity_mah=parse_decimal(
                values.get(headers.get("Kapazität nach Formierung [mAh]", -1))
            ),
            excel_specific_capacity_mah_g=parse_decimal(
                values.get(headers.get("Spezifische Kapazität [mAh/g]", -1))
            ),
            source_row=excel_row,
        ))
    return result


def cycle_aliases(excel_cell_id: str) -> tuple[str, ...]:
    """Return known raw-data aliases for an Excel cell ID.

    The twelve 35-deg-C exports omit ``_MA_`` in their filenames, while the
    Excel cell IDs include it.
    """
    aliases = [excel_cell_id]
    if "35°C" in excel_cell_id and "_MH_MA_VZ_" in excel_cell_id:
        aliases.append(excel_cell_id.replace("_MH_MA_VZ_", "_MH_VZ_", 1))
    return tuple(aliases)


def build_mass_index(masses: Iterable[CellMass]) -> dict[str, CellMass]:
    index: dict[str, CellMass] = {}
    for mass in masses:
        for alias in cycle_aliases(mass.excel_cell_id):
            if alias in index:
                raise ValueError(f"Mehrdeutige Zell-ID/Alias: {alias}")
            index[alias] = mass
    return index


def cycle_cell_id(path: Path) -> str:
    suffix = "_cycles.tsv.gz"
    if not path.name.endswith(suffix):
        raise ValueError(f"Keine Cycle-Datei: {path.name}")
    return path.name[:-len(suffix)]


def _cycle_groups(reader: Iterable[dict[str, str]]) -> Iterator[list[dict[str, str]]]:
    current_key: tuple[str, str] | None = None
    group: list[dict[str, str]] = []
    for row in reader:
        key = (row.get("Counter-Segment", ""), row.get("BaSyTec-Cycle", ""))
        if current_key is not None and key != current_key:
            yield group
            group = []
        current_key = key
        group.append(row)
    if group:
        yield group


def select_discharge_endpoint(rows: Iterable[dict[str, str]]) -> dict[str, str] | None:
    """Select the largest valid cumulative discharge value in one cycle."""
    candidates: list[tuple[Decimal, dict[str, str]]] = []
    for row in rows:
        current = parse_decimal(row.get("I[A]"))
        capacity = parse_decimal(row.get("Ah-Step"))
        if (row.get("Command", "").strip().casefold() == "discharge"
                and current is not None and current < 0
                and capacity is not None and capacity != 0):
            candidates.append((abs(capacity), row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def calculate_capacity_row(
    endpoint: dict[str, str], mass: CellMass, source_cell_id: str
) -> dict[str, str]:
    capacity_ah = abs(parse_decimal(endpoint["Ah-Step"]) or Decimal(0))
    capacity_mah = capacity_ah * 1000
    specific = capacity_mah / mass.active_mass_g
    plausible = Decimal(0) < specific <= MAX_PLAUSIBLE_SPECIFIC_CAPACITY
    classification_valid = endpoint.get("Classification-Status", "") != "invalid"
    notes: list[str] = []
    if not plausible:
        notes.append("Spezifische Kapazitaet ausserhalb 0..350 mAh/g")
    if not classification_valid:
        notes.append("Zyklusklassifikation ist invalid")
    return {
        "Group": mass.group,
        "Excel-Cell-ID": mass.excel_cell_id,
        "Cycle-Cell-ID": source_cell_id,
        "Geometry": mass.geometry,
        "Cathode-Active-Mass[g]": str(mass.active_mass_g),
        "Counter-Segment": endpoint.get("Counter-Segment", ""),
        "BaSyTec-Cycle": endpoint.get("BaSyTec-Cycle", ""),
        "Protocol-Block": endpoint.get("Protocol-Block", ""),
        "Cycle-In-Block": endpoint.get("Cycle-In-Block", ""),
        "Protocol-Cycle": endpoint.get("Protocol-Cycle", ""),
        "Cycle-Type": endpoint.get("Cycle-Type", ""),
        "Lifetime-Cycle": endpoint.get("Lifetime-Cycle", ""),
        "Checkpoint-Cycle": endpoint.get("Checkpoint-Cycle", ""),
        "Discharge-Capacity[Ah]": str(capacity_ah),
        "Discharge-Capacity[mAh]": str(capacity_mah),
        "Specific-Discharge-Capacity[mAh/g]": str(specific),
        "DateTime": endpoint.get("DateTime", ""),
        "Line": endpoint.get("Line", ""),
        "Command": endpoint.get("Command", ""),
        "I[A]": endpoint.get("I[A]", ""),
        "SourceFile": endpoint.get("SourceFile", ""),
        "SourceRow": endpoint.get("SourceRow", ""),
        "Classification-Status": endpoint.get("Classification-Status", ""),
        "Capacity-Status": "valid" if plausible and classification_valid else "invalid",
        "Capacity-Note": "; ".join(notes),
    }


def validate_cell(
    mass: CellMass, source_cell_id: str, cycle_2_capacity_mah: Decimal | None
) -> CellValidation:
    calculated_specific: Decimal | None = None
    formula_status = "MISSING"
    if mass.formation_capacity_mah is not None:
        calculated_specific = mass.formation_capacity_mah / mass.active_mass_g
        if mass.excel_specific_capacity_mah_g is not None:
            difference = abs(calculated_specific - mass.excel_specific_capacity_mah_g)
            formula_status = "PASS" if difference <= Decimal("1e-9") else "FAIL"

    difference_mah: Decimal | None = None
    relative_percent: Decimal | None = None
    comparison_status = "MISSING"
    note = "BaSyTec-Zyklus 2 nicht gefunden"
    if mass.formation_capacity_mah is not None and cycle_2_capacity_mah is not None:
        difference_mah = abs(cycle_2_capacity_mah - mass.formation_capacity_mah)
        relative = (difference_mah / mass.formation_capacity_mah
                    if mass.formation_capacity_mah != 0 else Decimal("Infinity"))
        relative_percent = relative * 100
        comparison_status = (
            "PASS" if difference_mah <= FORMATION_ABS_TOLERANCE_MAH
            or relative <= FORMATION_REL_TOLERANCE else "FAIL"
        )
        note = "" if comparison_status == "PASS" else "Excel-Formierung und BaSyTec-Zyklus 2 weichen ab"

    return CellValidation(
        excel_cell_id=mass.excel_cell_id,
        cycle_cell_id=source_cell_id,
        active_mass_g=str(mass.active_mass_g),
        excel_formation_capacity_mah="" if mass.formation_capacity_mah is None else str(mass.formation_capacity_mah),
        calculated_excel_specific_capacity_mah_g="" if calculated_specific is None else str(calculated_specific),
        excel_specific_capacity_mah_g="" if mass.excel_specific_capacity_mah_g is None else str(mass.excel_specific_capacity_mah_g),
        excel_formula_status=formula_status,
        basytec_cycle_2_capacity_mah="" if cycle_2_capacity_mah is None else str(cycle_2_capacity_mah),
        difference_mah="" if difference_mah is None else str(difference_mah),
        relative_difference_percent="" if relative_percent is None else str(relative_percent),
        formation_comparison_status=comparison_status,
        note=note,
    )


def process_cell(
    path: Path, mass: CellMass, writer: csv.DictWriter
) -> tuple[int, int, CellValidation]:
    source_cell_id = cycle_cell_id(path)
    exported = invalid = 0
    cycle_2_capacity: Decimal | None = None
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"Counter-Segment", "BaSyTec-Cycle", "Cycle-Type", "Command", "I[A]", "Ah-Step"}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path.name}: fehlende Spalten: {', '.join(sorted(missing))}")
        for rows in _cycle_groups(reader):
            endpoint = select_discharge_endpoint(rows)
            if endpoint is None:
                continue
            if (cycle_2_capacity is None
                    and endpoint.get("Counter-Segment") == "1"
                    and endpoint.get("BaSyTec-Cycle") == "2"):
                cycle_2_capacity = abs(parse_decimal(endpoint.get("Ah-Step")) or Decimal(0)) * 1000
            if endpoint.get("Cycle-Type") not in {"C/10", "C/2"}:
                continue
            output = calculate_capacity_row(endpoint, mass, source_cell_id)
            if output["Capacity-Status"] == "invalid":
                invalid += 1
                LOGGER.warning(
                    "%s: ungueltige Kapazitaet ausgeschlossen (Protokollzyklus %s): %s",
                    source_cell_id,
                    output["Protocol-Cycle"],
                    output["Capacity-Note"],
                )
                continue
            writer.writerow(output)
            exported += 1
    return exported, invalid, validate_cell(mass, source_cell_id, cycle_2_capacity)


def run(cycles_dir: Path, workbook: Path, output_dir: Path) -> dict[str, int]:
    masses = load_cell_masses(workbook)
    mass_index = build_mass_index(masses)
    cycle_paths = sorted(cycles_dir.glob("*_cycles.tsv.gz"), key=lambda path: path.name.casefold())
    if not cycle_paths:
        raise FileNotFoundError(f"Keine Cycle-Dateien in {cycles_dir}")
    matched = [(path, mass_index[cycle_cell_id(path)]) for path in cycle_paths
               if cycle_cell_id(path) in mass_index]
    matched_excel_ids = {mass.excel_cell_id for _, mass in matched}
    missing_excel = [mass.excel_cell_id for mass in masses if mass.excel_cell_id not in matched_excel_ids]
    if missing_excel:
        raise ValueError("Keine Cycle-Datei fuer Excel-Zellen: " + ", ".join(missing_excel))

    output_dir.mkdir(parents=True, exist_ok=True)
    capacity_path = output_dir / "capacity_by_cycle.tsv.gz"
    validation_path = output_dir / "capacity_validation.csv"
    validations: list[CellValidation] = []
    exported = invalid = 0
    with gzip.open(capacity_path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CAPACITY_COLUMNS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for path, mass in matched:
            count, invalid_count, validation = process_cell(path, mass, writer)
            exported += count
            invalid += invalid_count
            validations.append(validation)
            LOGGER.info("%s: %d Kapazitaeten", cycle_cell_id(path), count)

    with validation_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(CellValidation.__annotations__)
        writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(asdict(validation) for validation in validations)

    result = {
        "excel_cells": len(masses),
        "matched_cells": len(matched),
        "exported_cycles": exported,
        "invalid_capacities": invalid,
        "formula_failures": sum(item.excel_formula_status == "FAIL" for item in validations),
        "formation_failures": sum(item.formation_comparison_status == "FAIL" for item in validations),
        "formation_missing": sum(item.formation_comparison_status == "MISSING" for item in validations),
    }
    LOGGER.info("Kapazitaeten: %s", capacity_path)
    LOGGER.info("Pruefbericht: %s", validation_path)
    return result


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles-dir", type=Path, default=root / "data" / "processed" / "cycles")
    parser.add_argument(
        "--workbook", type=Path,
        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx",
    )
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "processed" / "capacity")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = build_parser().parse_args(argv)
    try:
        result = run(args.cycles_dir, args.workbook, args.output_dir)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        LOGGER.error("Kapazitaetsauswertung abgebrochen: %s", error)
        return 1
    print(
        "Kapazitaetsauswertung abgeschlossen: "
        f"{result['matched_cells']}/{result['excel_cells']} Zellen, "
        f"{result['exported_cycles']} Kapazitaetswerte, "
        f"{result['invalid_capacities']} unplausibel."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
