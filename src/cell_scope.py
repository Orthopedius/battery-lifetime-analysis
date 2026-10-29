"""Gemeinsame Auswahlregeln fuer Import, Inventar und Validierung."""

from __future__ import annotations

import csv
import fnmatch
from dataclasses import dataclass
from pathlib import Path

try:
    from .capacity import CellMass, cycle_aliases, load_cell_masses
except ImportError:  # pragma: no cover - direkter Skriptaufruf aus ``src``
    from capacity import CellMass, cycle_aliases, load_cell_masses  # type: ignore


@dataclass(frozen=True)
class ScopeRule:
    cell_pattern: str
    enabled: bool | None
    include_validation: bool | None
    expect_longterm: bool | None
    note: str


@dataclass(frozen=True)
class CellPolicy:
    cell_id: str
    excel_cell_id: str | None
    enabled: bool
    include_validation: bool
    expect_longterm: bool
    note: str


def _optional_bool(value: str) -> bool | None:
    text = value.strip().casefold()
    if not text:
        return None
    if text in {"1", "true", "yes", "ja", "j"}:
        return True
    if text in {"0", "false", "no", "nein", "n"}:
        return False
    raise ValueError(f"Ungueltiger Bool-Wert in cells.csv: {value!r}")


def load_rules(path: Path) -> list[ScopeRule]:
    if not path.is_file():
        raise FileNotFoundError(f"Zellkonfiguration nicht gefunden: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        required = {"cell_pattern", "enabled", "include_validation", "expect_longterm", "note"}
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"cells.csv: fehlende Spalten: {', '.join(sorted(missing))}")
        rules = []
        for line_number, row in enumerate(reader, start=2):
            pattern = row["cell_pattern"].strip()
            if not pattern or pattern.startswith("#"):
                continue
            rules.append(ScopeRule(
                pattern,
                _optional_bool(row["enabled"]),
                _optional_bool(row["include_validation"]),
                _optional_bool(row["expect_longterm"]),
                row["note"].strip(),
            ))
    return rules


class CellScope:
    """Resolve defaults from the Excel target list and explicit CSV overrides."""

    def __init__(self, masses: list[CellMass], rules: list[ScopeRule]) -> None:
        self.masses = masses
        self.rules = rules
        self._mass_by_alias = {
            alias: mass for mass in masses for alias in cycle_aliases(mass.excel_cell_id)
        }

    def policy(self, cell_id: str) -> CellPolicy:
        mass = self._mass_by_alias.get(cell_id)
        target = mass is not None
        enabled = include_validation = expect_longterm = target
        notes: list[str] = []
        names = (cell_id,) if mass is None else (cell_id, mass.excel_cell_id)
        for rule in self.rules:
            if not any(fnmatch.fnmatchcase(name.casefold(), rule.cell_pattern.casefold()) for name in names):
                continue
            if rule.enabled is not None:
                enabled = rule.enabled
            if rule.include_validation is not None:
                include_validation = rule.include_validation
            if rule.expect_longterm is not None:
                expect_longterm = rule.expect_longterm
            if rule.note:
                notes.append(rule.note)
        return CellPolicy(
            cell_id=cell_id,
            excel_cell_id=None if mass is None else mass.excel_cell_id,
            enabled=enabled,
            include_validation=include_validation,
            expect_longterm=expect_longterm,
            note="; ".join(dict.fromkeys(notes)),
        )

    def validation_ids(self) -> set[str]:
        return {
            alias
            for mass in self.masses
            for alias in cycle_aliases(mass.excel_cell_id)
            if self.policy(alias).include_validation
        }

    def validation_masses(self) -> list[CellMass]:
        return [mass for mass in self.masses if self.policy(mass.excel_cell_id).include_validation]


def load_cell_scope(workbook: Path, config: Path) -> CellScope:
    return CellScope(load_cell_masses(workbook), load_rules(config))
