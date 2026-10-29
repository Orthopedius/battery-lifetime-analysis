"""Rohdaten speicherschonend lesen, vereinheitlichen und auf State 2 filtern.

Die Implementierung verwendet nur die Python-Standardbibliothek. Jede Eingabedatei
wird zeilenweise verarbeitet; die Groesse der Rohdateien beeinflusst daher den
Arbeitsspeicherbedarf praktisch nicht. Pro erkannter Zelle entsteht eine
komprimierte TSV-Datei sowie ein maschinenlesbarer Verarbeitungsbericht (in: data/processed/state2/raw_reader_report.json).
Eine kompakte Zusammenfassung wird im Terminal ausgegeben.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import re
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Sequence, TextIO

try:
    from .cell_scope import CellScope, load_cell_scope
except ImportError:  # pragma: no cover - direkter Skriptaufruf
    from cell_scope import CellScope, load_cell_scope  # type: ignore


LOGGER = logging.getLogger(__name__)
SUPPORTED_SUFFIXES = {".txt", ".csv"}

OUTPUT_COLUMNS = (
    "DateTime", "Time[h]", "t-Step[h]", "t-Set[h]", "t-Cyc[h]", "Line",
    "Command", "U[V]", "I[A]", "Ah[Ah]", "Ah-Step", "Wh[Wh]", "Wh-Step",
    "T1[°C]", "Cyc-Count", "State", "SourceFile", "SourceRow",
)
NUMERIC_COLUMNS = {
    "Time[h]", "t-Step[h]", "t-Set[h]", "t-Cyc[h]", "Line", "U[V]", "I[A]",
    "Ah[Ah]", "Ah-Step", "Wh[Wh]", "Wh-Step", "T1[°C]", "Cyc-Count", "State",
}
DATETIME_FORMATS = (
    "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f",
)


def _header_key(value: str) -> str:
    """Erzeuge einen toleranten Schluessel fuer bekannte Header-Varianten."""
    value = value.lstrip("\ufeff#~").strip().casefold()
    return re.sub(r"[\s_]+", "", value)


HEADER_ALIASES = {
    _header_key(alias): canonical
    for canonical, aliases in {
        "DateTime": ("DateTime", "Datum/Zeit", "Timestamp"),
        "Time[h]": ("Time[h]", "#Time[h]", "~Time[h]"),
        "t-Step[h]": ("t-Step[h]",), "t-Set[h]": ("t-Set[h]",),
        "t-Cyc[h]": ("t-Cyc[h]",), "Line": ("Line",),
        "Command": ("Command",), "U[V]": ("U[V]", "Voltage[V]"),
        "I[A]": ("I[A]", "Current[A]"), "Ah[Ah]": ("Ah[Ah]",),
        "Ah-Step": ("Ah-Step", "Ah_Step"), "Wh[Wh]": ("Wh[Wh]",),
        "Wh-Step": ("Wh-Step", "Wh_Step"),
        "T1[°C]": ("T1[°C]", "T1[C]", "Temperature[°C]"),
        "Cyc-Count": ("Cyc-Count", "Cycle", "Cycle-Count"),
        "State": ("State",),
    }.items()
    for alias in aliases
}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    cell_id: str
    first_timestamp: datetime | None
    delimiter: str
    encoding: str


@dataclass
class FileReport:
    source_file: str
    cell_id: str
    size_bytes: int
    rows_read: int = 0
    rows_state2: int = 0
    rows_written: int = 0
    malformed_rows: int = 0
    invalid_numeric_rows: int = 0
    invalid_datetime_rows: int = 0
    first_timestamp: str | None = None
    status: str = "ok"
    error: str | None = None
    examples: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    raw_dir: str
    output_dir: str
    started_at: str
    completed_at: str | None = None
    discovered_files: int = 0
    selected_files: int = 0
    excluded_files: list[str] = field(default_factory=list)
    ignored_derived_files: list[str] = field(default_factory=list)
    recovered_state2_files: list[str] = field(default_factory=list)
    superseded_files: list[str] = field(default_factory=list)
    files: list[FileReport] = field(default_factory=list)


def detect_encoding(path: Path) -> str:
    """Erkenne UTF-8; alte Exportdateien werden als Windows-1252 gelesen."""
    with path.open("rb") as handle:
        sample = handle.read(64 * 1024)
    try:
        sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8-sig"


def detect_delimiter(header: str) -> str:
    """Erkenne das Trennzeichen ohne Dezimalkommas als CSV-Trenner zu deuten."""
    counts = {delimiter: header.count(delimiter) for delimiter in ("\t", ";", ",")}
    delimiter = max(counts, key=counts.get)
    if counts[delimiter] == 0:
        raise ValueError("Kein unterstuetztes Trennzeichen in der Kopfzeile gefunden")
    return delimiter


def parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    for date_format in DATETIME_FORMATS:
        try:
            return datetime.strptime(value, date_format)
        except ValueError:
            pass
    return None


def normalize_number(value: str) -> str:
    """Normalisiere Dezimalkomma auf Punkt und validiere den Zahlenwert."""
    value = value.strip().replace("\u00a0", "").replace(" ", "")
    if not value:
        return ""
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Ungueltige Zahl: {value!r}") from exc
    return value


def canonical_cell_id(path: Path) -> str:
    """Leite die Zell-ID aus typischen Export- und Abschnittssuffixen ab."""
    stem = re.sub(r"_state2$", "", path.stem, flags=re.IGNORECASE)
    stem = re.sub(r"_retry(?:_komplett)?$", "", stem, flags=re.IGNORECASE)
    suffix = re.compile(
        r"_(?:Formierung|Lebensdauer(?:test)?(?:_Langzeit)?|Alterung|Ladetest)(?:_\d+)?$",
        flags=re.IGNORECASE,
    )
    while True:
        shortened = suffix.sub("", stem)
        if shortened == stem:
            return stem
        stem = shortened


def _open_text(path: Path, encoding: str) -> TextIO:
    return path.open("r", encoding=encoding, errors="strict", newline="")


def inspect_source(path: Path, scan_rows: int = 10_000) -> SourceFile:
    """Lese Header und ersten Zeitstempel, aber niemals die ganze Datei."""
    encoding = detect_encoding(path)
    with _open_text(path, encoding) as handle:
        header_line = handle.readline()
        if not header_line:
            raise ValueError("Datei ist leer")
        delimiter = detect_delimiter(header_line)
        handle.seek(0)
        reader = csv.reader(handle, delimiter=delimiter)
        raw_header = next(reader)
        canonical = [HEADER_ALIASES.get(_header_key(name), name.strip()) for name in raw_header]
        try:
            datetime_index = canonical.index("DateTime")
        except ValueError:
            datetime_index = -1
        first_timestamp = None
        if datetime_index >= 0:
            for row_number, row in enumerate(reader, start=1):
                if row_number > scan_rows:
                    break
                if datetime_index < len(row):
                    first_timestamp = parse_datetime(row[datetime_index])
                    if first_timestamp is not None:
                        break
    return SourceFile(path, canonical_cell_id(path), first_timestamp, delimiter, encoding)


def discover_sources(raw_dir: Path) -> tuple[list[Path], list[Path]]:
    """Finde Quelldateien rekursiv und trenne bereits abgeleitete Dateien ab."""
    discovered = sorted(
        path for path in raw_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in SUPPORTED_SUFFIXES
    )
    derived = [path for path in discovered if path.stem.casefold().endswith("_state2")]
    return [path for path in discovered if path not in derived], derived


def prefer_complete_retries(paths: Sequence[Path]) -> tuple[list[Path], list[Path]]:
    """Nutze bei vorhandenem *_retry_komplett die vollstaendige Ersatzdatei."""
    retries: dict[tuple[Path, str, str], Path] = {}
    for path in paths:
        base = re.sub(r"_retry_komplett$", "", path.stem, flags=re.IGNORECASE)
        if base != path.stem:
            retries[(path.parent, base.casefold(), path.suffix.casefold())] = path
    selected, superseded = [], []
    for path in paths:
        key = (path.parent, path.stem.casefold(), path.suffix.casefold())
        (superseded if key in retries and path != retries[key] else selected).append(path)
    return selected, superseded


def prefer_state2_fallbacks(
    paths: Sequence[Path], derived: Sequence[Path]
) -> tuple[list[Path], list[Path], list[Path], list[Path]]:
    """Nutze *_state2 als Rettungsdatei, wenn der Primaerexport kleiner ist.

    Ein vollstaendiger Rohdatenexport ist normalerweise wesentlich groesser als
    seine State-2-Ableitung. Ist dagegen die Ableitung groesser, handelt es sich
    in diesem Datenbestand um einen abgebrochenen Primaerexport, dessen
    gefilterte Daten separat gerettet wurden.
    """
    selected = list(paths)
    recovered: list[Path] = []
    ignored: list[Path] = []
    superseded: list[Path] = []

    def source_key(path: Path) -> tuple[Path, str, str]:
        stem = re.sub(r"_retry_komplett$", "", path.stem, flags=re.IGNORECASE)
        stem = re.sub(r"_state2$", "", stem, flags=re.IGNORECASE)
        return path.parent, stem.casefold(), path.suffix.casefold()

    for fallback in derived:
        key = source_key(fallback)
        primary = next((path for path in selected if source_key(path) == key), None)
        if primary is None or fallback.stat().st_size > primary.stat().st_size:
            if primary is not None:
                selected.remove(primary)
                superseded.append(primary)
            selected.append(fallback)
            recovered.append(fallback)
        else:
            ignored.append(fallback)
    return selected, recovered, ignored, superseded


def build_column_map(header: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, raw_name in enumerate(header):
        canonical = HEADER_ALIASES.get(_header_key(raw_name))
        if canonical is not None and canonical not in result:
            result[canonical] = index
    if "State" not in result:
        raise ValueError("Pflichtspalte 'State' fehlt")
    return result


def _remember_example(report: FileReport, row_number: int, reason: str) -> None:
    if len(report.examples) < 5:
        report.examples.append(f"Zeile {row_number}: {reason}")


def iter_state2_rows(source: SourceFile, report: FileReport) -> Iterator[dict[str, str]]:
    """Liefere validierte State-2-Zeilen einzeln und speicherschonend."""
    with _open_text(source.path, source.encoding) as handle:
        reader = csv.reader(handle, delimiter=source.delimiter)
        header = next(reader)
        column_map = build_column_map(header)
        expected_length = len(header)
        for row_number, row in enumerate(reader, start=2):
            report.rows_read += 1
            if len(row) != expected_length:
                report.malformed_rows += 1
                _remember_example(report, row_number, f"{len(row)} statt {expected_length} Felder")
                continue
            try:
                state = normalize_number(row[column_map["State"]])
                if not state or Decimal(state) != Decimal(2):
                    continue
            except (ValueError, InvalidOperation):
                report.invalid_numeric_rows += 1
                _remember_example(report, row_number, "State ist keine gueltige Zahl")
                continue
            report.rows_state2 += 1
            output = {column: "" for column in OUTPUT_COLUMNS}
            invalid = False
            for column in OUTPUT_COLUMNS:
                if column in {"SourceFile", "SourceRow"}:
                    continue
                index = column_map.get(column)
                if index is None:
                    continue
                value = row[index].strip()
                if column in NUMERIC_COLUMNS and value:
                    try:
                        value = normalize_number(value)
                    except ValueError:
                        report.invalid_numeric_rows += 1
                        _remember_example(report, row_number, f"{column} ist keine gueltige Zahl")
                        invalid = True
                        break
                output[column] = value
            if invalid:
                continue
            if "DateTime" in column_map:
                timestamp = parse_datetime(output["DateTime"])
                if timestamp is None:
                    report.invalid_datetime_rows += 1
                    _remember_example(report, row_number, "DateTime ist ungueltig")
                    continue
                output["DateTime"] = timestamp.isoformat(sep=" ")
            output["State"] = "2"
            output["SourceFile"] = source.path.as_posix()
            output["SourceRow"] = str(row_number)
            yield output


def safe_filename(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value).rstrip(". ")
    return value or "unbenannte_zelle"


def process_cell(cell_id: str, sources: Sequence[SourceFile], output_dir: Path) -> list[FileReport]:
    """Fuehre alle Abschnitte einer Zelle in zeitlicher Reihenfolge zusammen."""
    destination = output_dir / f"{safe_filename(cell_id)}_state2.tsv.gz"
    reports: list[FileReport] = []
    ordered = sorted(sources, key=lambda source: (
        source.first_timestamp is None, source.first_timestamp or datetime.max,
        source.path.name.casefold(),
    ))
    with gzip.open(destination, "wt", encoding="utf-8", newline="", compresslevel=6) as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_COLUMNS, delimiter="\t")
        writer.writeheader()
        for source in ordered:
            report = FileReport(
                source_file=str(source.path), cell_id=cell_id,
                size_bytes=source.path.stat().st_size,
                first_timestamp=source.first_timestamp.isoformat(sep=" ") if source.first_timestamp else None,
            )
            try:
                for output_row in iter_state2_rows(source, report):
                    writer.writerow(output_row)
                    report.rows_written += 1
            except (OSError, UnicodeError, csv.Error, ValueError) as exc:
                report.status, report.error = "error", str(exc)
                LOGGER.error("Fehler in %s: %s", source.path, exc)
            reports.append(report)
    return reports


def run(
    raw_dir: Path,
    output_dir: Path,
    report_path: Path | None = None,
    cell_scope: CellScope | None = None,
) -> RunReport:
    """Fuehre Discovery, Inspektion, Sortierung, Filterung und Export aus."""
    raw_dir, output_dir = raw_dir.resolve(), output_dir.resolve()
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"Rohdatenordner nicht gefunden: {raw_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = RunReport(str(raw_dir), str(output_dir), datetime.now().isoformat(timespec="seconds"))
    candidates, derived = discover_sources(raw_dir)
    report.discovered_files = len(candidates) + len(derived)
    candidates, superseded = prefer_complete_retries(candidates)
    candidates, recovered, ignored, fallback_superseded = prefer_state2_fallbacks(
        candidates, derived
    )
    report.ignored_derived_files = [str(path) for path in ignored]
    report.recovered_state2_files = [str(path) for path in recovered]
    report.superseded_files = [str(path) for path in (*superseded, *fallback_superseded)]
    if cell_scope is not None:
        included: list[Path] = []
        for path in candidates:
            if cell_scope.policy(canonical_cell_id(path)).enabled:
                included.append(path)
            else:
                report.excluded_files.append(str(path))
        candidates = included
    grouped: dict[str, list[SourceFile]] = defaultdict(list)
    for path in candidates:
        try:
            source = inspect_source(path)
        except (OSError, UnicodeError, csv.Error, ValueError) as exc:
            report.files.append(FileReport(
                str(path), canonical_cell_id(path), path.stat().st_size,
                status="error", error=str(exc),
            ))
            LOGGER.error("Datei kann nicht inspiziert werden: %s (%s)", path, exc)
            continue
        grouped[source.cell_id].append(source)
        report.selected_files += 1
    for cell_id in sorted(grouped, key=str.casefold):
        LOGGER.info("Verarbeite %s (%d Dateien)", cell_id, len(grouped[cell_id]))
        report.files.extend(process_cell(cell_id, grouped[cell_id], output_dir))
    report.completed_at = datetime.now().isoformat(timespec="seconds")
    report_path = report_path or output_dir / "raw_reader_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(asdict(report), handle, ensure_ascii=False, indent=2)
    return report


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Rohdaten je Zelle zusammenfuehren und auf State == 2 filtern.")
    parser.add_argument("--raw-dir", type=Path, default=root / "data" / "raw")
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "processed" / "state2")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--cells-config", type=Path, default=root / "config" / "cells.csv")
    parser.add_argument("--workbook", type=Path,
                        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx")
    parser.add_argument("--all-cells", action="store_true",
                        help="Konfiguration ignorieren und alle gefundenen Zellen verarbeiten")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    try:
        scope = None if args.all_cells else load_cell_scope(args.workbook, args.cells_config)
        report = run(args.raw_dir, args.output_dir, args.report, scope)
    except (OSError, ValueError) as exc:
        LOGGER.error("Abbruch: %s", exc)
        return 1
    errors = sum(file.status == "error" for file in report.files)
    written = sum(file.rows_written for file in report.files)
    LOGGER.info("Fertig: %d Dateien, %d State-2-Zeilen, %d Fehler", report.selected_files, written, errors)
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
