import tempfile
import unittest
from pathlib import Path

from prepare_ocv_data import _is_candidate, downsample_file, duration_from_name, process_file


class PrepareOcvDataTests(unittest.TestCase):
    def test_duration_is_read_from_filename(self) -> None:
        self.assertEqual(duration_from_name("cell_48h_Formierung.txt"), 48)
        self.assertIsNone(duration_from_name("cell_PreLi1_Vorversuchsreihe.txt"))

    def test_candidate_selection_excludes_longterm_and_half_cells(self) -> None:
        self.assertTrue(_is_candidate("df_MH_MA_VZ_3,8_5µm_48h_1_Lebensdauer.txt"))
        self.assertTrue(_is_candidate("df_MH_MA_VZ_3,8_5µm_48h_1_Lebensdauer_Langzeit.txt"))
        self.assertFalse(_is_candidate("df_MH_MA_VZ_3,8_5µm_48h_1_Lebensdauer_Lebensdauer.txt"))
        self.assertFalse(_is_candidate("df_MH_MA_VV_HZ_Prä_48h_1_Ladetest.txt"))

    def test_file_is_truncated_without_state_filter(self) -> None:
        header = "#Time[h] DataSet DateTime Command U[V] I[A] State\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "cell_48h_Formierung.txt"
            destination = root / "out.txt"
            source.write_text(
                header
                + "47,9 1 01.01.2026 Pause 3,1 0 0\n"
                + "48 2 01.01.2026 Pause 3,2 0 1\n"
                + "48,1 3 01.01.2026 Charge 3,3 1 2\n",
                encoding="utf-8",
            )

            result = process_file(source, destination)

            self.assertEqual(result.rows_written, 2)
            self.assertEqual(result.rows_skipped_after_limit, 1)
            self.assertIn(" 1\n", destination.read_text(encoding="utf-8"))

    def test_downsampling_uses_finer_initial_density(self) -> None:
        header = "#Time[h] DataSet DateTime Command U[V] I[A] State\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "ocv.txt"
            destination = root / "plot.txt"
            rows = [
                "0 1 01.01.2026 Pause 3,1 0 0\n",
                "0.0027777778 2 01.01.2026 Pause 3,1 0 0\n",
                "0.0055555556 3 01.01.2026 Pause 3,1 0 0\n",
                "0.1666666667 4 01.01.2026 Pause 3,1 0 0\n",
                "0.175 5 01.01.2026 Pause 3,1 0 0\n",
                "0.1833333334 6 01.01.2026 Pause 3,1 0 0\n",
                "1.2 7 01.01.2026 Pause 3,1 0 0\n",
            ]
            source.write_text(header + "".join(rows), encoding="utf-8")

            result = downsample_file(source, destination)

            self.assertEqual(result.rows_written, 6)
            self.assertEqual(len(destination.read_text(encoding="utf-8").splitlines()), 7)


if __name__ == "__main__":
    unittest.main()