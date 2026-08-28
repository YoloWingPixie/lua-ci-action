import unittest
from unittest import mock

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

    def test_changed_lua_paths_include_current_rename_and_deleted_paths(self) -> None:
        def repository_git(*args: str) -> str:
            if args[0] == "merge-base":
                return "merge\n"
            if args[0] == "diff":
                return "\n".join(
                    (
                        "M\tsrc/changed.lua",
                        "A\tsrc/new.lua",
                        "D\tsrc/deleted.lua",
                        "R100\tsrc/old.lua\tsrc/moved.lua",
                        "M\tREADME.md",
                    )
                )
            self.fail(f"unexpected git call: {args}")

        with (
            mock.patch.object(complexity, "resolve_ref", side_effect=("base", "head")),
            mock.patch.object(complexity, "git", side_effect=repository_git),
        ):
            paths = complexity.changed_lua_paths("base-ref", "head-ref")

        self.assertEqual(
            (
                "src/changed.lua",
                "src/deleted.lua",
                "src/moved.lua",
                "src/new.lua",
            ),
            paths,
        )


if __name__ == "__main__":
    unittest.main()
