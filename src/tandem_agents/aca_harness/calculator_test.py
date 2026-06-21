"""Focused unittest coverage for the deterministic calculator harness."""

import unittest

from . import (
    add as exported_add,
    describe_operation as exported_describe_operation,
    subtract as exported_subtract,
)
from .calculator import add, describe_operation, multiply, subtract


class CalculatorTest(unittest.TestCase):
    def test_package_exports_calculator_helpers(self):
        self.assertIs(exported_add, add)
        self.assertIs(exported_subtract, subtract)
        self.assertIs(exported_describe_operation, describe_operation)

    def test_add_returns_sum(self):
        self.assertEqual(add(2, 3), 5)

    def test_multiply_returns_product(self):
        self.assertEqual(multiply(4, 5), 20)
        self.assertEqual(multiply(-2, 6), -12)

    def test_subtract_returns_difference(self):
        self.assertEqual(subtract(7, 4), 3)

    def test_describe_operation_supports_add(self):
        self.assertEqual(describe_operation("add", 2, 3), "2 + 3 = 5")

    def test_describe_operation_supports_subtract(self):
        self.assertEqual(describe_operation("subtract", 7, 4), "7 - 4 = 3")

    def test_describe_operation_supports_multiply(self):
        self.assertEqual(describe_operation("multiply", 4, 5), "4 * 5 = 20")

    def test_describe_operation_rejects_unknown_operation(self):
        with self.assertRaises(ValueError) as error:
            describe_operation("divide", 2, 3)

        self.assertEqual(str(error.exception), "unknown operation: divide")


if __name__ == "__main__":
    unittest.main()
