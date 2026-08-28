import unittest

from lua_ci_action import complexity, file_complexity, reporting


class ReportingTest(unittest.TestCase):
    def test_reports_are_compact_and_pull_request_findings_use_changed_files(self) -> None:
        limits = complexity.Limits(17, 8, 10, 5)
        changed_function = complexity.FunctionMetric("src/changed.lua", "changed", 3, 20, 15, 18, 2)
        other_function = complexity.FunctionMetric("src/other.lua", "other", 4, 12, 8, 3, 1)
        complexity_result = reporting.ComplexityResult(
            analysis=complexity.AnalysisResult(
                file_count=2,
                function_count=2,
                source_nloc=23,
                duplicate_rate_percent=1.25,
                functions=(changed_function, other_function),
            ),
            limits=limits,
            files=(
                file_complexity.FileComplexity("src/changed.lua", 1, 18),
                file_complexity.FileComplexity("src/other.lua", 1, 3),
            ),
            changes=file_complexity.ComplexityChanges(
                existing_files=(
                    file_complexity.FileComplexityChange(
                        "src/changed.lua", "src/changed.lua", 16, 18, 2
                    ),
                ),
                new_files=(),
            ),
        )
        data = reporting.ReportData(
            format=reporting.CheckResult(
                "Format",
                False,
                ("src/changed.lua", "src/other.lua"),
                (reporting.Finding("src/other.lua", None, "Format", "StyLua"),),
            ),
            syntax=reporting.CheckResult(
                "Syntax",
                False,
                ("src/changed.lua", "src/other.lua"),
                (reporting.Finding("src/changed.lua", 7, "Syntax", "unexpected symbol"),),
            ),
            source_policy=reporting.CheckResult(
                "Source policy",
                False,
                ("src/changed.lua", "src/other.lua"),
                (reporting.Finding("src/other.lua", 2, "B01", "global write"),),
            ),
            complexity=complexity_result,
            changed_paths=("src/changed.lua",),
        )

        project = reporting.render_project(data)
        pull_request = reporting.render_pull_request(data)

        self.assertIn(reporting.PROJECT_MARKER, project)
        self.assertIn("| Format | Fail | 2 | 1 |", project)
        self.assertIn("| Files | Functions | NLOC | Duplication |", project)
        self.assertIn(reporting.PULL_REQUEST_MARKER, pull_request)
        self.assertIn("| Format | Pass | 1 | 0 |", pull_request)
        self.assertIn("| Syntax | Fail | 1 | 1 |", pull_request)
        self.assertIn("`src/changed.lua`", pull_request)
        self.assertNotIn("src/other.lua", pull_request)
        self.assertNotIn("Advisory", project + pull_request)
        self.assertNotIn("(", project + pull_request)
        self.assertNotIn(")", project + pull_request)


if __name__ == "__main__":
    unittest.main()
