"""BaSyTec-Teilprogrammzaehler zu echten Protokollzyklen rekonstruieren.

Die BaSyTec-Dateien enthalten keinen durchgehenden 50er-Zaehler. C/10,
Diagnose und C/2 laufen als Teilprogramme, deren ``Cyc-Count`` jeweils neu
beginnen kann. Deshalb wird der physikalische Zyklustyp zuerst aus ``Line``
erkannt und anschliessend unabhaengig mit ``Command`` und ``I[A]`` geprueft.
Erst danach entsteht die durchgehende Protokollnummer
1-2, 6-50, 51-52, 56-100, ...; Diagnose 3-5, 53-55, ... wird nicht als
Kapazitaetsmessung verwendet.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator, Sequence

try:
    from .cell_scope import CellScope, load_cell_scope
except ImportError:  # pragma: no cover - direkter Skriptaufruf
    from cell_scope import CellScope, load_cell_scope  # type: ignore


LOGGER = logging.getLogger(__name__)
BLOCK_SIZE = 50
C2_PER_BLOCK = 45
CURRENT_ZERO_TOLERANCE_A = Decimal("1e-12")

ADDED_COLUMNS = (
    "BaSyTec-Cycle",
    "Protocol-Block",
    "Cycle-In-Block",
    "Protocol-Cycle",
    "Cycle-Type",
    "Lifetime-Cycle",
    "Checkpoint-Cycle",
    "Counter-Segment",
    "Classification-Status",
    "Classification-Evidence",
)

# In den untersuchten BaSyTec-Exporten beobachtete Programmzeilen. Unbekannte
# Linien sind Warnungen, keine automatischen Fehler, damit neue Programme
# kontrolliert aufgenommen werden koennen.
EXPECTED_COMMAND_BY_LINE = {
    17: "charge", 18: "charge", 19: "discharge",
    21: "pause", 22: "charge", 23: "charge", 24: "pause", 25: "discharge",
    27: "charge", 28: "charge", 29: "discharge",
    32: "charge", 33: "charge", 34: "discharge",
}
SIGNATURE_LINES = {
    "C/10": {17, 18, 19},
    "Diagnose": {21, 22, 23, 24, 25, 27, 28, 29},
    "C/2": {32, 33, 34},
}
FORMATION_LINES = set(range(2, 14))


@dataclass(frozen=True)
class CyclePosition:
    basytec_cycle: int
    protocol_block: int
    cycle_in_block: int
    cycle_type: str
    c2_cycle_in_segment: int | None
    checkpoint_in_segment: int


@dataclass(frozen=True)
class ValidationResult:
    status: str
    evidence: str


def classify_counter(value: int) -> CyclePosition:
    """Klassifiziere einen positiven BaSyTec-Zaehler innerhalb des 50er-Schemas."""
    if value < 1:
        raise ValueError(f"Cyc-Count muss positiv sein, erhalten: {value}")
    protocol_block, zero_based = divmod(value - 1, BLOCK_SIZE)
    cycle_in_block = zero_based + 1
    if cycle_in_block <= 2:
        cycle_type = "C/10"
        c2_cycle = None
    elif cycle_in_block <= 5:
        cycle_type = "Diagnose"
        c2_cycle = None
    else:
        cycle_type = "C/2"
        c2_cycle = protocol_block * C2_PER_BLOCK + cycle_in_block - 5
    return CyclePosition(
        basytec_cycle=value,
        protocol_block=protocol_block + 1,
        cycle_in_block=cycle_in_block,
        cycle_type=cycle_type,
        c2_cycle_in_segment=c2_cycle,
        checkpoint_in_segment=protocol_block * C2_PER_BLOCK,
    )


def _integer(value: str, column: str) -> int:
    try:
        number = Decimal(value.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"{column} ist keine Zahl: {value!r}") from exc
    integer = int(number)
    if number != integer:
        raise ValueError(f"{column} ist nicht ganzzahlig: {value!r}")
    return integer


def _decimal(value: str, column: str) -> Decimal:
    try:
        return Decimal(value.strip().replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError(f"{column} ist keine Zahl: {value!r}") from exc


def validate_cycle_rows(cycle_type: str, rows: Sequence[dict[str, str]]) -> ValidationResult:
    """Pruefe die erwartete Klasse anhand von Line, Command und Stromrichtung."""
    errors: list[str] = []
    warnings: list[str] = []
    observed_lines: set[int] = set()
    checked_rows = 0

    for row in rows:
        line_text = row.get("Line", "").strip()
        command = row.get("Command", "").strip().casefold()
        current_text = row.get("I[A]", "").strip()
        if not line_text or not command or not current_text:
            warnings.append("Line/Command/I[A] unvollstaendig")
            continue
        try:
            line = _integer(line_text, "Line")
            current = _decimal(current_text, "I[A]")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        checked_rows += 1
        observed_lines.add(line)

        expected_command = EXPECTED_COMMAND_BY_LINE.get(line)
        if expected_command and command != expected_command:
            warnings.append(f"Line {line}: {command} statt {expected_command}")

        if command == "charge" and current <= CURRENT_ZERO_TOLERANCE_A:
            errors.append(f"Line {line}: Charge ohne positiven Strom")
        elif command == "discharge" and current >= -CURRENT_ZERO_TOLERANCE_A:
            errors.append(f"Line {line}: Discharge ohne negativen Strom")
        elif command in {"pause", "calconce"} and abs(current) > CURRENT_ZERO_TOLERANCE_A:
            errors.append(f"Line {line}: {command} mit I={current} A")

    signature = SIGNATURE_LINES[cycle_type]
    matched = sorted(observed_lines & signature)
    if not matched:
        warnings.append(
            f"keine typische {cycle_type}-Linie aus {sorted(signature)} gefunden"
        )
    if checked_rows == 0:
        errors.append("keine pruefbare Zeile vorhanden")

    # Wiederholungen derselben Meldung machen den Bericht sonst unleserlich.
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))
    details = [f"Signaturlinien={matched}"] + errors + warnings
    if errors:
        status = "invalid"
    elif warnings:
        status = "warning"
    else:
        status = "valid"
    return ValidationResult(status, "; ".join(details))


def infer_cycle_type(rows: Sequence[dict[str, str]]) -> ValidationResult:
    """Erkenne den Teilprogrammtyp anhand seiner charakteristischen Linien.

    Die Reihenfolge der Tests ist absichtlich C/2, C/10, Diagnose. In seltenen
    unvollstaendigen Exporten kann der letzte Datensatz eines Teilprogramms und
    der Anfang des naechsten denselben Zaehler tragen. Die fuer die
    Kapazitaet relevante C/2- bzw. C/10-Signatur hat dann Vorrang; die spaetere
    Validierung dokumentiert alle beobachteten Linien.
    """
    observed: set[int] = set()
    for row in rows:
        text = row.get("Line", "").strip()
        if not text:
            continue
        try:
            observed.add(_integer(text, "Line"))
        except ValueError as exc:
            return ValidationResult("Unknown", str(exc))

    for cycle_type in ("C/2", "C/10", "Diagnose"):
        matched = sorted(observed & SIGNATURE_LINES[cycle_type])
        if matched:
            return ValidationResult(cycle_type, f"Typ aus Line erkannt: {matched}")
    if observed and observed <= FORMATION_LINES:
        return ValidationResult("Formation", f"Formierungslinien: {sorted(observed)}")
    return ValidationResult("Unknown", f"keine bekannte Signaturlinie: {sorted(observed)}")


def _rows_by_consecutive_cycle(
    reader: Iterable[dict[str, str]],
) -> Iterator[tuple[int, list[dict[str, str]]]]:
    """Puffere nur einen Zyklus; Zaehlerwiederholungen nach Reset bleiben getrennt."""
    current_cycle: int | None = None
    buffered: list[dict[str, str]] = []
    for row in reader:
        cycle = _integer(row.get("Cyc-Count", ""), "Cyc-Count")
        if current_cycle is not None and cycle != current_cycle:
            yield current_cycle, buffered
            buffered = []
        current_cycle = cycle
        buffered.append(row)
    if current_cycle is not None:
        yield current_cycle, buffered


def reconstruct_rows(
    reader: Iterable[dict[str, str]],
) -> Iterator[dict[str, str]]:
    """Ergaenze physikalischen Typ, Protokollnummer und Reset-Segment."""
    segment = 1
    previous_counter: int | None = None
    previous_protocol_type: str | None = None
    protocol_block = 0
    c10_ordinal = 0
    diagnosis_ordinal = 0

    for counter, rows in _rows_by_consecutive_cycle(reader):
        if previous_counter is not None and counter < previous_counter:
            segment += 1

        inference = infer_cycle_type(rows)
        cycle_type = inference.status
        cycle_in_block: int | None = None
        protocol_cycle: int | None = None
        lifetime_cycle: int | None = None
        checkpoint: int | None = None

        if cycle_type == "C/10":
            if protocol_block == 0 or previous_protocol_type == "C/2":
                protocol_block += 1
                c10_ordinal = 0
                diagnosis_ordinal = 0
            c10_ordinal += 1
            cycle_in_block = counter if counter in (1, 2) else min(c10_ordinal, 2)
        elif cycle_type == "Diagnose" and protocol_block:
            diagnosis_ordinal += 1
            cycle_in_block = min(2 + diagnosis_ordinal, 5)
        elif cycle_type == "C/2" and protocol_block:
            cycle_in_block = 5 + counter

        range_error = ""
        if cycle_type == "C/2" and not 1 <= counter <= C2_PER_BLOCK:
            range_error = f"C/2-Teilprogrammzaehler ausserhalb 1..45: {counter}"
        elif cycle_type in {"C/10", "Diagnose", "C/2"} and not protocol_block:
            range_error = "Lebensdauerteilprogramm beginnt ohne erkennbaren C/10-Block"

        if cycle_in_block is not None and protocol_block:
            protocol_cycle = (protocol_block - 1) * BLOCK_SIZE + cycle_in_block
            checkpoint = (protocol_block - 1) * C2_PER_BLOCK
            if cycle_type == "C/2":
                lifetime_cycle = checkpoint + counter

        if cycle_type in SIGNATURE_LINES:
            validation = validate_cycle_rows(cycle_type, rows)
            evidence = f"{inference.evidence}; {validation.evidence}"
            status = "invalid" if range_error else validation.status
        elif cycle_type == "Formation":
            status = "warning"
            evidence = f"{inference.evidence}; ausserhalb der Lebensdauerauswertung"
        else:
            status = "invalid"
            evidence = inference.evidence
        if range_error:
            evidence = f"{evidence}; {range_error}"

        additions = {
            "BaSyTec-Cycle": str(counter),
            "Protocol-Block": "" if not protocol_block else str(protocol_block),
            "Cycle-In-Block": "" if cycle_in_block is None else str(cycle_in_block),
            "Protocol-Cycle": "" if protocol_cycle is None else str(protocol_cycle),
            "Cycle-Type": cycle_type,
            "Lifetime-Cycle": "" if lifetime_cycle is None else str(lifetime_cycle),
            "Checkpoint-Cycle": "" if checkpoint is None else str(checkpoint),
            "Counter-Segment": str(segment),
            "Classification-Status": status,
            "Classification-Evidence": evidence,
        }
        for row in rows:
            yield {**row, **additions}
        if cycle_type in SIGNATURE_LINES:
            previous_protocol_type = cycle_type
        previous_counter = counter


def reconstruct_file(source: Path, destination: Path) -> dict[str, int]:
    """Verarbeite eine komprimierte State-2-Datei zeilenweise."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    counts = {"rows": 0, "valid": 0, "warning": 0, "invalid": 0}
    with gzip.open(source, "rt", encoding="utf-8", newline="") as input_handle:
        reader = csv.DictReader(input_handle, delimiter="\t")
        if not reader.fieldnames or "Cyc-Count" not in reader.fieldnames:
            raise ValueError(f"Cyc-Count fehlt in {source}")
        fieldnames = list(reader.fieldnames) + [
            column for column in ADDED_COLUMNS if column not in reader.fieldnames
        ]
        with gzip.open(destination, "wt", encoding="utf-8", newline="") as output_handle:
            writer = csv.DictWriter(output_handle, fieldnames=fieldnames, delimiter="\t")
            writer.writeheader()
            previous_identity: tuple[str, str, str] | None = None
            for row in reconstruct_rows(reader):
                writer.writerow(row)
                counts["rows"] += 1
                identity = (
                    row["Counter-Segment"], row["BaSyTec-Cycle"],
                    row["Classification-Status"],
                )
                if identity != previous_identity:
                    counts[row["Classification-Status"]] += 1
                    previous_identity = identity
    return counts


def run(
    input_dir: Path, output_dir: Path, cell_scope: CellScope | None = None
) -> dict[str, dict[str, int]]:
    input_dir, output_dir = input_dir.resolve(), output_dir.resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Eingabeordner nicht gefunden: {input_dir}")
    sources = sorted(input_dir.glob("*_state2.tsv.gz"))
    if cell_scope is not None:
        suffix = "_state2.tsv.gz"
        sources = [
            source for source in sources
            if cell_scope.policy(source.name[:-len(suffix)]).enabled
        ]
    results: dict[str, dict[str, int]] = {}
    for source in sources:
        destination = output_dir / source.name.replace("_state2.tsv.gz", "_cycles.tsv.gz")
        LOGGER.info("Rekonstruiere %s", source.name)
        results[source.name] = reconstruct_file(source, destination)
    return results


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="BaSyTec-Zyklen rekonstruieren und validieren.")
    parser.add_argument("--input-dir", type=Path, default=root / "data" / "processed" / "state2")
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "processed" / "cycles")
    parser.add_argument("--cells-config", type=Path, default=root / "config" / "cells.csv")
    parser.add_argument("--workbook", type=Path,
                        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx")
    parser.add_argument("--all-cells", action="store_true")
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
        results = run(args.input_dir, args.output_dir, scope)
    except (OSError, ValueError) as exc:
        LOGGER.error("Abbruch: %s", exc)
        return 1
    files = len(results)
    cycles = sum(sum(v[key] for key in ("valid", "warning", "invalid")) for v in results.values())
    invalid = sum(v["invalid"] for v in results.values())
    LOGGER.info("Fertig: %d Dateien, %d Zyklen, %d ungueltig", files, cycles, invalid)
    return 2 if invalid else 0


if __name__ == "__main__":
    sys.exit(main())
