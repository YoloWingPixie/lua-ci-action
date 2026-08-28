import unittest

from lua_ci_action import complexity


class ComplexityTest(unittest.TestCase):
    limits = complexity.Limits(17, 8, 10, 5)

    def test_violation_thresholds_are_exclusive(self) -> None:
        at_limit = complexity.FunctionMetric("src/a.lua", "run", 1, 2, 2, 17, 8)
        over_limit = complexity.FunctionMetric("src/a.lua", "run", 1, 2, 2, 18, 9)

        self.assertEqual((), complexity.violations(at_limit, self.limits))
        self.assertEqual(
            ("CCN 18 > 17", "parameters 9 > 8"),
            complexity.violations(over_limit, self.limits),
        )

    def test_changed_lines_overlap_function_body(self) -> None:
        function = complexity.FunctionMetric("src/a.lua", "run", 10, 20, 8, 2, 1)

        self.assertTrue(complexity.overlaps_changed_lines(function, {"src/a.lua": ((20, 20),)}))
        self.assertFalse(complexity.overlaps_changed_lines(function, {"src/a.lua": ((21, 21),)}))

    def test_report_states_that_findings_are_advisory(self) -> None:
        function = complexity.FunctionMetric("src/a.lua", "run", 1, 5, 5, 18, 1)
        result = complexity.AnalysisResult(1, 1, 5, 0.0, (function,))

        report = complexity.render_report(result, {}, self.limits)

        self.assertIn("Advisory only", report)
        self.assertIn("Functions above a warning ceiling: **1**", report)


if __name__ == "__main__":
    unittest.main()
