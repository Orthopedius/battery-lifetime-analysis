"""Gueltige Lebensdauerkapazitaeten in die Form der Excel-Vorlage exportieren.

Das Modul benoetigt bewusst weder pandas noch openpyxl. Es schreibt die
Office-Open-XML-Datei mit der Python-Standardbibliothek und uebernimmt dabei
Farben, Schriftarten und Zahlenformate aus der bereitgestellten Vorlage.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import math
import shutil
import statistics
import sys
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree as ET

try:
    from .capacity import CellMass, load_cell_masses, parse_decimal
except ImportError:  # pragma: no cover - direkter Skriptaufruf
    from capacity import CellMass, load_cell_masses, parse_decimal  # type: ignore


LOGGER = logging.getLogger(__name__)
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
WORKSHEET_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
)
STYLES_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
)
THEME_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
)
WORKSHEET_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)
WORKBOOK_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
)
STYLES_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
)
THEME_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.theme+xml"
)
CORE_CONTENT_TYPE = "application/vnd.openxmlformats-package.core-properties+xml"
APP_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.extended-properties+xml"
)

ET.register_namespace("", MAIN_NS)
ET.register_namespace("r", REL_NS)
ET.register_namespace("vt", VT_NS)

SHEET_BY_GROUP = {
    "CR2032 Referenz": "Ref_1h",
    "CR2032 5µm 1h": "5um_1h",
    "CR2032 5µm 24h": "5um_24h",
    "CR2032 5µm 48h": "5um_48h",
    "CR2032 5µm 168h": "5um_168h",
    "CR2032 10µm 1h": "10um_1h",
    "CR2032 10µm 24h": "10um_24h",
    "CR2032 10µm 48h": "10um_48h",
    "CR2032 10µm 168h": "10um_168h",
    "CR2032 5µm 48h 35°C": "5um_48h_35C",
    "CR2032 10µm 48h 35°C": "10um_48h_35C",
    "Pouch Ref 48h": "Pouch_Ref_48h",
    "Pouch 10µm 48h 0b": "Pouch_10um_48h_0b",
    "Pouch 10µm 48h 1b": "Pouch_10um_48h_1b",
    "Pouch 10µm 48h 2b": "Pouch_10um_48h_2b",
    "Pouch 10µm 48h 3b": "Pouch_10um_48h_3b",
}

NO_LONGTERM_CELL = "df_MH_MA_VZ_3,8_5µm_24h_4"
HEADERS = (
    "Zyklus", "spezifische Kapazität C/2", "Ah-Step C/2",
    "Zyklus", "spezifische Kapazität C/10", "Ah-Step C/10",
)
SUMMARY_HEADERS = (
    "Zyklus", "spezifische Kapazität C/2", "Std spezifische Kapazität C/2",
    "SOH C/2", "Std SOH C/2", "Zyklus", "spezifische Kapazität C/10",
    "Std spezifische Kapazität C/10", "SOH C/10", "Std SOH C/10",
)


@dataclass(frozen=True)
class CapacityPoint:
    cycle: int
    specific_mah_g: float
    capacity_ah: float


@dataclass(frozen=True)
class SummaryPoint:
    cycle: int
    mean_capacity: float
    std_capacity: float | None
    mean_soh: float
    std_soh: float | None


def column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _column_number(reference: str) -> int:
    result = 0
    for character in reference:
        if not character.isalpha():
            break
        result = result * 26 + ord(character.upper()) - 64
    return result


def load_capacity_points(path: Path) -> dict[str, dict[str, list[CapacityPoint]]]:
    """Lade ausschliesslich bereits als gueltig markierte Kapazitaeten."""
    result: dict[str, dict[str, list[CapacityPoint]]] = defaultdict(
        lambda: {"C/2": [], "C/10": []}
    )
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {
            "Excel-Cell-ID", "Protocol-Cycle", "Cycle-Type",
            "Discharge-Capacity[Ah]", "Specific-Discharge-Capacity[mAh/g]",
            "Capacity-Status",
        }
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Kapazitaetsdatei: fehlende Spalten: {', '.join(sorted(missing))}")
        seen: set[tuple[str, int]] = set()
        for row_number, row in enumerate(reader, start=2):
            if row["Capacity-Status"] != "valid" or row["Cycle-Type"] not in {"C/2", "C/10"}:
                continue
            cycle_value = parse_decimal(row["Protocol-Cycle"])
            specific = parse_decimal(row["Specific-Discharge-Capacity[mAh/g]"])
            capacity = parse_decimal(row["Discharge-Capacity[Ah]"])
            if cycle_value is None or cycle_value != int(cycle_value) or specific is None or capacity is None:
                raise ValueError(f"Kapazitaetsdatei: ungueltiger Wert in Zeile {row_number}")
            cell_id = row["Excel-Cell-ID"]
            cycle = int(cycle_value)
            key = (cell_id, cycle)
            if key in seen:
                raise ValueError(f"Doppelter Protokollzyklus: {cell_id}, {cycle}")
            seen.add(key)
            result[cell_id][row["Cycle-Type"]].append(
                CapacityPoint(cycle, float(specific), float(capacity))
            )
    for rates in result.values():
        for series in rates.values():
            series.sort(key=lambda item: item.cycle)
    return result


def summarize(
    cell_ids: Iterable[str], rate: str,
    points: dict[str, dict[str, list[CapacityPoint]]],
) -> list[SummaryPoint]:
    """Berechne Mittel, Stichproben-Std und zellbezogenes SOH je Zyklus."""
    capacities: dict[int, list[float]] = defaultdict(list)
    sohs: dict[int, list[float]] = defaultdict(list)
    for cell_id in cell_ids:
        series = points.get(cell_id, {}).get(rate, [])
        if not series:
            continue
        baseline = series[0].specific_mah_g
        if baseline <= 0:
            continue
        for point in series:
            capacities[point.cycle].append(point.specific_mah_g)
            sohs[point.cycle].append(point.specific_mah_g / baseline * 100.0)

    result: list[SummaryPoint] = []
    for cycle in sorted(capacities):
        values = capacities[cycle]
        soh_values = sohs[cycle]
        result.append(SummaryPoint(
            cycle=cycle,
            mean_capacity=statistics.fmean(values),
            std_capacity=statistics.stdev(values) if len(values) > 1 else None,
            mean_soh=statistics.fmean(soh_values),
            std_soh=statistics.stdev(soh_values) if len(soh_values) > 1 else None,
        ))
    return result


def _cell(parent: ET.Element, reference: str, value: str | int | float | None,
          style: int | None = None) -> ET.Element:
    attributes = {"r": reference}
    if style is not None:
        attributes["s"] = str(style)
    cell = ET.SubElement(parent, f"{{{MAIN_NS}}}c", attributes)
    if value is None:
        return cell
    if isinstance(value, str):
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, f"{{{MAIN_NS}}}is")
        text = ET.SubElement(inline, f"{{{MAIN_NS}}}t")
        text.text = value
    else:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"Nicht endlicher Excelwert in {reference}: {value}")
        ET.SubElement(cell, f"{{{MAIN_NS}}}v").text = str(value)
    return cell


def _row(sheet_data: ET.Element, rows: dict[int, ET.Element], number: int) -> ET.Element:
    if number not in rows:
        rows[number] = ET.SubElement(
            sheet_data, f"{{{MAIN_NS}}}row", {"r": str(number)}
        )
    return rows[number]


def _write_series(sheet_data: ET.Element, rows: dict[int, ET.Element], start_col: int,
                  c2: Sequence[CapacityPoint], c10: Sequence[CapacityPoint]) -> int:
    max_length = max(len(c2), len(c10), 0)
    for offset in range(max_length):
        row_number = 4 + offset
        row = _row(sheet_data, rows, row_number)
        if offset < len(c2):
            point = c2[offset]
            _cell(row, f"{column_name(start_col)}{row_number}", point.cycle)
            _cell(row, f"{column_name(start_col + 1)}{row_number}", point.specific_mah_g)
            _cell(row, f"{column_name(start_col + 2)}{row_number}", point.capacity_ah, 3)
        if offset < len(c10):
            point = c10[offset]
            _cell(row, f"{column_name(start_col + 3)}{row_number}", point.cycle)
            _cell(row, f"{column_name(start_col + 4)}{row_number}", point.specific_mah_g)
            _cell(row, f"{column_name(start_col + 5)}{row_number}", point.capacity_ah)
    return 3 + max_length


def _write_summary(sheet_data: ET.Element, rows: dict[int, ET.Element], start_col: int,
                   c2: Sequence[SummaryPoint], c10: Sequence[SummaryPoint]) -> int:
    max_length = max(len(c2), len(c10), 0)
    for offset in range(max_length):
        row_number = 4 + offset
        row = _row(sheet_data, rows, row_number)
        if offset < len(c2):
            item = c2[offset]
            values = (item.cycle, item.mean_capacity, item.std_capacity, item.mean_soh, item.std_soh)
            for index, value in enumerate(values):
                _cell(row, f"{column_name(start_col + index)}{row_number}", value,
                      3 if index == 4 else None)
        if offset < len(c10):
            item = c10[offset]
            values = (item.cycle, item.mean_capacity, item.std_capacity, item.mean_soh, item.std_soh)
            for index, value in enumerate(values):
                _cell(row, f"{column_name(start_col + 5 + index)}{row_number}", value)
    return 3 + max_length


def build_sheet_xml(
    sheet_name: str, group: str, masses: Sequence[CellMass],
    points: dict[str, dict[str, list[CapacityPoint]]],
) -> tuple[bytes, int, int]:
    cell_ids = [mass.excel_cell_id for mass in masses]
    summary_col = len(masses) * 7 + 1
    last_col = summary_col + 9
    root = ET.Element(f"{{{MAIN_NS}}}worksheet")
    dimension = ET.SubElement(root, f"{{{MAIN_NS}}}dimension", {"ref": "A1"})
    views = ET.SubElement(root, f"{{{MAIN_NS}}}sheetViews")
    ET.SubElement(views, f"{{{MAIN_NS}}}sheetView", {"workbookViewId": "0"})
    ET.SubElement(root, f"{{{MAIN_NS}}}sheetFormatPr", {"defaultRowHeight": "15"})
    cols = ET.SubElement(root, f"{{{MAIN_NS}}}cols")
    ET.SubElement(cols, f"{{{MAIN_NS}}}col", {
        "min": "1", "max": str(last_col), "width": "18", "customWidth": "1"
    })
    sheet_data = ET.SubElement(root, f"{{{MAIN_NS}}}sheetData")
    rows: dict[int, ET.Element] = {}

    row1 = _row(sheet_data, rows, 1)
    for col in range(1, last_col + 1):
        _cell(row1, f"{column_name(col)}1", sheet_name if col == 1 else None,
              4 if col == 1 else 5)
    row2, row3 = _row(sheet_data, rows, 2), _row(sheet_data, rows, 3)
    merge_refs: list[str] = [f"A1:{column_name(last_col)}1"]
    max_row = 3
    for index, mass in enumerate(masses):
        start = index * 7 + 1
        title = f"{mass.excel_cell_id}_Lebensdauer_Langzeit"
        if mass.excel_cell_id == NO_LONGTERM_CELL:
            title += " – keine Langzeitdaten"
        for col in range(start, start + 6):
            _cell(row2, f"{column_name(col)}2", title if col == start else None,
                  4 if col == start else 5)
        merge_refs.append(f"{column_name(start)}2:{column_name(start + 5)}2")
        for offset, header in enumerate(HEADERS):
            _cell(row3, f"{column_name(start + offset)}3", header,
                  2 if offset == 2 else 1)
        rates = points.get(mass.excel_cell_id, {"C/2": [], "C/10": []})
        max_row = max(max_row, _write_series(
            sheet_data, rows, start, rates.get("C/2", []), rates.get("C/10", [])
        ))

    summary_title = f"Mittelwert {group.replace('CR2032 ', '')}"
    for col in range(summary_col, summary_col + 10):
        _cell(row2, f"{column_name(col)}2", summary_title if col == summary_col else None,
              4 if col == summary_col else 5)
    merge_refs.append(f"{column_name(summary_col)}2:{column_name(last_col)}2")
    for offset, header in enumerate(SUMMARY_HEADERS):
        _cell(row3, f"{column_name(summary_col + offset)}3", header,
              2 if offset == 4 else 1)
    max_row = max(max_row, _write_summary(
        sheet_data, rows, summary_col,
        summarize(cell_ids, "C/2", points), summarize(cell_ids, "C/10", points),
    ))

    sheet_data[:] = sorted(sheet_data, key=lambda item: int(item.attrib["r"]))
    for row in sheet_data:
        row[:] = sorted(row, key=lambda cell: _column_number(cell.attrib["r"]))
    dimension.set("ref", f"A1:{column_name(last_col)}{max(max_row, 4)}")
    merges = ET.SubElement(root, f"{{{MAIN_NS}}}mergeCells", {"count": str(len(merge_refs))})
    for reference in merge_refs:
        ET.SubElement(merges, f"{{{MAIN_NS}}}mergeCell", {"ref": reference})
    ET.SubElement(root, f"{{{MAIN_NS}}}pageMargins", {
        "left": "0.7", "right": "0.7", "top": "0.75", "bottom": "0.75",
        "header": "0.3", "footer": "0.3",
    })
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), max_row, last_col


def _workbook_xml(sheets: Sequence[tuple[str, bytes]]) -> bytes:
    """Erzeuge eine minimale Arbeitsmappe ohne versionsspezifische Erweiterungen.

    Die Vorlage enthaelt ``mc:Ignorable`` und ``mc:Choice`` mit QName-Werten.
    Ein erneutes Serialisieren dieser Elemente kann Praefixe umbenennen, ohne
    die QName-Werte anzupassen. Eine kleine, standardkonforme Arbeitsmappe
    vermeidet diese Fehlerklasse vollstaendig.
    """
    workbook = ET.Element(f"{{{MAIN_NS}}}workbook")
    ET.SubElement(workbook, f"{{{MAIN_NS}}}workbookPr")
    views = ET.SubElement(workbook, f"{{{MAIN_NS}}}bookViews")
    ET.SubElement(views, f"{{{MAIN_NS}}}workbookView", {"activeTab": "0"})
    sheet_parent = ET.SubElement(workbook, f"{{{MAIN_NS}}}sheets")
    for index, (name, _) in enumerate(sheets, start=1):
        ET.SubElement(sheet_parent, f"{{{MAIN_NS}}}sheet", {
            "name": name, "sheetId": str(index),
            f"{{{REL_NS}}}id": f"rIdSheet{index}",
        })
    ET.SubElement(workbook, f"{{{MAIN_NS}}}calcPr", {"calcId": "191029"})
    return ET.tostring(workbook, encoding="utf-8", xml_declaration=True)


def _workbook_relationships_xml(sheet_count: int) -> bytes:
    relationships = ET.Element(f"{{{PACKAGE_REL_NS}}}Relationships")
    for index in range(1, sheet_count + 1):
        ET.SubElement(relationships, f"{{{PACKAGE_REL_NS}}}Relationship", {
            "Id": f"rIdSheet{index}", "Type": WORKSHEET_REL_TYPE,
            "Target": f"worksheets/sheet{index}.xml",
        })
    ET.SubElement(relationships, f"{{{PACKAGE_REL_NS}}}Relationship", {
        "Id": "rIdStyles", "Type": STYLES_REL_TYPE, "Target": "styles.xml",
    })
    ET.SubElement(relationships, f"{{{PACKAGE_REL_NS}}}Relationship", {
        "Id": "rIdTheme", "Type": THEME_REL_TYPE, "Target": "theme/theme1.xml",
    })
    return ET.tostring(relationships, encoding="utf-8", xml_declaration=True)


def _content_types_xml(sheet_count: int) -> bytes:
    content_types = ET.Element(f"{{{CONTENT_NS}}}Types")
    ET.SubElement(content_types, f"{{{CONTENT_NS}}}Default", {
        "Extension": "rels",
        "ContentType": "application/vnd.openxmlformats-package.relationships+xml",
    })
    ET.SubElement(content_types, f"{{{CONTENT_NS}}}Default", {
        "Extension": "xml", "ContentType": "application/xml",
    })
    overrides = (
        ("/xl/workbook.xml", WORKBOOK_CONTENT_TYPE),
        ("/xl/theme/theme1.xml", THEME_CONTENT_TYPE),
        ("/xl/styles.xml", STYLES_CONTENT_TYPE),
        ("/docProps/core.xml", CORE_CONTENT_TYPE),
        ("/docProps/app.xml", APP_CONTENT_TYPE),
    )
    for part_name, content_type in overrides:
        ET.SubElement(content_types, f"{{{CONTENT_NS}}}Override", {
            "PartName": part_name, "ContentType": content_type,
        })
    for index in range(1, sheet_count + 1):
        ET.SubElement(content_types, f"{{{CONTENT_NS}}}Override", {
            "PartName": f"/xl/worksheets/sheet{index}.xml",
            "ContentType": WORKSHEET_CONTENT_TYPE,
        })
    return ET.tostring(content_types, encoding="utf-8", xml_declaration=True)


def _app_properties_xml(sheets: Sequence[tuple[str, bytes]]) -> bytes:
    properties = ET.Element(f"{{{APP_NS}}}Properties")
    ET.SubElement(properties, f"{{{APP_NS}}}Application").text = "Microsoft Excel"
    ET.SubElement(properties, f"{{{APP_NS}}}DocSecurity").text = "0"
    ET.SubElement(properties, f"{{{APP_NS}}}ScaleCrop").text = "false"
    heading_pairs = ET.SubElement(properties, f"{{{APP_NS}}}HeadingPairs")
    vector = ET.SubElement(heading_pairs, f"{{{VT_NS}}}vector", {
        "size": "2", "baseType": "variant",
    })
    label_variant = ET.SubElement(vector, f"{{{VT_NS}}}variant")
    ET.SubElement(label_variant, f"{{{VT_NS}}}lpstr").text = "Arbeitsblätter"
    count_variant = ET.SubElement(vector, f"{{{VT_NS}}}variant")
    ET.SubElement(count_variant, f"{{{VT_NS}}}i4").text = str(len(sheets))
    titles = ET.SubElement(properties, f"{{{APP_NS}}}TitlesOfParts")
    title_vector = ET.SubElement(titles, f"{{{VT_NS}}}vector", {
        "size": str(len(sheets)), "baseType": "lpstr",
    })
    for name, _ in sheets:
        ET.SubElement(title_vector, f"{{{VT_NS}}}lpstr").text = name
    ET.SubElement(properties, f"{{{APP_NS}}}LinksUpToDate").text = "false"
    ET.SubElement(properties, f"{{{APP_NS}}}SharedDoc").text = "false"
    ET.SubElement(properties, f"{{{APP_NS}}}HyperlinksChanged").text = "false"
    ET.SubElement(properties, f"{{{APP_NS}}}AppVersion").text = "16.0300"
    return ET.tostring(properties, encoding="utf-8", xml_declaration=True)


def write_workbook(template: Path, destination: Path,
                   sheets: Sequence[tuple[str, bytes]]) -> None:
    """Erzeuge eine saubere XLSX und uebernehme Styles/Theme der Vorlage."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(template, "r") as source:
        required_parts = {
            "_rels/.rels", "xl/theme/theme1.xml", "xl/styles.xml", "docProps/core.xml",
        }
        missing_parts = required_parts.difference(source.namelist())
        if missing_parts:
            raise ValueError(
                "Excel-Vorlage: erforderliche Paketbestandteile fehlen: "
                + ", ".join(sorted(missing_parts))
            )

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False,
                                         dir=destination.parent) as temporary:
            temporary_path = Path(temporary.name)
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as output:
                for part in sorted(required_parts):
                    output.writestr(part, source.read(part))
                output.writestr("xl/workbook.xml", _workbook_xml(sheets))
                output.writestr(
                    "xl/_rels/workbook.xml.rels",
                    _workbook_relationships_xml(len(sheets)),
                )
                output.writestr("[Content_Types].xml", _content_types_xml(len(sheets)))
                output.writestr("docProps/app.xml", _app_properties_xml(sheets))
                for index, (_, xml) in enumerate(sheets, start=1):
                    output.writestr(f"xl/worksheets/sheet{index}.xml", xml)
            shutil.move(str(temporary_path), destination)
        finally:
            temporary_path.unlink(missing_ok=True)


def run(capacity_path: Path, mass_workbook: Path, template: Path,
        destination: Path, report_path: Path | None = None) -> dict[str, int]:
    masses = load_cell_masses(mass_workbook)
    grouped: dict[str, list[CellMass]] = defaultdict(list)
    for mass in masses:
        if mass.group not in SHEET_BY_GROUP:
            raise ValueError(f"Keine Blattzuordnung fuer Gruppe: {mass.group}")
        grouped[mass.group].append(mass)
    missing_groups = set(SHEET_BY_GROUP).difference(grouped)
    if missing_groups:
        raise ValueError("Gruppen ohne Zelldaten: " + ", ".join(sorted(missing_groups)))

    points = load_capacity_points(capacity_path)
    sheets: list[tuple[str, bytes]] = []
    report_lines = [
        "Excel-Export Lebensdauerdaten",
        f"Quelle: {capacity_path.resolve()}",
        "Zeitbasierte Darstellung: nein",
        "Ungueltige Werte: bereits vor dem Export ausgeschlossen",
        "",
    ]
    for group, sheet_name in SHEET_BY_GROUP.items():
        xml, max_row, last_col = build_sheet_xml(sheet_name, group, grouped[group], points)
        sheets.append((sheet_name, xml))
        value_count = sum(
            len(points.get(mass.excel_cell_id, {}).get(rate, []))
            for mass in grouped[group] for rate in ("C/2", "C/10")
        )
        report_lines.append(
            f"{sheet_name}: {len(grouped[group])} Zellen, {value_count} Werte, "
            f"Bereich A1:{column_name(last_col)}{max_row}"
        )
    report_lines.extend([
        "",
        f"{NO_LONGTERM_CELL}: keine Langzeitdaten",
        "SOH: je Zelle und Rate auf deren ersten gueltigen Kapazitaetswert normiert.",
        "Std: Stichproben-Standardabweichung (n-1); bei nur einem Wert leer.",
    ])
    write_workbook(template, destination, sheets)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8-sig")
    return {
        "sheets": len(sheets), "cells": len(masses),
        "capacity_values": sum(
            len(series) for rates in points.values() for series in rates.values()
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capacity", type=Path,
                        default=root / "data" / "processed" / "capacity" / "capacity_by_cycle.tsv.gz")
    parser.add_argument("--mass-workbook", type=Path,
                        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx")
    parser.add_argument("--template", type=Path,
                        default=root / "template" / "Zelldaten - Vollzellen - Zeit.xlsx")
    parser.add_argument("--output", type=Path,
                        default=root / "data" / "output" / "Zelldaten_Lebensdauer_Auswertung.xlsx")
    parser.add_argument("--report", type=Path,
                        default=root / "data" / "output" / "excel_export_report.txt")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = build_parser().parse_args(argv)
    try:
        result = run(args.capacity, args.mass_workbook, args.template, args.output, args.report)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        LOGGER.error("Excel-Export abgebrochen: %s", error)
        return 1
    LOGGER.info(
        "Excel-Export fertig: %d Blaetter, %d Zellen, %d Kapazitaetswerte -> %s",
        result["sheets"], result["cells"], result["capacity_values"], args.output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
