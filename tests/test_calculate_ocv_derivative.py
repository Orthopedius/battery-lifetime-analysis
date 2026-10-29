import unittest

from calculate_ocv_derivative import derivative


class OcvDerivativeTests(unittest.TestCase):
    def test_central_difference_and_one_sided_endpoints(self) -> None:
        values = [(0.0, 0.0), (1.0, 2.0), (3.0, 6.0)]
        result = derivative(values)

        self.assertEqual(result[0], (0.0, 0.0, 2.0, "forward"))
        self.assertEqual(result[1], (1.0, 2.0, 2.0, "central"))
        self.assertEqual(result[2], (3.0, 6.0, 2.0, "backward"))

    def test_single_value_has_no_derivative(self) -> None:
        self.assertEqual(derivative([(2.0, 4.0)]), [(2.0, 4.0, None, "undefined")])


if __name__ == "__main__":
    unittest.main()