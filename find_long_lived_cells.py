"""Batterien mit mehr als einer vorgegebenen Zahl C/2-Zyklen auflisten."""

from __future__ import annotations

import argparse
import csv
import gzip
from pathlib import Path


def find_cells(input_dir: Path, threshold: int) -> list[tuple[str, int]]:
    """Liefere Zell-ID und maximalen echten C/2-Lebensdauerzyklus."""
    results: list[tuple[str, int]] = []
    for path in sorted(input_dir.glob("*_cycles.tsv.gz")):
        maximum = 0
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames or "Lifetime-Cycle" not in reader.fieldnames:
                raise ValueError(f"Spalte Lifetime-Cycle fehlt in {path}")
            for row in reader:
                value = row["Lifetime-Cycle"].strip()
                if value:
                    maximum = max(maximum, int(value))
        if maximum > threshold:
            cell_id = path.name.removesuffix("_cycles.tsv.gz")
            results.append((cell_id, maximum))
    return sorted(results, key=lambda item: (-item[1], item[0].casefold()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batterien oberhalb einer C/2-Zyklusschwelle auflisten."
    )
    parser.add_argument("--input-dir", type=Path, default=Path("data/processed/cycles"))
    parser.add_argument("--threshold", type=int, default=500)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/output/batteries_over_500_cycles.csv"),
    )
    args = parser.parse_args()

    cells = find_cells(args.input_dir, args.threshold)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(("Batterie", "Max. Lifetime-Cycle"))
        writer.writerows(cells)

    print("Batterie\tMax. Lifetime-Cycle")
    for cell_id, maximum in cells:
        print(f"{cell_id}\t{maximum}")
    print(f"\nAnzahl Batterien mit > {args.threshold} Zyklen: {len(cells)}")
    print(f"CSV-Tabelle: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
