''' testet auf: Korrektheit der neuen Zählweise (mit drei Diagnosecycles pro Block),
    Gültigkeit der Kombi aus Line/Command/Strom, Simulation von widersprüchlichen Daten, ...
'''
import unittest

from src.cycle_reconstruction import (
    classify_counter,
    infer_cycle_type,
    reconstruct_rows,
    validate_cycle_rows,
)


def row(cycle, line, command, current):
    return {
        "Cyc-Count": str(cycle),
        "Line": str(line),
        "Command": command,
        "I[A]": str(current),
    }


class CycleReconstructionTests(unittest.TestCase):
    def test_all_protocol_boundaries(self):
        expected = {
            1: ("C/10", None, 0),
            2: ("C/10", None, 0),
            3: ("Diagnose", None, 0),
            5: ("Diagnose", None, 0),
            6: ("C/2", 1, 0),
            50: ("C/2", 45, 0),
            51: ("C/10", None, 45),
            52: ("C/10", None, 45),
            53: ("Diagnose", None, 45),
            55: ("Diagnose", None, 45),
            56: ("C/2", 46, 45),
            100: ("C/2", 90, 45),
            101: ("C/10", None, 90),
            105: ("Diagnose", None, 90),
            106: ("C/2", 91, 90),
            150: ("C/2", 135, 90),
        }
        for counter, values in expected.items():
            with self.subTest(counter=counter):
                position = classify_counter(counter)
                self.assertEqual(
                    (
                        position.cycle_type,
                        position.c2_cycle_in_segment,
                        position.checkpoint_in_segment,
                    ),
                    values,
                )

    def test_line_command_and_current_confirm_classification(self):
        c10 = validate_cycle_rows(
            "C/10", [row(1, 17, "Charge", "0.0005"), row(1, 19, "Discharge", "-0.0005")]
        )
        diagnosis = validate_cycle_rows(
            "Diagnose", [row(3, 21, "Pause", "0"), row(3, 22, "Charge", "0.01")]
        )
        c2 = validate_cycle_rows(
            "C/2", [row(6, 32, "Charge", "0.0025"), row(6, 34, "Discharge", "-0.0025")]
        )
        self.assertEqual(c10.status, "valid")
        self.assertEqual(diagnosis.status, "valid")
        self.assertEqual(c2.status, "valid")

    def test_wrong_command_is_reported_and_wrong_current_direction_is_invalid(self):
        result = validate_cycle_rows(
            "C/2",
            [row(6, 32, "Discharge", "0.0025"), row(6, 34, "Discharge", "0.0025")],
        )
        self.assertEqual(result.status, "invalid")
        self.assertIn("statt charge", result.evidence)
        self.assertIn("ohne negativen Strom", result.evidence)

    def test_missing_type_signature_causes_warning(self):
        result = validate_cycle_rows("C/10", [row(1, 32, "Charge", "0.0025")])
        self.assertEqual(result.status, "warning")
        self.assertIn("keine typische C/10-Linie", result.evidence)

    def test_part_program_resets_create_real_protocol_cycles(self):
        input_rows = [
            row(1, 17, "Charge", "0.0005"),
            row(2, 19, "Discharge", "-0.0005"),
            row(1, 21, "Pause", "0"),
            row(2, 27, "Charge", "0.0005"),
            row(1, 32, "Charge", "0.0025"),
            row(2, 34, "Discharge", "-0.0025"),
            row(1, 17, "Charge", "0.0005"),
            row(2, 19, "Discharge", "-0.0005"),
            row(1, 21, "Pause", "0"),
            row(2, 27, "Charge", "0.0005"),
            row(1, 32, "Charge", "0.0025"),
        ]
        output = list(reconstruct_rows(input_rows))
        self.assertEqual(
            [item["Protocol-Cycle"] for item in output],
            ["1", "2", "3", "4", "6", "7", "51", "52", "53", "54", "56"],
        )
        self.assertEqual(
            [item["Lifetime-Cycle"] for item in output],
            ["", "", "", "", "1", "2", "", "", "", "", "46"],
        )
        self.assertEqual(output[-1]["Checkpoint-Cycle"], "45")

    def test_line_signature_determines_type_instead_of_counter_value(self):
        self.assertEqual(infer_cycle_type([row(1, 32, "Charge", "0.0025")]).status, "C/2")
        self.assertEqual(infer_cycle_type([row(45, 17, "Charge", "0.0005")]).status, "C/10")

    def test_non_positive_counter_is_rejected(self):
        with self.assertRaises(ValueError):
            classify_counter(0)


if __name__ == "__main__":
    unittest.main()
