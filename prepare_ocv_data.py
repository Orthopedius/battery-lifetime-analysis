"""Prepare the BaSyTec files that are candidates for an OCV time plot.

The selection is intentionally explicit. No State filtering is performed.
Files whose name contains a duration such as ``48h`` are truncated at that
elapsed time. Long-term exports are included because their beginning can
contain the OCV section; every file is stopped at its first nonzero current.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Sequence


DURATION_PATTERN = re.compile(r"(?<![\d,.])(?P<hours>\d+(?:[,.]\d+)?)h(?=[_.-]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class ProcessResult:
    source: str
    destination: str
    duration_hours: float | None
    rows_written: int
    rows_skipped_after_limit: int


@dataclass(frozen=True)
class DownsampleResult:
    source: str
    destination: str
    rows_read: int
    rows_written: int
    rows_skipped: int


def duration_from_name(name: str) -> Decimal | None:
    match = DURATION_PATTERN.search(name)
    if not match:
        return None
    try:
        return Decimal(match.group("hours").replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"Ungueltige Dauer im Dateinamen: {name}") from error


def selected_sources(raw_dir: Path) -> list[Path]:
    candidates = [path for path in raw_dir.rglob("*.txt") if _is_candidate(path.name)]
    selected: list[Path] = []
    for path in sorted(candidates, key=lambda item: item.name.casefold()):
        if "_retry_komplett" in path.stem.casefold():
            selected.append(path)
            continue
        if path.stem.casefold().endswith("_state2"):
            primary = path.with_name(path.stem[:-len("_state2")] + path.suffix)
            retry = path.with_name(path.stem[:-len("_state2")] + "_retry_komplett" + path.suffix)
            if (primary in candidates and primary.stat().st_size > 500) or retry in candidates:
                continue
        state2 = path.with_name(path.stem + "_state2" + path.suffix)
        if state2 in candidates and path.stat().st_size <= 500:
            continue
        retry = path.with_name(path.stem + "_retry_komplett" + path.suffix)
        if retry in candidates:
            continue
        selected.append(path)
    if not selected:
        raise FileNotFoundError("Keine OCV-Kandidaten in den Rohdaten gefunden")
    return selected


def _is_candidate(name: str) -> bool:
    lowered = name.casefold()
    return (
        name.casefold().endswith(".txt")
        and not lowered.endswith("_lebensdauer_lebensdauer.txt")
        and "_vz_" in lowered
        and any(token in lowered for token in ("preli", "formierung", "lebensdauer"))
    )


def _column_index(header: str, column_name: set[str]) -> int:
    columns = header.lstrip("\ufeff#~").split()
    for index, column in enumerate(columns):
        if column.casefold() in column_name:
            return index
    raise ValueError("Gesuchte BaSyTec-Spalte fehlt")


def _data_index(header: str, fields: list[str], column_index: int) -> int:
    columns = header.lstrip("\ufeff#~").split()
    if len(fields) == len(columns) + 1:
        datetime_index = _column_index(header, {"datetime"})
        if column_index > datetime_index:
            return column_index + 1
    return column_index


def _elapsed_hours(value: str, path: Path, line_number: int) -> Decimal:
    try:
        return Decimal(value.replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"Ungueltige Zeit in {path}, Zeile {line_number}: {value!r}") from error


def _current_index(header: str) -> int:
    return _column_index(header, {"i[a]", "current[a]"})


def _encoding(path: Path) -> str:
    sample = path.read_bytes()[:65536]
    try:
        sample.decode("utf-8-sig")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8-sig"


def process_file(source: Path, destination: Path) -> ProcessResult:
    limit = duration_from_name(source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    skipped = 0
    with source.open("r", encoding=_encoding(source), errors="strict", newline="") as input_handle:
        header = input_handle.readline()
        if not header:
            raise ValueError(f"Datei ist leer: {source}")
        time_index = _column_index(header, {"time[h]", "#time[h]", "~time[h]"}) if limit is not None else -1
        current_index = _current_index(header)
        with destination.open("w", encoding="utf-8", newline="") as output_handle:
            output_handle.write(header)
            for line_number, line in enumerate(input_handle, start=2):
                if line.strip():
                    fields = line.split()
                    row_current_index = _data_index(header, fields, current_index)
                    row_time_index = _data_index(header, fields, time_index) if limit is not None else -1
                    if len(fields) <= row_current_index or (limit is not None and len(fields) <= row_time_index):
                        skipped += 1
                        continue
                    if limit is not None and _elapsed_hours(fields[row_time_index], source, line_number) > limit:
                        skipped += 1
                        continue
                    try:
                        current = Decimal(fields[row_current_index].replace(",", "."))
                    except InvalidOperation as error:
                        raise ValueError(
                            f"Ungueltiger Strom in {source}, Zeile {line_number}: {fields[row_current_index]!r}"
                        ) from error
                    if current != 0:
                        skipped += 1
                        skipped += sum(1 for _ in input_handle)
                        break
                output_handle.write(line)
                rows_written += 1
    return ProcessResult(str(source), str(destination), float(limit) if limit is not None else None,
                         rows_written, skipped)


def run(raw_dir: Path, output_dir: Path) -> list[ProcessResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_file in output_dir.glob("*.txt"):
        old_file.unlink()
    results = [process_file(source, output_dir / source.name) for source in selected_sources(raw_dir)]
    output_dir.joinpath("ocv_processing_report.json").write_text(
        json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8"
    )
    return results


def downsample_file(source: Path, destination: Path) -> DownsampleResult:
    """Keep every 10 s for the first 10 min, then every 60 s."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows_read = rows_written = rows_skipped = 0
    next_sample: Decimal | None = None
    with source.open("r", encoding=_encoding(source), errors="strict", newline="") as input_handle:
        header = input_handle.readline()
        if not header:
            raise ValueError(f"Datei ist leer: {source}")
        time_index = _column_index(header, {"time[h]", "#time[h]", "~time[h]"})
        with destination.open("w", encoding="utf-8", newline="") as output_handle:
            output_handle.write(header)
            for line_number, line in enumerate(input_handle, start=2):
                if not line.strip():
                    continue
                fields = line.split()
                row_time_index = _data_index(header, fields, time_index)
                if len(fields) <= row_time_index:
                    rows_skipped += 1
                    continue
                elapsed = _elapsed_hours(fields[row_time_index], source, line_number)
                rows_read += 1
                if next_sample is None or elapsed >= next_sample:
                    output_handle.write(line)
                    rows_written += 1
                    initial_phase = elapsed * Decimal(3600) <= Decimal("600.5")
                    interval = Decimal(10 if initial_phase else 60)
                    next_sample = elapsed + interval / Decimal(3600)
                else:
                    rows_skipped += 1
    return DownsampleResult(str(source), str(destination), rows_read, rows_written, rows_skipped)


def downsample_directory(input_dir: Path, output_dir: Path) -> list[DownsampleResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_file in output_dir.glob("*.txt"):
        old_file.unlink()
    sources = sorted(input_dir.glob("*.txt"), key=lambda path: path.name.casefold())
    results = [downsample_file(source, output_dir / source.name) for source in sources]
    output_dir.joinpath("ocv_plot_processing_report.json").write_text(
        json.dumps([asdict(result) for result in results], indent=2), encoding="utf-8"
    )
    return results


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=root / "data" / "raw")
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "processed" / "ocv")
    parser.add_argument("--plot-dir", type=Path, default=root / "data" / "processed" / "ocv_plot")
    args = parser.parse_args(argv)
    try:
        results = run(args.raw_dir, args.output_dir)
        plot_results = downsample_directory(args.output_dir, args.plot_dir)
    except (OSError, ValueError) as error:
        print(f"OCV-Aufbereitung abgebrochen: {error}", file=sys.stderr)
        return 1
    print(f"{len(results)} OCV-Dateien nach {args.output_dir} geschrieben")
    print(f"{len(plot_results)} reduzierte Plot-Dateien nach {args.plot_dir} geschrieben")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())