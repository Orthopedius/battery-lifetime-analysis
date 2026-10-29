"""Create an OriginPro-friendly workbook for the OCV derivative."""

from __future__ import annotations

import argparse
import csv
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from create_ocv_excel import (
    SHEET_ORDER,
    _column_name,
    _display_title,
    _data_type,
    _sheet_for,
    _worksheet,
)
from calculate_ocv_derivative import load_ocv, derivative


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    inline = cell.findtext(f".//{{{MAIN_NS}}}t")
    if inline is not None:
        return inline
    value = cell.findtext(f"{{{MAIN_NS}}}v")
    if value is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared[int(value)]
    return value


def included_titles(workbook_path: Path) -> set[str]:
    with zipfile.ZipFile(workbook_path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.text or "" for node in item.iter(f"{{{MAIN_NS}}}t")) for item in strings]
        titles: set[str] = set()
        for index, _ in enumerate(workbook.findall(f".//{{{MAIN_NS}}}sheet"), start=1):
            sheet = ET.fromstring(archive.read(f"xl/worksheets/sheet{index}.xml"))
            row = sheet.find(f".//{{{MAIN_NS}}}row")
            if row is None:
                continue
            for cell in row.findall(f"{{{MAIN_NS}}}c"):
                value = _cell_value(cell, shared)
                if value:
                    titles.add(value)
    return titles


def _title_key(value: str) -> str:
    return value.casefold().replace("µ", "u").replace("á", "u").replace("░", "c")


def load_derivative(path: Path) -> list[tuple[float, float | None]]:
    values: list[tuple[float, float | None]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            slope = row["dU/dt [V/h]"].strip()
            values.append((float(row["Zeit [h]"].replace(",", ".")), float(slope) if slope else None))
    return values


def _average_derivative(series_list: list[list[tuple[float, float | None]]]) -> list[tuple[float, float | None]]:
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for series in series_list:
        for time, slope in series:
            if slope is None:
                continue
            interval = 10 if time * 3600 <= 600.5 else 60
            bucket = round(time * 3600 / interval)
            buckets.setdefault((interval, bucket), []).append((time, slope))
    return [
        (sum(time for time, _ in values) / len(values),
         sum(slope for _, slope in values) / len(values))
        for _, values in sorted(buckets.items(), key=lambda item: item[0][1] * item[0][0])
    ]


def _worksheet_with_derivatives(columns: list[list[object]]) -> str:
    return _worksheet(columns)


def write_workbook(input_dir: Path, source_workbook: Path, output: Path) -> int:
    allowed = {_title_key(title) for title in included_titles(source_workbook)}
    groups: dict[str, list[Path]] = {name: [] for name in SHEET_ORDER}
    for path in sorted(input_dir.glob("*_derivative.tsv"), key=lambda item: item.name.casefold()):
        source_name = path.name.removesuffix("_derivative.tsv")
        if _title_key(_display_title(Path(source_name))) not in allowed:
            continue
        groups[_sheet_for(source_name)].append(path)
    sheets = [name for name in SHEET_ORDER if groups[name]]
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
        + "".join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1))
        + "</sheets></workbook>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1))
        + "</Relationships>"
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types '
        'xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, len(sheets) + 1))
        + "</Types>"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, sheet_name in enumerate(sheets, 1):
            columns: list[list[object]] = []
            series_by_type: dict[str, list[list[tuple[float, float | None]]]] = {}
            for path in groups[sheet_name]:
                source_name = path.name.removesuffix("_derivative.tsv")
                values = load_derivative(path)
                series_by_type.setdefault(_data_type(Path(source_name)), []).append(values)
                title = _display_title(Path(source_name))
                columns.extend([
                    [title, "Zeit [h]"] + [time for time, _ in values],
                    [title, "dU/dt [V/h]"] + [slope for _, slope in values],
                    [""],
                ])
            for data_type, series_list in series_by_type.items():
                average = _average_derivative(series_list)
                columns.extend([
                    [f"Durchschnitt {data_type}", "Zeit [h]"] + [time for time, _ in average],
                    [f"Durchschnitt {data_type}", "dU/dt [V/h]"] + [slope for _, slope in average],
                ])
            if columns and columns[-1] == [""]:
                columns.pop()
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet_with_derivatives(columns))
    return sum(len(values) for values in groups.values())


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=root / "data" / "processed" / "ocv_derivative")
    parser.add_argument("--source-workbook", type=Path, default=root / "data" / "output" / "OCV_Plotdaten.xlsx")
    parser.add_argument("--output", type=Path, default=root / "data" / "output" / "OCV_Ableitung.xlsx")
    args = parser.parse_args(argv)
    try:
        count = write_workbook(args.input_dir, args.source_workbook, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"OCV-Ableitungs-Excel abgebrochen: {error}", file=sys.stderr)
        return 1
    print(f"Arbeitsmappe geschrieben: {args.output} ({count} Batterien)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())