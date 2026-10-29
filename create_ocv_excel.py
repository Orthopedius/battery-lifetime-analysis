"""Create an OriginPro-friendly workbook from reduced OCV text files."""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


SHEET_ORDER = (
    "Ref_1h", "5um_1h", "5um_24h", "5um_48h", "5um_168h",
    "10um_1h", "10um_24h", "10um_48h", "10um_168h",
    "5um_48h_35C", "10um_48h_35C", "Pouch_Ref_48h",
    "Pouch_10um_48h_0b", "Pouch_10um_48h_1b", "Pouch_10um_48h_2b",
    "Pouch_10um_48h_3b", "Other",
)
TIME_NAMES = {"time[h]", "#time[h]", "~time[h]"}


def _sheet_for(name: str) -> str:
    lowered = name.casefold()
    is_pouch = "pouch" in lowered or "_pz_" in lowered
    if is_pouch and "ref" in lowered:
        return "Pouch_Ref_48h"
    for batch in ("0b", "1b", "2b", "3b"):
        if is_pouch and batch in lowered:
            return f"Pouch_10um_48h_{batch}"
    if "35°c" in lowered or "35?c" in lowered or "35░c" in lowered:
        return "10um_48h_35C" if "10" in lowered else "5um_48h_35C"
    thickness = "10um" if "10µm" in name or "10ám" in name or "10" in lowered else "5um"
    for duration in ("168h", "48h", "24h", "1h"):
        if duration in lowered:
            if "ref" in lowered:
                return "Ref_1h"
            return f"{thickness}_{duration}"
    if "ref" in lowered:
        return "Ref_1h"
    return "Other"


def _encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    try:
        sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8-sig"


def _column_index(header: str, names: set[str]) -> int:
    columns = header.lstrip("\ufeff#~").split()
    for index, column in enumerate(columns):
        if column.casefold() in names:
            return index
    raise ValueError("BaSyTec-Spalte fehlt")


def _data_index(header: str, fields: list[str], index: int) -> int:
    columns = header.lstrip("\ufeff#~").split()
    if len(fields) == len(columns) + 1:
        datetime_index = _column_index(header, {"datetime"})
        if index > datetime_index:
            return index + 1
    return index


def _number(value: str) -> str:
    return value.replace(",", ".")


def load_series(path: Path) -> list[tuple[str, str]]:
    with path.open("r", encoding=_encoding(path), errors="strict", newline="") as handle:
        header = handle.readline()
        if not header:
            raise ValueError(f"Datei ist leer: {path}")
        time_index = _column_index(header, TIME_NAMES)
        voltage_index = _column_index(header, {"u[v]", "voltage[v]"})
        values: list[tuple[str, str]] = []
        for line_number, line in enumerate(handle, start=2):
            if not line.strip():
                continue
            fields = line.split()
            row_time = _data_index(header, fields, time_index)
            row_voltage = _data_index(header, fields, voltage_index)
            if len(fields) <= max(row_time, row_voltage):
                continue
            values.append((_number(fields[row_time]), _number(fields[row_voltage])))
    return values


def _cell(reference: str, value: object, style: int = 0) -> str:
    attributes = f' r="{reference}" s="{style}"'
    if value is None:
        return f'<c{attributes}/>'
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[Ee][-+]?\d+)?", value)):
        return f'<c{attributes}><v>{escape(str(value))}</v></c>'
    return f'<c{attributes} t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _worksheet(columns: list[list[object]]) -> str:
    rows: list[str] = []
    max_rows = max((len(column) for column in columns), default=0)
    for row_number in range(max_rows):
        cells = []
        for column_number, column in enumerate(columns, start=1):
            if row_number < len(column):
                cells.append(_cell(f"{_column_name(column_number)}{row_number + 1}", column[row_number]))
        rows.append(f'<row r="{row_number + 1}">' + "".join(cells) + "</row>")
    dimension = f"A1:{_column_name(len(columns))}{max_rows}" if columns else "A1"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><sheetData>{"".join(rows)}</sheetData>'
        '<autoFilter ref="A1:' + _column_name(len(columns)) + str(max_rows) + '"/>'
        '</worksheet>'
    )


def _display_title(path: Path) -> str:
    title = path.stem.replace("_Lebensdauertest", " Lebensdauertest")
    title = title.replace("_Lebensdauer", " Lebensdauer")
    title = title.replace("_Formierung", " Formierung")
    title = title.replace("_Vorversuchsreihe", " Vorversuchsreihe")
    title = title.replace("_Langzeit", " Langzeit")
    title = title.replace("_", " ")
    return title


def _average_series(series_list: list[list[tuple[str, str]]]) -> list[tuple[float, float]]:
    buckets: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for series in series_list:
        for time_text, voltage_text in series:
            time = float(time_text)
            interval = 10 if time * 3600 <= 600.5 else 60
            bucket = round(time * 3600 / interval)
            buckets.setdefault((interval, bucket), []).append((time, float(voltage_text)))
    return [
        (sum(time for time, _ in values) / len(values),
         sum(voltage for _, voltage in values) / len(values))
        for _, values in sorted(buckets.items(), key=lambda item: item[0][1] * item[0][0])
    ]


def _data_type(path: Path) -> str:
    lowered = path.name.casefold()
    if "formierung" in lowered:
        return "Formierung"
    if "lebensdauertest" in lowered:
        return "Lebensdauertest"
    if "lebensdauer" in lowered:
        return "Lebensdauer"
    return "Sonstige"


def write_workbook(groups: dict[str, list[Path]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    sheets = [name for name in SHEET_ORDER if groups.get(name)]
    workbook = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
    for index, name in enumerate(sheets, start=1):
        workbook += f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
    workbook += "</sheets></workbook>"
    package_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    workbook_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    for index in range(1, len(sheets) + 1):
        workbook_rels += f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        content += f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    workbook_rels += '</Relationships>'
    content += '</Types>'
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        for index, sheet_name in enumerate(sheets, start=1):
            columns: list[list[object]] = []
            series_by_type: dict[str, list[list[tuple[str, str]]]] = {}
            for path in groups[sheet_name]:
                series = load_series(path)
                if "langzeit" in path.name.casefold() and len(series) <= 1:
                    continue
                title = _display_title(path)
                series_by_type.setdefault(_data_type(path), []).append(series)
                columns.extend([
                    [title, "Zeit [h]"] + [time for time, _ in series],
                    [title, "Spannung [V]"] + [voltage for _, voltage in series],
                    [""],
                ])
            for data_type, series_list in series_by_type.items():
                average = _average_series(series_list)
                columns.extend([
                    [f"Durchschnitt {data_type}", "Zeit [h]"] + [time for time, _ in average],
                    [f"Durchschnitt {data_type}", "Spannung [V]"] + [voltage for _, voltage in average],
                ])
            if columns and columns[-1] == [""]:
                columns.pop()
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _worksheet(columns))


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=root / "data" / "processed" / "ocv_plot")
    parser.add_argument("--output", type=Path, default=root / "data" / "output" / "OCV_Plotdaten.xlsx")
    args = parser.parse_args(argv)
    try:
        groups: dict[str, list[Path]] = {name: [] for name in SHEET_ORDER}
        for path in sorted(args.input_dir.glob("*.txt"), key=lambda item: item.name.casefold()):
            groups[_sheet_for(path.name)].append(path)
        write_workbook(groups, args.output)
    except (OSError, ValueError) as error:
        print(f"OCV-Excel-Erstellung abgebrochen: {error}", file=sys.stderr)
        return 1
    print(f"Arbeitsmappe geschrieben: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())