"""Vier Lebensdauerplots nach dem Vorbild von ``Vollzellen - Lebensdauer.ipynb``.

Die Datei liest die bereits validierten Kapazitaeten aus
``data/processed/capacity/capacity_by_cycle.tsv.gz``. Dadurch werden weder
Rohdaten erneut gefiltert noch Aktivmassen, Zellen oder Zaehlerluecken im
Plotcode dupliziert.

Standardausgabe:

    python plot_lifetime_data.py

Exakt die alten Achsgrenzen (x: 0..470; Kapazitaet: 100..180; SOH: 72..102):

    python plot_lifetime_data.py --original-limits

Die SVG-Dateien werden nach ``data/output/plots`` geschrieben.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import logging
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


LOGGER = logging.getLogger(__name__)
VALID_RATES = {"C/2", "C/10"}
CAPACITY_COLUMN = "Specific-Discharge-Capacity[mAh/g]"


@dataclass(frozen=True)
class PlotGroup:
    group: str
    label: str
    color: str


@dataclass(frozen=True)
class AggregatePoint:
    cycle: int
    mean: float
    sample_std: float | None
    contributors: int


REFERENCE = PlotGroup("CR2032 Referenz", "Ref - 1 h", "grey")
FAMILIES: dict[str, tuple[PlotGroup, ...]] = {
    "5 µm": (
        REFERENCE,
        PlotGroup("CR2032 5µm 1h", "Prä - 1 h", "#DC143C"),
        PlotGroup("CR2032 5µm 24h", "Prä - 24 h", "#4169E1"),
        PlotGroup("CR2032 5µm 48h", "Prä - 48 h", "#228B22"),
        PlotGroup("CR2032 5µm 168h", "Prä - 168 h", "#7A3DB8"),
    ),
    "10 µm": (
        REFERENCE,
        PlotGroup("CR2032 10µm 1h", "Prä - 1 h", "#DC143C"),
        PlotGroup("CR2032 10µm 24h", "Prä - 24 h", "#4169E1"),
        PlotGroup("CR2032 10µm 48h", "Prä - 48 h", "#228B22"),
        PlotGroup("CR2032 10µm 168h", "Prä - 168 h", "#7A3DB8"),
    ),
}

# (Gruppe, C-Rate) -> Zell-ID -> Protokollzyklus -> spez. Kapazitaet
SeriesIndex = dict[tuple[str, str], dict[str, dict[int, float]]]


def _number(value: str, column: str, row_number: int) -> float:
    try:
        number = float(value.strip().replace(",", "."))
    except ValueError as error:
        raise ValueError(
            f"Ungueltige Zahl in {column}, Zeile {row_number}: {value!r}"
        ) from error
    if not number > 0:
        raise ValueError(
            f"Nicht positiver Wert in {column}, Zeile {row_number}: {value!r}"
        )
    return number


def load_series(path: Path) -> SeriesIndex:
    """Lade nur gueltige C/2- und C/10-Kapazitaeten aus der Pipeline."""
    if not path.is_file():
        raise FileNotFoundError(f"Kapazitaetsdatei nicht gefunden: {path}")
    result: SeriesIndex = defaultdict(lambda: defaultdict(dict))
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {
            "Group", "Excel-Cell-ID", "Protocol-Cycle", "Cycle-Type",
            CAPACITY_COLUMN, "Capacity-Status",
        }
        missing = required.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "Kapazitaetsdatei: fehlende Spalten: " + ", ".join(sorted(missing))
            )
        for row_number, row in enumerate(reader, start=2):
            if row["Capacity-Status"].strip() != "valid":
                continue
            rate = row["Cycle-Type"].strip()
            if rate not in VALID_RATES:
                continue
            cycle_number = _number(row["Protocol-Cycle"], "Protocol-Cycle", row_number)
            if not cycle_number.is_integer():
                raise ValueError(
                    f"Nicht ganzzahliger Protokollzyklus in Zeile {row_number}"
                )
            capacity = _number(row[CAPACITY_COLUMN], CAPACITY_COLUMN, row_number)
            group = row["Group"].strip()
            cell = row["Excel-Cell-ID"].strip()
            cycle = int(cycle_number)
            series = result[(group, rate)][cell]
            if cycle in series:
                raise ValueError(
                    f"Doppelter Plotwert: {cell}, {rate}, Protokollzyklus {cycle}"
                )
            series[cycle] = capacity
    return {
        key: {cell: dict(sorted(values.items())) for cell, values in cells.items()}
        for key, cells in result.items()
    }


def aggregate_series(
    series: Mapping[str, Mapping[int, float]],
    *,
    normalize_soh: bool = False,
    common_range: bool = True,
    require_complete_cycles: bool = False,
) -> list[AggregatePoint]:
    """Berechne arithmetisches Mittel und Stichproben-Standardabweichung.

    ``common_range=True`` entspricht dem Notebook: Die Darstellung endet am
    letzten Zyklus der kuerzesten Zellreihe. Mit
    ``require_complete_cycles=True`` wird ein Zyklus nur dann aggregiert, wenn
    alle Zellen der Gruppe einen Wert beitragen. Fehlende Einzelzyklen bleiben
    damit echte Luecken und werden weder interpoliert noch mit wechselndem
    Stichprobenumfang gemittelt.
    """
    non_empty = {cell: dict(values) for cell, values in series.items() if values}
    if not non_empty:
        return []
    last_common_cycle = (
        min(max(values) for values in non_empty.values()) if common_range else None
    )
    by_cycle: dict[int, list[float]] = defaultdict(list)
    for values in non_empty.values():
        first_cycle = min(values)
        baseline = values[first_cycle]
        if baseline <= 0:
            continue
        for cycle, capacity in values.items():
            if last_common_cycle is not None and cycle > last_common_cycle:
                continue
            value = capacity / baseline * 100.0 if normalize_soh else capacity
            by_cycle[cycle].append(value)

    result: list[AggregatePoint] = []
    expected_contributors = len(non_empty)
    for cycle in sorted(by_cycle):
        values = by_cycle[cycle]
        if require_complete_cycles and len(values) != expected_contributors:
            continue
        result.append(AggregatePoint(
            cycle=cycle,
            mean=statistics.fmean(values),
            sample_std=statistics.stdev(values) if len(values) > 1 else None,
            contributors=len(values),
        ))
    return result


def _errorbar_points(
    points: Sequence[AggregatePoint],
    step: int,
    *,
    required_contributors: int,
) -> tuple[list[int], list[float], list[float]]:
    if step < 1:
        raise ValueError("Fehlerbalken-Schrittweite muss mindestens 1 sein")
    selected = [
        point for point in points[::step]
        if point.sample_std is not None
        and point.contributors == required_contributors
    ]
    return (
        [point.cycle for point in selected],
        [point.mean for point in selected],
        [point.sample_std or 0.0 for point in selected],
    )


def _configure_plotting(matplotlib: Any, plt: Any) -> Any:
    try:
        from matplotlib import font_manager

        font_manager.findfont("Times New Roman", fallback_to_default=False)
        font_family = "Times New Roman"
    except ValueError:
        font_family = "DejaVu Serif"
        LOGGER.warning("Times New Roman nicht gefunden; verwende %s", font_family)
    plt.rcParams.update({
        "font.family": font_family,
        "mathtext.fontset": "custom",
        "mathtext.rm": font_family,
        "mathtext.it": f"{font_family}:italic",
        "mathtext.bf": f"{font_family}:bold",
        "axes.formatter.use_locale": False,
    })
    plt.style.use("seaborn-v0_8-whitegrid")

    # Dezimalkomma ohne abhaengige Betriebssystem-Locale.
    from matplotlib.ticker import FuncFormatter

    matplotlib.rcParams["axes.formatter.use_locale"] = False
    return FuncFormatter(lambda value, _: f"{value:g}".replace(".", ","))


def _plot_one_metric(
    plt: Any,
    german_formatter: Any,
    data: SeriesIndex,
    family_name: str,
    groups: Sequence[PlotGroup],
    *,
    metric: str,
    output_path: Path,
    common_range: bool,
    original_limits: bool,
    show: bool,
) -> None:
    cm = 1 / 2.54
    fig, axis = plt.subplots(figsize=(16.5 * cm, 9 * cm), constrained_layout=False)
    if metric == "soh":
        axis.axhline(y=80, color="black", linestyle="--", linewidth=1.5, zorder=1)

    for group in groups:
        c2_series = data.get((group.group, "C/2"), {})
        c10_series = data.get((group.group, "C/10"), {})
        if not c2_series and not c10_series:
            LOGGER.warning("Keine Plotdaten fuer %s", group.group)
            continue
        z = len(set(c2_series) | set(c10_series))
        c2_all = aggregate_series(
            c2_series,
            normalize_soh=metric == "soh",
            common_range=common_range,
        )
        c10_all = aggregate_series(
            c10_series,
            normalize_soh=metric == "soh",
            common_range=common_range,
        )
        # Das Notebook waehlt zuerst jeden 15. C/2-Datenpunkt aus. Deshalb
        # muss dieses Raster auf der vollstaendigen Zyklusachse bleiben. Fuer
        # Mittelwerte und Balken werden danach bei ``common-range`` nur Zyklen
        # mit konstantem Stichprobenumfang z zugelassen.
        c2 = (
            [point for point in c2_all if point.contributors == len(c2_series)]
            if common_range else c2_all
        )
        c10 = (
            [point for point in c10_all if point.contributors == len(c10_series)]
            if common_range else c10_all
        )
        if c2:
            axis.scatter(
                [point.cycle for point in c2], [point.mean for point in c2],
                label=f"{group.label} (z = {z})", color=group.color,
                marker="o", s=10, zorder=3,
            )
            x, y, error = _errorbar_points(
                c2_all, 15, required_contributors=len(c2_series)
            )
            if x:
                axis.errorbar(
                    x, y, yerr=error, fmt="none", ecolor=group.color,
                    elinewidth=1, capsize=3, zorder=1,
                )
        if c10:
            axis.scatter(
                [point.cycle for point in c10], [point.mean for point in c10],
                color=group.color, marker="o", s=10, zorder=3,
            )
            x, y, error = _errorbar_points(
                c10_all, 1, required_contributors=len(c10_series)
            )
            if x:
                axis.errorbar(
                    x, y, yerr=error, fmt="none", ecolor=group.color,
                    elinewidth=1, capsize=3, zorder=1,
                )

    axis.set_xlabel(r"$\mathrm{Zyklenzahl\ } n\longrightarrow$", fontsize=14)
    if metric == "capacity":
        axis.set_ylabel(
            r"$\mathrm{spez.\ Kapazität\ } C/\mathrm{mAh\ g^{-1}}\ \longrightarrow$",
            fontsize=14,
        )
        axis.set_title(
            f"A) Gemittelte spez. Kapazität prälithiierter Knopfzellen "
            f"({family_name} Li-Folie)",
            fontsize=14, pad=14,
        )
        if original_limits:
            axis.set_ylim(100, 180)
    else:
        axis.set_ylabel(
            r"$\mathrm{State\ of\ Health\ } \mathrm{SOH}_\mathrm{c}/\mathrm{\%}"
            r"\ \longrightarrow$",
            fontsize=14,
        )
        axis.set_title(
            f"B) Gemittelter SOH prälithiierter Knopfzellen "
            f"({family_name} Li-Folie)",
            fontsize=14, pad=14,
        )
        if original_limits:
            axis.set_ylim(72, 102)

    if original_limits:
        axis.set_xlim(0, 470)
    else:
        axis.set_xlim(left=0)
    axis.xaxis.set_major_formatter(german_formatter)
    axis.yaxis.set_major_formatter(german_formatter)
    axis.grid(True, linewidth=0.5, alpha=1, zorder=0)
    axis.tick_params(axis="both", labelsize=12)
    axis.legend(
        loc="upper right", ncol=1, frameon=True, facecolor="white",
        edgecolor="black", fontsize=12, handlelength=1,
        columnspacing=0.8, handletextpad=0.2,
    )
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, format="svg", bbox_inches="tight")
    LOGGER.info("Plot gespeichert: %s", output_path)
    if show:
        plt.show()
    plt.close(fig)


def create_plots(
    data: SeriesIndex,
    output_dir: Path,
    *,
    common_range: bool = True,
    original_limits: bool = False,
    show: bool = False,
) -> list[Path]:
    try:
        import matplotlib
        if not show:
            matplotlib.use("Agg")
        from matplotlib import pyplot as plt
    except ImportError as error:
        raise RuntimeError(
            "matplotlib fehlt. Installation z. B. mit: python -m pip install matplotlib"
        ) from error
    german_formatter = _configure_plotting(matplotlib, plt)

    outputs: list[Path] = []
    for family_name, groups in FAMILIES.items():
        file_tag = family_name.replace(" ", "_")
        for metric, prefix in (("capacity", "Lebensdauer-spezifische_Kapazitaet"),
                               ("soh", "Lebensdauer-SOH")):
            destination = output_dir / f"{prefix}_{file_tag}.svg"
            _plot_one_metric(
                plt, german_formatter, data, family_name, groups,
                metric=metric, output_path=destination,
                common_range=common_range, original_limits=original_limits,
                show=show,
            )
            outputs.append(destination)
    return outputs


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path,
        default=root / "data" / "processed" / "capacity" / "capacity_by_cycle.tsv.gz",
        help="Gueltige Kapazitaetstabelle aus capacity.py",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=root / "data" / "output" / "plots",
    )
    parser.add_argument(
        "--aggregation", choices=("common-range", "available"), default="common-range",
        help=(
            "common-range endet wie das Notebook bei der kuerzesten Zellreihe; "
            "available nutzt spaetere Werte der verbleibenden Zellen"
        ),
    )
    parser.add_argument(
        "--original-limits", action="store_true",
        help="Alte Achsgrenzen 0..470, 100..180 bzw. 72..102 verwenden",
    )
    parser.add_argument("--show", action="store_true", help="Plots zusaetzlich anzeigen")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    if not args.verbose:
        logging.getLogger("matplotlib").setLevel(logging.WARNING)
    try:
        data = load_series(args.input)
        outputs = create_plots(
            data, args.output_dir,
            common_range=args.aggregation == "common-range",
            original_limits=args.original_limits,
            show=args.show,
        )
    except (OSError, ValueError, RuntimeError) as error:
        LOGGER.error("Plot-Erstellung abgebrochen: %s", error)
        return 1
    LOGGER.info("Fertig: %d Plots aus %s", len(outputs), args.input)
    return 0


if __name__ == "__main__":
    sys.exit(main())
