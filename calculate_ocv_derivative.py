"""Calculate dU/dt for the reduced OCV files."""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from prepare_ocv_data import _column_index, _data_index, _encoding


def _number(value: str) -> float:
    return float(value.strip().replace(",", "."))


def load_ocv(path: Path) -> list[tuple[float, float]]:
    with path.open("r", encoding=_encoding(path), errors="strict", newline="") as handle:
        header = handle.readline()
        if not header:
            raise ValueError(f"Datei ist leer: {path}")
        time_index = _column_index(header, {"time[h]", "#time[h]", "~time[h]"})
        voltage_index = _column_index(header, {"u[v]", "voltage[v]"})
        values: list[tuple[float, float]] = []
        for line in handle:
            if not line.strip():
                continue
            fields = line.split()
            row_time = _data_index(header, fields, time_index)
            row_voltage = _data_index(header, fields, voltage_index)
            if len(fields) <= max(row_time, row_voltage):
                continue
            time, voltage = _number(fields[row_time]), _number(fields[row_voltage])
            if values and time <= values[-1][0]:
                continue
            values.append((time, voltage))
    return values


def derivative(values: list[tuple[float, float]]) -> list[tuple[float, float, float | None, str]]:
    if len(values) < 2:
        return [(time, voltage, None, "undefined") for time, voltage in values]
    result: list[tuple[float, float, float | None, str]] = []
    for index, (time, voltage) in enumerate(values):
        if index == 0:
            next_time, next_voltage = values[1]
            slope = (next_voltage - voltage) / (next_time - time)
            method = "forward"
        elif index == len(values) - 1:
            previous_time, previous_voltage = values[index - 1]
            slope = (voltage - previous_voltage) / (time - previous_time)
            method = "backward"
        else:
            previous_time, previous_voltage = values[index - 1]
            next_time, next_voltage = values[index + 1]
            slope = (next_voltage - previous_voltage) / (next_time - previous_time)
            method = "central"
        result.append((time, voltage, slope if slope == slope else None, method))
    return result


def process_file(source: Path, destination: Path) -> tuple[int, int]:
    values = load_ocv(source)
    calculated = derivative(values)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(("Zeit [h]", "Spannung [V]", "dU/dt [V/h]", "Methode"))
        for time, voltage, slope, method in calculated:
            value = "" if slope is None else f"{slope:.12g}"
            writer.writerow((f"{time:.12g}", f"{voltage:.12g}", value, method))
    return len(values), len(calculated)


def run(input_dir: Path, output_dir: Path) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    for old_file in output_dir.glob("*.tsv"):
        old_file.unlink()
    total = 0
    for source in sorted(input_dir.glob("*.txt"), key=lambda path: path.name.casefold()):
        read, written = process_file(source, output_dir / f"{source.stem}_derivative.tsv")
        total += written
    return total


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=root / "data" / "processed" / "ocv_plot")
    parser.add_argument("--output-dir", type=Path, default=root / "data" / "processed" / "ocv_derivative")
    args = parser.parse_args(argv)
    try:
        total = run(args.input_dir, args.output_dir)
    except (OSError, ValueError) as error:
        print(f"OCV-Ableitung abgebrochen: {error}", file=sys.stderr)
        return 1
    print(f"{total} Ableitungspunkte nach {args.output_dir} geschrieben")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())