from __future__ import annotations

import unittest

from plot_lifetime_data import AggregatePoint, _errorbar_points, aggregate_series


class LifetimePlotTests(unittest.TestCase):
    def test_capacity_mean_and_sample_standard_deviation(self) -> None:
        series = {
            "a": {6: 100.0, 7: 90.0},
            "b": {6: 200.0, 7: 180.0},
        }
        result = aggregate_series(series)

        self.assertEqual([point.cycle for point in result], [6, 7])
        self.assertEqual(result[0].mean, 150.0)
        self.assertAlmostEqual(result[0].sample_std or 0, 70.71067811865476)
        self.assertEqual(result[0].contributors, 2)

    def test_soh_uses_first_value_of_each_cell(self) -> None:
        series = {
            "a": {6: 200.0, 7: 180.0},
            "b": {6: 100.0, 7: 80.0},
        }
        result = aggregate_series(series, normalize_soh=True)

        self.assertEqual(result[0].mean, 100.0)
        self.assertEqual(result[1].mean, 85.0)
        self.assertAlmostEqual(result[1].sample_std or 0, 7.0710678118654755)

    def test_common_range_matches_notebook_shortest_series_rule(self) -> None:
        series = {
            "short": {6: 100.0, 7: 90.0},
            "long": {6: 110.0, 7: 100.0, 8: 95.0},
        }

        common = aggregate_series(series, common_range=True)
        available = aggregate_series(series, common_range=False)

        self.assertEqual([point.cycle for point in common], [6, 7])
        self.assertEqual([point.cycle for point in available], [6, 7, 8])
        self.assertEqual(available[-1].contributors, 1)

    def test_complete_cycles_do_not_mix_changing_sample_sizes(self) -> None:
        series = {
            "a": {6: 100.0, 7: 90.0},
            "b": {6: 110.0},
        }

        result = aggregate_series(
            series, common_range=True, require_complete_cycles=True
        )

        self.assertEqual([point.cycle for point in result], [6])
        self.assertEqual(result[0].contributors, 2)

    def test_errorbars_use_notebook_stride_and_full_group_only(self) -> None:
        points = [
            AggregatePoint(cycle=cycle, mean=100.0, sample_std=2.0, contributors=4)
            for cycle in range(6, 51)
        ]
        points[15] = AggregatePoint(
            cycle=21, mean=100.0, sample_std=20.0, contributors=3
        )

        x, y, error = _errorbar_points(
            points, 15, required_contributors=4
        )

        self.assertEqual(x, [6, 36])
        self.assertEqual(y, [100.0, 100.0])
        self.assertEqual(error, [2.0, 2.0])


if __name__ == "__main__":
    unittest.main()
