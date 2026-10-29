"""Pipeline-weite Qualitaetspruefungen und lesbare Verarbeitungsberichte.

Die Pruefung verbindet die Ergebnisse von ``raw_reader.py``,
``cycle_reconstruction.py`` und ``capacity.py``. Sie veraendert keine
Messdaten. Wiederholte Befunde werden gebuendelt, damit auch grosse
Datensaetze einen uebersichtlichen Bericht ergeben.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Sequence

try:  # Paketimport in Tests und direkter Aufruf als Skript werden unterstuetzt.
    from .capacity import (
        MAX_PLAUSIBLE_SPECIFIC_CAPACITY,
        CellMass,
        build_mass_index,
        load_cell_masses,
    )
    from .cell_scope import load_cell_scope
except ImportError:  # pragma: no cover - nur bei ``python src/validation.py``
    from capacity import (  # type: ignore
        MAX_PLAUSIBLE_SPECIFIC_CAPACITY,
        CellMass,
        build_mass_index,
        load_cell_masses,
    )
    from cell_scope import load_cell_scope  # type: ignore


LOGGER = logging.getLogger(__name__)
ALLOWED_CLASSIFICATION_STATUS = {"valid", "warning", "invalid"}
ALLOWED_CAPACITY_TYPES = {"C/10", "C/2"}
IDENTITY_COLUMNS = ("SourceFile", "SourceRow", "DateTime", "State")
MAX_EXAMPLES = 5
MIN_CAPACITY_VALUES_PER_CELL = 10
DECIMAL_TOLERANCE = Decimal("1e-12")

STATE2_REQUIRED_COLUMNS = {
    "DateTime", "Line", "Command", "I[A]", "Ah-Step", "Cyc-Count",
    "State", "SourceFile", "SourceRow",
}
CYCLE_REQUIRED_COLUMNS = STATE2_REQUIRED_COLUMNS | {
    "BaSyTec-Cycle", "Protocol-Block", "Cycle-In-Block", "Protocol-Cycle", "Cycle-Type",
    "Lifetime-Cycle", "Checkpoint-Cycle", "Counter-Segment",
    "Classification-Status", "Classification-Evidence",
}
CAPACITY_REQUIRED_COLUMNS = {
    "Excel-Cell-ID", "Cycle-Cell-ID", "Cathode-Active-Mass[g]",
    "Counter-Segment", "BaSyTec-Cycle", "Protocol-Cycle", "Cycle-Type", "Lifetime-Cycle",
    "Discharge-Capacity[Ah]", "Discharge-Capacity[mAh]",
    "Specific-Discharge-Capacity[mAh/g]", "Classification-Status",
    "Capacity-Status", "Capacity-Note",
    "DateTime", "Line", "Command", "I[A]", "SourceFile", "SourceRow",
}


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    count: int = 0
    examples: list[str] = field(default_factory=list)


class Findings:
    """Aggregate equal findings and retain only a few concrete examples."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str, str], Finding] = {}

    def add(self, severity: str, code: str, message: str, example: str = "") -> None:
        key = (severity, code, message)
        finding = self._items.setdefault(key, Finding(severity, code, message))
        finding.count += 1
        if example and example not in finding.examples and len(finding.examples) < MAX_EXAMPLES:
            finding.examples.append(example)

    def items(self) -> list[Finding]:
        order = {"error": 0, "warning": 1, "info": 2}
        return sorted(self._items.values(), key=lambda item: (order[item.severity], item.code))

    def count(self, severity: str) -> int:
        return sum(item.count for item in self._items.values() if item.severity == severity)

    def groups(self, severity: str) -> int:
        return sum(item.severity == severity for item in self._items.values())


@dataclass(frozen=True)
class FileAudit:
    rows: int
    identity_digest: str


@dataclass(frozen=True)
class CycleAudit:
    rows: int
    identity_digest: str
    cycles: int


@dataclass(frozen=True)
class CycleRecord:
    cycle_type: str
    classification_status: str
    discharge_capacity_ah: Decimal | None = None
    source_file: str = ""
    source_row: str = ""


@dataclass
class ValidationReport:
    stats: dict[str, int | str]
    findings: Findings


def parse_decimal(value: str, *, integer: bool = False) -> Decimal | None:
    text = value.strip().replace("\u00a0", "")
    if not text:
        return None
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", ".")
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or (integer and number != number.to_integral_value()):
        return None
    return number


def parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, pattern)
            except ValueError:
                pass
    return None


def _cell_id(path: Path, suffix: str) -> str:
    if not path.name.endswith(suffix):
        raise ValueError(f"Unerwarteter Dateiname: {path.name}")
    return path.name[:-len(suffix)]


def _update_digest(digest: object, row: dict[str, str]) -> None:
    payload = "\x1f".join(row.get(column, "") for column in IDENTITY_COLUMNS) + "\n"
    digest.update(payload.encode("utf-8"))  # type: ignore[attr-defined]


def _check_columns(
    fieldnames: Sequence[str] | None,
    required: set[str],
    findings: Findings,
    code: str,
    filename: str,
) -> bool:
    missing = sorted(required.difference(fieldnames or ()))
    if missing:
        findings.add("error", code, "Pflichtspalten fehlen", f"{filename}: {', '.join(missing)}")
        return False
    return True


def scan_state2_file(path: Path, findings: Findings) -> FileAudit:
    digest = hashlib.sha256()
    rows = 0
    previous_datetime: datetime | None = None
    identities: set[tuple[str, str]] = set()
    checked_sources: dict[str, bool] = {}
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not _check_columns(reader.fieldnames, STATE2_REQUIRED_COLUMNS, findings,
                              "STATE2_COLUMNS", path.name):
            return FileAudit(0, digest.hexdigest())
        for line_number, row in enumerate(reader, start=2):
            rows += 1
            example = f"{path.name}: Zeile {line_number}"
            _update_digest(digest, row)
            if row["State"].strip() != "2":
                findings.add("error", "STATE_NOT_2", "State-2-Datei enthaelt State != 2", example)
            for column in ("Line", "Cyc-Count", "SourceRow"):
                value = parse_decimal(row[column], integer=True)
                if value is None or value < 0 or (column == "Cyc-Count" and value < 1):
                    findings.add("error", "STATE2_BAD_INTEGER",
                                 f"Ungueltiger positiver Ganzzahlwert in {column}", example)
            for column in ("I[A]", "Ah-Step"):
                if parse_decimal(row[column]) is None:
                    findings.add("error", "STATE2_BAD_NUMBER",
                                 f"Ungueltiger Zahlenwert in {column}", example)
            if not row["Command"].strip():
                findings.add("error", "STATE2_COMMAND_MISSING", "Command ist leer", example)
            source_file = row["SourceFile"].strip()
            if not source_file:
                findings.add("error", "STATE2_SOURCE_MISSING", "SourceFile ist leer", example)
            elif source_file not in checked_sources:
                checked_sources[source_file] = Path(source_file).is_file()
                if not checked_sources[source_file]:
                    findings.add("error", "STATE2_SOURCE_NOT_FOUND", "Referenzierte Rohdatei fehlt", source_file)
            timestamp = parse_datetime(row["DateTime"])
            if timestamp is None:
                findings.add("error", "STATE2_BAD_DATETIME", "Ungueltiger DateTime-Wert", example)
            elif previous_datetime is not None and timestamp < previous_datetime:
                findings.add("warning", "STATE2_TIME_ORDER",
                             "Zeitstempel innerhalb einer Zell-Datei fallen zurueck", example)
            if timestamp is not None:
                previous_datetime = timestamp
            identity = (row["SourceFile"], row["SourceRow"])
            if identity in identities:
                findings.add("error", "STATE2_DUPLICATE_SOURCE",
                             "Doppelte SourceFile/SourceRow-Kombination", example)
            identities.add(identity)
    if rows == 0:
        findings.add("warning", "STATE2_EMPTY", "State-2-Datei ist leer", path.name)
    return FileAudit(rows, digest.hexdigest())


def scan_state2_directory(
    directory: Path, findings: Findings, include_cells: set[str] | None = None
) -> dict[str, FileAudit]:
    result: dict[str, FileAudit] = {}
    for path in sorted(directory.glob("*_state2.tsv.gz"), key=lambda item: item.name.casefold()):
        cell = _cell_id(path, "_state2.tsv.gz")
        if include_cells is not None and cell not in include_cells:
            continue
        result[cell] = scan_state2_file(path, findings)
    if not result:
        findings.add("error", "STATE2_FILES_MISSING", "Keine State-2-Dateien gefunden", str(directory))
    return result


def scan_raw_reader_report(
    path: Path,
    state2: dict[str, FileAudit],
    findings: Findings,
    include_cells: set[str] | None = None,
) -> dict[str, int]:
    """Cross-check raw_reader counters against the materialized State-2 files."""
    with path.open("r", encoding="utf-8") as handle:
        report = json.load(handle)
    files = report.get("files")
    if not isinstance(files, list):
        findings.add("error", "RAW_REPORT_FORMAT", "raw_reader_report.json enthaelt keine Dateiliste", str(path))
        return {}
    totals: Counter[str] = Counter()
    written_by_cell: Counter[str] = Counter()
    scoped_files = 0
    recovered_paths = {str(value) for value in report.get("recovered_state2_files", [])}
    scoped_recovered = 0
    selected = report.get("selected_files")
    if isinstance(selected, int) and selected != len(files):
        findings.add("error", "RAW_REPORT_FILE_COUNT", "selected_files passt nicht zur Dateiliste", f"{selected} != {len(files)}")
    for item in files:
        if not isinstance(item, dict):
            findings.add("error", "RAW_REPORT_FORMAT", "Ungueltiger Dateieintrag im Raw-Reader-Bericht", repr(item))
            continue
        source = str(item.get("source_file", ""))
        cell = str(item.get("cell_id", ""))
        if include_cells is not None and cell not in include_cells:
            continue
        scoped_files += 1
        if source in recovered_paths:
            scoped_recovered += 1
        for column in (
            "rows_read", "rows_state2", "rows_written", "malformed_rows",
            "invalid_numeric_rows", "invalid_datetime_rows",
        ):
            value = item.get(column, 0)
            if not isinstance(value, int) or value < 0:
                findings.add("error", "RAW_REPORT_FORMAT", f"Ungueltiger Zaehler {column}", source)
            else:
                totals[column] += value
        if isinstance(item.get("rows_written"), int):
            written_by_cell[cell] += item["rows_written"]
        if item.get("status") != "ok" or item.get("error"):
            findings.add("error", "RAW_FILE_FAILED", "Rohdatei wurde nicht fehlerfrei verarbeitet", f"{source}: {item.get('error')}")
        rejected = sum(
            item.get(column, 0) if isinstance(item.get(column, 0), int) else 0
            for column in ("malformed_rows", "invalid_numeric_rows", "invalid_datetime_rows")
        )
        if rejected:
            findings.add("error", "RAW_ROWS_REJECTED", "Fehlerhafte Rohdatenzeilen wurden verworfen", f"{source}: {rejected}")
        if item.get("rows_state2") != item.get("rows_written"):
            findings.add("error", "RAW_STATE2_WRITE_LOSS", "State-2-Zeilen wurden nicht vollstaendig geschrieben", source)

    for cell in sorted(state2):
        if written_by_cell[cell] != state2[cell].rows:
            findings.add("error", "RAW_REPORT_ROW_MISMATCH",
                         "Raw-Reader-Bericht passt nicht zur State-2-Ausgabedatei",
                         f"{cell}: {written_by_cell[cell]} != {state2[cell].rows}")
    for cell in sorted(written_by_cell.keys() - state2.keys()):
        if written_by_cell[cell]:
            findings.add("error", "RAW_REPORT_OUTPUT_MISSING",
                         "Bericht nennt geschriebene Zeilen ohne State-2-Datei", cell)
    totals["files"] = scoped_files
    totals["recovered_state2_files"] = scoped_recovered
    return dict(totals)


def _validate_cycle_section(
    cell: str,
    rows: list[dict[str, str]],
    cycle_index: dict[tuple[str, str, str], CycleRecord],
    findings: Findings,
    previous_lifetime: int,
) -> int:
    first = rows[0]
    segment = first["Counter-Segment"].strip()
    counter = first["BaSyTec-Cycle"].strip()
    example = f"{cell}: Segment {segment}, BaSyTec-Cycle {counter}"
    metadata = (
        "Counter-Segment", "BaSyTec-Cycle", "Protocol-Block", "Cycle-In-Block",
        "Protocol-Cycle", "Cycle-Type", "Lifetime-Cycle", "Checkpoint-Cycle",
        "Classification-Status",
    )
    for column in metadata:
        if any(row[column] != first[column] for row in rows[1:]):
            findings.add("error", "CYCLE_METADATA_MIXED",
                         f"Metadaten innerhalb eines Zyklus wechseln: {column}", example)

    segment_number = parse_decimal(segment, integer=True)
    counter_number = parse_decimal(counter, integer=True)
    if segment_number is None or segment_number < 1 or counter_number is None or counter_number < 1:
        findings.add("error", "CYCLE_BAD_COUNTER", "Ungueltiger Segment-/Zykluszaehler", example)
        return previous_lifetime

    cycle_type = first["Cycle-Type"]
    block = parse_decimal(first["Protocol-Block"], integer=True)
    cycle_in_block = parse_decimal(first["Cycle-In-Block"], integer=True)
    protocol_cycle = parse_decimal(first["Protocol-Cycle"], integer=True)
    if cycle_type in {"C/10", "Diagnose", "C/2"}:
        if block is None or block < 1 or cycle_in_block is None or protocol_cycle is None:
            findings.add("error", "CYCLE_PROTOCOL_NUMBER_MISSING",
                         "Erkannter Lebensdauerzyklus ohne gueltige Protokollnummer", example)
        else:
            expected_protocol = (int(block) - 1) * 50 + int(cycle_in_block)
            if protocol_cycle != expected_protocol:
                findings.add("error", "CYCLE_PROTOCOL_MATH_MISMATCH",
                             "Protokollzyklus passt nicht zu Block und Position",
                             f"{example}: {protocol_cycle} != {expected_protocol}")
            valid_position = (
                cycle_type == "C/10" and cycle_in_block in {Decimal(1), Decimal(2)}
                or cycle_type == "Diagnose" and Decimal(3) <= cycle_in_block <= Decimal(5)
                or cycle_type == "C/2" and Decimal(6) <= cycle_in_block <= Decimal(50)
            )
            if not valid_position:
                findings.add("error", "CYCLE_TYPE_POSITION_MISMATCH",
                             "Zyklustyp passt nicht zur Position im 50er-Block", example)
    elif cycle_type not in {"Formation", "Unknown"}:
        findings.add("error", "CYCLE_BAD_TYPE", "Unbekannter Cycle-Type", f"{example}: {cycle_type}")

    status = first["Classification-Status"].strip()
    if status not in ALLOWED_CLASSIFICATION_STATUS:
        findings.add("error", "CYCLE_BAD_STATUS", "Unbekannter Classification-Status", f"{example}: {status}")
    elif status == "invalid":
        findings.add("error", "CYCLE_CLASSIFICATION_INVALID",
                     "Line/Command/I[A]-Pruefung klassifiziert Zyklus als invalid", example)
    elif status == "warning" and cycle_type != "Formation":
        findings.add("warning", "CYCLE_CLASSIFICATION_WARNING",
                     "Zyklusklassifikation enthaelt Warnungen", example)

    lifetime_text = first["Lifetime-Cycle"].strip()
    if cycle_type == "C/2":
        lifetime = parse_decimal(lifetime_text, integer=True)
        if lifetime is None or lifetime < 1:
            findings.add("error", "LIFETIME_CYCLE_MISSING", "C/2-Zyklus ohne echte Zyklusnummer", example)
        else:
            lifetime_int = int(lifetime)
            if block is not None and cycle_in_block is not None:
                expected_lifetime = (int(block) - 1) * 45 + int(cycle_in_block) - 5
                if lifetime_int != expected_lifetime:
                    findings.add("error", "LIFETIME_CYCLE_MISMATCH",
                                 "Lifetime-Cycle passt nicht zum Protokollblock",
                                 f"{example}: {lifetime_int} != {expected_lifetime}")
            if lifetime_int <= previous_lifetime:
                findings.add("error", "LIFETIME_NOT_INCREASING",
                             "Echte Zyklusnummer ist nicht streng steigend", example)
            previous_lifetime = max(previous_lifetime, lifetime_int)
    elif lifetime_text:
        findings.add("error", "LIFETIME_ON_NON_C2",
                     "C/10-/Diagnosezyklus besitzt eine Lifetime-Cycle-Nummer", example)

    key = (cell, segment, counter)
    if key in cycle_index:
        findings.add("error", "CYCLE_DUPLICATE_KEY", "Doppelter Zell-/Segment-/Cycle-Schluessel", example)
    candidates: list[tuple[Decimal, dict[str, str]]] = []
    for row in rows:
        current = parse_decimal(row["I[A]"])
        capacity = parse_decimal(row["Ah-Step"])
        if (row["Command"].strip().casefold() == "discharge"
                and current is not None and current < 0
                and capacity is not None and capacity != 0):
            candidates.append((abs(capacity), row))
    endpoint = max(candidates, key=lambda item: item[0]) if candidates else None
    cycle_index[key] = CycleRecord(
        first["Cycle-Type"], status,
        None if endpoint is None else endpoint[0],
        "" if endpoint is None else endpoint[1]["SourceFile"],
        "" if endpoint is None else endpoint[1]["SourceRow"],
    )
    return previous_lifetime


def scan_cycle_file(
    path: Path,
    findings: Findings,
    cycle_index: dict[tuple[str, str, str], CycleRecord],
) -> CycleAudit:
    cell = _cell_id(path, "_cycles.tsv.gz")
    digest = hashlib.sha256()
    rows = cycles = 0
    previous_lifetime = 0
    buffered: list[dict[str, str]] = []
    current_key: tuple[str, str] | None = None
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not _check_columns(reader.fieldnames, CYCLE_REQUIRED_COLUMNS, findings,
                              "CYCLE_COLUMNS", path.name):
            return CycleAudit(0, digest.hexdigest(), 0)
        for row in reader:
            rows += 1
            _update_digest(digest, row)
            key = (row["Counter-Segment"], row["BaSyTec-Cycle"])
            if current_key is not None and key != current_key:
                previous_lifetime = _validate_cycle_section(
                    cell, buffered, cycle_index, findings, previous_lifetime
                )
                cycles += 1
                buffered = []
            current_key = key
            buffered.append(row)
        if buffered:
            _validate_cycle_section(cell, buffered, cycle_index, findings, previous_lifetime)
            cycles += 1
    return CycleAudit(rows, digest.hexdigest(), cycles)


def scan_cycle_directory(
    directory: Path, findings: Findings, include_cells: set[str] | None = None
) -> tuple[dict[str, CycleAudit], dict[tuple[str, str, str], CycleRecord]]:
    audits: dict[str, CycleAudit] = {}
    cycle_index: dict[tuple[str, str, str], CycleRecord] = {}
    for path in sorted(directory.glob("*_cycles.tsv.gz"), key=lambda item: item.name.casefold()):
        cell = _cell_id(path, "_cycles.tsv.gz")
        if include_cells is not None and cell not in include_cells:
            continue
        audits[cell] = scan_cycle_file(path, findings, cycle_index)
    if not audits:
        findings.add("error", "CYCLE_FILES_MISSING", "Keine Cycle-Dateien gefunden", str(directory))
    return audits, cycle_index


def compare_state2_and_cycles(
    state2: dict[str, FileAudit], cycles: dict[str, CycleAudit], findings: Findings
) -> None:
    for cell in sorted(state2.keys() - cycles.keys()):
        findings.add("error", "CYCLE_FILE_MISSING", "State-2-Zelle ohne Cycle-Datei", cell)
    for cell in sorted(cycles.keys() - state2.keys()):
        findings.add("error", "CYCLE_FILE_ORPHAN", "Cycle-Datei ohne State-2-Datei", cell)
    for cell in sorted(state2.keys() & cycles.keys()):
        if state2[cell].rows != cycles[cell].rows:
            findings.add("error", "PIPELINE_ROW_LOSS",
                         "Zeilenzahl zwischen State-2 und Cycles ist verschieden",
                         f"{cell}: {state2[cell].rows} != {cycles[cell].rows}")
        if state2[cell].identity_digest != cycles[cell].identity_digest:
            findings.add("error", "PIPELINE_IDENTITY_CHANGED",
                         "Reihenfolge oder Quellidentitaet wurde zwischen State-2 und Cycles veraendert", cell)


def scan_capacity_file(
    path: Path,
    masses: Sequence[CellMass],
    cycle_index: dict[tuple[str, str, str], CycleRecord],
    findings: Findings,
    expect_longterm: dict[str, bool] | None = None,
) -> tuple[int, Counter[str]]:
    mass_index = build_mass_index(masses)
    rows = 0
    per_cell: Counter[str] = Counter()
    keys: set[tuple[str, str, str]] = set()
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if not _check_columns(reader.fieldnames, CAPACITY_REQUIRED_COLUMNS, findings,
                              "CAPACITY_COLUMNS", path.name):
            return 0, per_cell
        for line_number, row in enumerate(reader, start=2):
            rows += 1
            cycle_cell = row["Cycle-Cell-ID"].strip()
            excel_cell = row["Excel-Cell-ID"].strip()
            example = f"{cycle_cell}: Segment {row['Counter-Segment']}, Cycle {row['BaSyTec-Cycle']}"
            per_cell[excel_cell] += 1
            mass = mass_index.get(cycle_cell)
            if mass is None or mass.excel_cell_id != excel_cell:
                findings.add("error", "CAPACITY_CELL_MAPPING", "Kapazitaetszeile ist keiner Excel-Zelle eindeutig zugeordnet", example)
                continue

            key = (cycle_cell, row["Counter-Segment"], row["BaSyTec-Cycle"])
            if key in keys:
                findings.add("error", "CAPACITY_DUPLICATE_KEY", "Doppelter Kapazitaetsschluessel", example)
            keys.add(key)
            cycle = cycle_index.get(key)
            if cycle is None:
                findings.add("error", "CAPACITY_CYCLE_MISSING", "Kapazitaet verweist auf keinen rekonstruierten Zyklus", example)
            else:
                if row["Cycle-Type"] != cycle.cycle_type:
                    findings.add("error", "CAPACITY_TYPE_MISMATCH", "Cycle-Type unterscheidet sich von Cycle-Datei", example)
                if row["Classification-Status"] != cycle.classification_status:
                    findings.add("error", "CAPACITY_CLASSIFICATION_MISMATCH", "Classification-Status unterscheidet sich von Cycle-Datei", example)

            if row["Cycle-Type"] not in ALLOWED_CAPACITY_TYPES:
                findings.add("error", "DIAGNOSE_CAPACITY_EXPORTED", "Diagnose-/unbekannter Zyklus als Kapazitaet exportiert", example)
            active_mass = parse_decimal(row["Cathode-Active-Mass[g]"])
            capacity_ah = parse_decimal(row["Discharge-Capacity[Ah]"])
            capacity_mah = parse_decimal(row["Discharge-Capacity[mAh]"])
            specific = parse_decimal(row["Specific-Discharge-Capacity[mAh/g]"])
            if None in (active_mass, capacity_ah, capacity_mah, specific):
                findings.add("error", "CAPACITY_BAD_NUMBER", "Ungueltiger Zahlenwert in Kapazitaetszeile", f"{path.name}: Zeile {line_number}")
                continue
            assert active_mass is not None and capacity_ah is not None
            assert capacity_mah is not None and specific is not None
            if abs(active_mass - mass.active_mass_g) > DECIMAL_TOLERANCE:
                findings.add("error", "CAPACITY_MASS_MISMATCH", "Aktivmasse unterscheidet sich von Excel", example)
            if capacity_ah <= 0 or capacity_mah <= 0:
                findings.add("error", "CAPACITY_NON_POSITIVE", "Entladekapazitaet ist nicht positiv", example)
            if abs(capacity_ah * 1000 - capacity_mah) > DECIMAL_TOLERANCE:
                findings.add("error", "CAPACITY_UNIT_MISMATCH", "Ah-zu-mAh-Umrechnung ist inkonsistent", example)
            if abs(capacity_mah / active_mass - specific) > DECIMAL_TOLERANCE:
                findings.add("error", "CAPACITY_SPECIFIC_MISMATCH", "Spezifische Kapazitaet ist nicht reproduzierbar", example)
            if cycle is not None:
                if cycle.discharge_capacity_ah is None:
                    findings.add("error", "CAPACITY_ENDPOINT_MISSING", "Cycle-Datei besitzt keinen gueltigen Entladeendpunkt", example)
                elif abs(capacity_ah - cycle.discharge_capacity_ah) > DECIMAL_TOLERANCE:
                    findings.add("error", "CAPACITY_ENDPOINT_MISMATCH", "Kapazitaet entspricht nicht dem groessten Ah-Step-Entladeendpunkt", example)
                if (row["SourceFile"], row["SourceRow"]) != (cycle.source_file, cycle.source_row):
                    findings.add("error", "CAPACITY_SOURCE_MISMATCH", "Kapazitaet verweist nicht auf die gewaehlte Entladezeile", example)

            current = parse_decimal(row["I[A]"])
            if row["Command"].strip().casefold() != "discharge" or current is None or current >= 0:
                findings.add("error", "CAPACITY_NOT_DISCHARGE", "Kapazitaet stammt nicht aus Discharge mit negativem Strom", example)

            expected_status = (
                "valid" if Decimal(0) < specific <= MAX_PLAUSIBLE_SPECIFIC_CAPACITY
                and row["Classification-Status"] != "invalid" else "invalid"
            )
            if row["Capacity-Status"] != expected_status:
                findings.add("error", "CAPACITY_STATUS_MISMATCH", "Capacity-Status passt nicht zu Plausibilitaet/Klassifikation", example)
            if specific <= 0 or specific > MAX_PLAUSIBLE_SPECIFIC_CAPACITY:
                findings.add("error", "CAPACITY_IMPLAUSIBLE", "Spezifische Kapazitaet ausserhalb 0..350 mAh/g", f"{example}: {specific}")

    expected_cells = {mass.excel_cell_id for mass in masses}
    for cell in sorted(expected_cells - per_cell.keys()):
        longterm_expected = True if expect_longterm is None else expect_longterm.get(cell, True)
        if longterm_expected:
            findings.add("error", "CAPACITY_CELL_MISSING", "Excel-Zelle ohne Kapazitaetswert", cell)
        else:
            findings.add("info", "LONGTERM_NOT_EXPECTED",
                         "Keine Kapazitaetsreihe ist als erwartet konfiguriert", cell)
    for cell, count in sorted(per_cell.items()):
        longterm_expected = True if expect_longterm is None else expect_longterm.get(cell, True)
        if count < MIN_CAPACITY_VALUES_PER_CELL and longterm_expected:
            findings.add("warning", "CAPACITY_SERIES_SHORT",
                         f"Weniger als {MIN_CAPACITY_VALUES_PER_CELL} Kapazitaetswerte", f"{cell}: {count}")
        elif count < MIN_CAPACITY_VALUES_PER_CELL:
            findings.add("info", "LONGTERM_NOT_EXPECTED",
                         "Kurze Kapazitaetsreihe ist als erwartet konfiguriert", f"{cell}: {count}")
    return rows, per_cell


def scan_capacity_validation(
    path: Path, masses: Sequence[CellMass], findings: Findings
) -> int:
    expected = {mass.excel_cell_id for mass in masses}
    seen: set[str] = set()
    rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        required = {"excel_cell_id", "excel_formula_status", "formation_comparison_status"}
        if not _check_columns(reader.fieldnames, required, findings,
                              "CAPACITY_VALIDATION_COLUMNS", path.name):
            return 0
        for row in reader:
            rows += 1
            cell = row["excel_cell_id"]
            if cell in seen:
                findings.add("error", "CAPACITY_VALIDATION_DUPLICATE", "Doppelte Zelle im Kapazitaets-Pruefbericht", cell)
            seen.add(cell)
            for column in ("excel_formula_status", "formation_comparison_status"):
                status = row[column].strip().upper()
                if status == "FAIL":
                    findings.add("error", "REFERENCE_CHECK_FAILED", f"{column} ist FAIL", cell)
                elif status == "MISSING":
                    findings.add("error", "REFERENCE_CHECK_MISSING", f"{column} ist MISSING", cell)
                elif status != "PASS":
                    findings.add("error", "REFERENCE_CHECK_BAD_STATUS", f"Unbekannter Status in {column}", f"{cell}: {status}")
    for cell in sorted(expected - seen):
        findings.add("error", "REFERENCE_CELL_MISSING", "Excel-Zelle fehlt im Kapazitaets-Pruefbericht", cell)
    for cell in sorted(seen - expected):
        findings.add("error", "REFERENCE_CELL_ORPHAN", "Unbekannte Zelle im Kapazitaets-Pruefbericht", cell)
    return rows


def validate_pipeline(
    state2_dir: Path,
    cycles_dir: Path,
    capacity_file: Path,
    capacity_validation_file: Path,
    workbook: Path,
    raw_reader_report: Path | None = None,
    cells_config: Path | None = None,
) -> ValidationReport:
    findings = Findings()
    raw_reader_report = raw_reader_report or state2_dir / "raw_reader_report.json"
    cells_config = cells_config or Path(__file__).resolve().parents[1] / "config" / "cells.csv"
    for label, path in (
        ("State-2-Ordner", state2_dir), ("Cycle-Ordner", cycles_dir),
        ("Kapazitaetsdatei", capacity_file), ("Kapazitaets-Pruefbericht", capacity_validation_file),
        ("Aktivmassen-Excel", workbook), ("Raw-Reader-Bericht", raw_reader_report),
        ("Zellkonfiguration", cells_config),
    ):
        if not path.exists():
            findings.add("error", "INPUT_MISSING", f"{label} fehlt", str(path))
    if findings.count("error"):
        return ValidationReport({"status": "FAILED"}, findings)

    scope = load_cell_scope(workbook, cells_config)
    masses = scope.validation_masses()
    include_cells = scope.validation_ids()
    expect_longterm = {
        mass.excel_cell_id: scope.policy(mass.excel_cell_id).expect_longterm
        for mass in masses
    }
    state2 = scan_state2_directory(state2_dir, findings, include_cells)
    raw_stats = scan_raw_reader_report(raw_reader_report, state2, findings, include_cells)
    cycles, cycle_index = scan_cycle_directory(cycles_dir, findings, include_cells)
    compare_state2_and_cycles(state2, cycles, findings)
    capacity_rows, capacity_cells = scan_capacity_file(
        capacity_file, masses, cycle_index, findings, expect_longterm
    )
    reference_rows = scan_capacity_validation(capacity_validation_file, masses, findings)
    errors = findings.count("error")
    warnings = findings.count("warning")
    stats: dict[str, int | str] = {
        "status": "FAILED" if errors else ("WARNING" if warnings else "PASS"),
        "excel_cells": len(masses),
        "raw_files": raw_stats.get("files", 0),
        "raw_rows_read": raw_stats.get("rows_read", 0),
        "raw_rows_rejected": sum(raw_stats.get(key, 0) for key in (
            "malformed_rows", "invalid_numeric_rows", "invalid_datetime_rows"
        )),
        "recovered_state2_files": raw_stats.get("recovered_state2_files", 0),
        "state2_files": len(state2),
        "state2_rows": sum(item.rows for item in state2.values()),
        "cycle_files": len(cycles),
        "cycle_rows": sum(item.rows for item in cycles.values()),
        "cycles": sum(item.cycles for item in cycles.values()),
        "capacity_cells": len(capacity_cells),
        "capacity_rows": capacity_rows,
        "reference_rows": reference_rows,
        "error_occurrences": errors,
        "warning_occurrences": warnings,
        "error_groups": findings.groups("error"),
        "warning_groups": findings.groups("warning"),
    }
    return ValidationReport(stats, findings)


def write_reports(report: ValidationReport, output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    text_path = output_dir / "validation_report.txt"
    csv_path = output_dir / "validation_issues.csv"
    json_path = output_dir / "validation_report.json"

    lines = [
        "VALIDIERUNGSBERICHT LEBENSDAUER-AUSWERTUNG",
        "=" * 47,
        f"Gesamtstatus: {report.stats.get('status', 'FAILED')}",
        "",
        "KENNZAHLEN",
    ]
    lines.extend(f"- {key}: {value}" for key, value in report.stats.items() if key != "status")
    lines.extend(["", "BEFUNDE"])
    items = report.findings.items()
    if not items:
        lines.append("- Keine Auffaelligkeiten gefunden.")
    for item in items:
        lines.append(f"- [{item.severity.upper()}] {item.code} ({item.count}x): {item.message}")
        lines.extend(f"    Beispiel: {example}" for example in item.examples)
    # BOM sorgt dafuer, dass auch Windows PowerShell 5/Notepad Umlaute erkennt.
    text_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=("severity", "code", "count", "message", "examples"),
            delimiter=";",
        )
        writer.writeheader()
        for item in items:
            writer.writerow({
                "severity": item.severity,
                "code": item.code,
                "count": item.count,
                "message": item.message,
                "examples": " | ".join(item.examples),
            })

    json_path.write_text(json.dumps({
        "stats": report.stats,
        "findings": [asdict(item) for item in items],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return text_path, csv_path, json_path


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state2-dir", type=Path, default=root / "data" / "processed" / "state2")
    parser.add_argument("--cycles-dir", type=Path, default=root / "data" / "processed" / "cycles")
    parser.add_argument("--capacity-file", type=Path,
                        default=root / "data" / "processed" / "capacity" / "capacity_by_cycle.tsv.gz")
    parser.add_argument("--capacity-validation-file", type=Path,
                        default=root / "data" / "processed" / "capacity" / "capacity_validation.csv")
    parser.add_argument("--workbook", type=Path,
                        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx")
    parser.add_argument("--raw-reader-report", type=Path,
                        default=root / "data" / "processed" / "state2" / "raw_reader_report.json")
    parser.add_argument("--cells-config", type=Path, default=root / "config" / "cells.csv")
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = build_parser().parse_args(argv)
    try:
        report = validate_pipeline(
            args.state2_dir, args.cycles_dir, args.capacity_file,
            args.capacity_validation_file, args.workbook, args.raw_reader_report,
            args.cells_config,
        )
        paths = write_reports(report, args.output_dir)
    except (OSError, ValueError) as error:
        LOGGER.error("Validierung abgebrochen: %s", error)
        return 1
    LOGGER.info("Berichte: %s, %s, %s", *paths)
    LOGGER.info(
        "Status %s: %s Fehler, %s Warnungen",
        report.stats.get("status"), report.stats.get("error_occurrences", 0),
        report.stats.get("warning_occurrences", 0),
    )
    return 2 if report.findings.count("error") else 0


if __name__ == "__main__":
    sys.exit(main())
