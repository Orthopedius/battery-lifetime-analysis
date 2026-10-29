"""Inventar der Roh- und Ergebnisdaten erstellen, ohne Rohdaten zu veraendern."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Sequence

try:
    from .cell_scope import load_cell_scope
    from .raw_reader import (
        canonical_cell_id,
        discover_sources,
        prefer_complete_retries,
        prefer_state2_fallbacks,
    )
except ImportError:  # pragma: no cover
    from cell_scope import load_cell_scope  # type: ignore
    from raw_reader import (  # type: ignore
        canonical_cell_id,
        discover_sources,
        prefer_complete_retries,
        prefer_state2_fallbacks,
    )


LOGGER = logging.getLogger(__name__)
FIELDS = (
    "cell_id", "excel_cell_id", "target_cell", "enabled", "include_validation",
    "expect_longterm", "policy_note", "source_files", "lifetime_source_files",
    "explicit_longterm_files", "raw_rows_read", "state2_rows", "cycle_sections",
    "c2_cycles", "max_lifetime_cycle", "capacity_values", "has_longterm_data",
    "fingerprint", "change_status", "recommended_action",
)


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _fingerprint(entries: list[dict[str, object]]) -> str:
    """Fast metadata fingerprint; it reads no measurement content."""
    parts: list[str] = []
    for entry in sorted(entries, key=lambda item: str(item.get("source_file", "")).casefold()):
        path = Path(str(entry.get("source_file", "")))
        try:
            stat = path.stat()
            parts.append(f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}")
        except OSError:
            parts.append(f"{path}|MISSING|{entry.get('size_bytes', '')}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _load_baseline(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(row["cell_id"]): str(row["fingerprint"])
        for row in payload.get("cells", [])
        if isinstance(row, dict) and row.get("cell_id") and row.get("fingerprint")
    }


def _cycle_metrics(cycles_dir: Path) -> dict[str, dict[str, int]]:
    metrics: dict[str, dict[str, int]] = {}
    for path in cycles_dir.glob("*_cycles.tsv.gz"):
        cell = path.name[:-len("_cycles.tsv.gz")]
        seen: set[tuple[str, str]] = set()
        sections = c2 = maximum = 0
        with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                key = (row.get("Counter-Segment", ""), row.get("BaSyTec-Cycle", ""))
                if key in seen:
                    continue
                seen.add(key)
                sections += 1
                if row.get("Cycle-Type") == "C/2":
                    c2 += 1
                    text = row.get("Lifetime-Cycle", "").strip()
                    if text.isdigit():
                        maximum = max(maximum, int(text))
        metrics[cell] = {"cycle_sections": sections, "c2_cycles": c2, "max_lifetime_cycle": maximum}
    return metrics


def _capacity_counts(path: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            counts[row.get("Cycle-Cell-ID", "")] += 1
    return counts


def build_inventory(
    raw_dir: Path,
    raw_report: Path,
    cycles_dir: Path,
    capacity_file: Path,
    workbook: Path,
    cells_config: Path,
    previous_fingerprints: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    scope = load_cell_scope(workbook, cells_config)
    report = json.loads(raw_report.read_text(encoding="utf-8"))
    report_by_path = {
        str(Path(str(entry.get("source_file", ""))).resolve()).casefold(): entry
        for entry in report.get("files", [])
        if isinstance(entry, dict) and entry.get("source_file")
    }
    candidates, derived = discover_sources(raw_dir)
    candidates, _ = prefer_complete_retries(candidates)
    candidates, _, _, _ = prefer_state2_fallbacks(candidates, derived)
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for path in candidates:
        key = str(path.resolve()).casefold()
        prior = report_by_path.get(key, {})
        entry: dict[str, object] = {
            "source_file": str(path),
            "cell_id": canonical_cell_id(path),
            "size_bytes": path.stat().st_size,
            "rows_read": prior.get("rows_read", 0),
            "rows_written": prior.get("rows_written", 0),
        }
        grouped[str(entry["cell_id"])].append(entry)
    for mass in scope.masses:
        if not any(scope.policy(alias).excel_cell_id == mass.excel_cell_id for alias in grouped):
            grouped.setdefault(mass.excel_cell_id, [])

    cycle_metrics = _cycle_metrics(cycles_dir)
    capacity_counts = _capacity_counts(capacity_file)
    previous_fingerprints = previous_fingerprints or {}
    rows: list[dict[str, object]] = []
    for cell in sorted(grouped, key=str.casefold):
        entries = grouped[cell]
        policy = scope.policy(cell)
        names = [Path(str(entry.get("source_file", ""))).name for entry in entries]
        lifetime = [name for name in names if any(
            token in Path(name).stem.casefold() for token in ("lebensdauer", "langzeit", "alterung")
        )]
        explicit_longterm = [name for name in names if "langzeit" in Path(name).stem.casefold()]
        metrics = cycle_metrics.get(cell, {"cycle_sections": 0, "c2_cycles": 0, "max_lifetime_cycle": 0})
        capacity_values = capacity_counts[cell]
        has_longterm = metrics["max_lifetime_cycle"] >= 10 or capacity_values >= 10
        fingerprint = _fingerprint(entries)
        previous = previous_fingerprints.get(cell)
        change = "baseline" if not previous_fingerprints else (
            "new" if previous is None else ("unchanged" if previous == fingerprint else "changed")
        )
        if not policy.enabled:
            action = "exclude"
        elif not policy.expect_longterm:
            action = "formation_only_expected"
        elif not has_longterm:
            action = "check_missing_longterm"
        elif change in {"new", "changed"}:
            action = "process_new_or_changed"
        else:
            action = "process"
        rows.append({
            "cell_id": cell,
            "excel_cell_id": policy.excel_cell_id or "",
            "target_cell": _bool(policy.excel_cell_id is not None),
            "enabled": _bool(policy.enabled),
            "include_validation": _bool(policy.include_validation),
            "expect_longterm": _bool(policy.expect_longterm),
            "policy_note": policy.note,
            "source_files": len(entries),
            "lifetime_source_files": len(lifetime),
            "explicit_longterm_files": len(explicit_longterm),
            "raw_rows_read": sum(int(entry.get("rows_read", 0)) for entry in entries),
            "state2_rows": sum(int(entry.get("rows_written", 0)) for entry in entries),
            **metrics,
            "capacity_values": capacity_values,
            "has_longterm_data": _bool(has_longterm),
            "fingerprint": fingerprint,
            "change_status": change,
            "recommended_action": action,
        })
    return rows


def write_inventory(rows: list[dict[str, object]], csv_path: Path, json_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cells": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=root / "data" / "raw")
    parser.add_argument("--raw-report", type=Path,
                        default=root / "data" / "processed" / "state2" / "raw_reader_report.json")
    parser.add_argument("--cycles-dir", type=Path, default=root / "data" / "processed" / "cycles")
    parser.add_argument("--capacity-file", type=Path,
                        default=root / "data" / "processed" / "capacity" / "capacity_by_cycle.tsv.gz")
    parser.add_argument("--workbook", type=Path,
                        default=root / "template" / "Zelldaten_mit_spezifischer_Kapazitaet.xlsx")
    parser.add_argument("--cells-config", type=Path, default=root / "config" / "cells.csv")
    parser.add_argument("--csv-output", type=Path, default=root / "data" / "output" / "raw_data_inventory.csv")
    parser.add_argument("--json-output", type=Path, default=root / "data" / "output" / "raw_data_inventory.json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = build_parser().parse_args(argv)
    try:
        baseline = _load_baseline(args.json_output)
        rows = build_inventory(
            args.raw_dir, args.raw_report, args.cycles_dir, args.capacity_file,
            args.workbook, args.cells_config, baseline,
        )
        write_inventory(rows, args.csv_output, args.json_output)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        LOGGER.error("Inventar konnte nicht erstellt werden: %s", error)
        return 1
    counts = Counter(str(row["recommended_action"]) for row in rows)
    LOGGER.info("Inventar: %d Zellen, %s", len(rows), dict(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
