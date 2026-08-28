import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lua_ci_action import complexity as lua_complexity
from lua_ci_action import file_complexity as lua_file_complexity
from lua_ci_action.complexity import FunctionMetric


class LuaFileComplexityTest(unittest.TestCase):
    def test_totals_every_file_and_sorts_by_total_complexity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            empty_path = source / "empty.lua"
            low_a_path = source / "low-a.lua"
            low_b_path = source / "low-b.lua"
            high_path = source / "nested" / "high.lua"
            for path in (empty_path, low_a_path, low_b_path, high_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            functions = (
                self.function(high_path, "first", 7),
                self.function(low_b_path, "only", 4),
                self.function(low_a_path, "only", 4),
                self.function(high_path, "second", 3),
            )

            metrics = lua_file_complexity.file_complexities(source, functions)

            self.assertEqual(
                (
                    lua_file_complexity.FileComplexity(high_path.as_posix(), 2, 10),
                    lua_file_complexity.FileComplexity(low_a_path.as_posix(), 1, 4),
                    lua_file_complexity.FileComplexity(low_b_path.as_posix(), 1, 4),
                    lua_file_complexity.FileComplexity(empty_path.as_posix(), 0, 0),
                ),
                metrics,
            )

    def test_report_defines_and_displays_total_ccn(self) -> None:
        report = lua_file_complexity.render_report(
            (
                lua_file_complexity.FileComplexity("src/high.lua", 2, 10),
                lua_file_complexity.FileComplexity("src/empty.lua", 0, 0),
            )
        )

        self.assertIn("Total CCN is the sum of function cyclomatic complexity numbers", report)
        self.assertIn("| `src/high.lua` | 10 | 2 |", report)
        self.assertIn("| `src/empty.lua` | 0 | 0 |", report)

    def test_changes_use_merge_base_and_classify_renames_new_files_and_deletions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.git(repository, "init", "--initial-branch=base")
            self.git(repository, "config", "user.name", "Test")
            self.git(repository, "config", "user.email", "test@example.com")
            self.write(repository, "src/existing.lua", self.lua_function(0))
            self.write(repository, "src/renamed.lua", self.lua_function(1))
            self.write(repository, "src/deleted.lua", "Medusa.Deleted = { Alpha = 1, Beta = 2 }\n")
            self.git(repository, "add", ".")
            self.git(repository, "commit", "-m", "base")
            self.git(repository, "switch", "-c", "feature")
            self.write(repository, "src/existing.lua", self.lua_function(2))
            self.git(repository, "mv", "src/renamed.lua", "src/moved.lua")
            self.write(repository, "src/moved.lua", self.lua_function(0))
            self.write(repository, "src/new.lua", self.lua_function(3))
            self.write(repository, "src/new-empty.lua", "Medusa.New = {}\n")
            self.git(repository, "rm", "src/deleted.lua")
            self.git(repository, "add", ".")
            self.git(repository, "commit", "-m", "feature")
            self.git(repository, "switch", "base")
            self.write(repository, "src/base-only.lua", self.lua_function(6))
            self.git(repository, "add", ".")
            self.git(repository, "commit", "-m", "advance base")

            def repository_git(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repository), *args],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                ).stdout

            with (
                mock.patch.object(lua_complexity, "git", side_effect=repository_git),
                mock.patch.object(lua_file_complexity, "git", side_effect=repository_git),
            ):
                changes = lua_file_complexity.complexity_changes(Path("src"), "base", "feature")

            self.assertEqual(
                (
                    lua_file_complexity.FileComplexityChange(
                        "src/existing.lua", "src/existing.lua", 1, 3, 2
                    ),
                    lua_file_complexity.FileComplexityChange(
                        "src/moved.lua", "src/renamed.lua", 2, 1, -1
                    ),
                ),
                changes.existing_files,
            )
            self.assertEqual(
                (
                    lua_file_complexity.FileComplexity("src/new.lua", 1, 4),
                    lua_file_complexity.FileComplexity("src/new-empty.lua", 0, 0),
                ),
                changes.new_files,
            )

    def test_change_report_separates_existing_changes_and_all_new_files(self) -> None:
        report = lua_file_complexity.render_change_report(
            lua_file_complexity.ComplexityChanges(
                existing_files=(
                    lua_file_complexity.FileComplexityChange(
                        "src/current.lua", "src/old.lua", 8, 3, -5
                    ),
                ),
                new_files=(
                    lua_file_complexity.FileComplexity("src/new.lua", 2, 10),
                    lua_file_complexity.FileComplexity("src/empty.lua", 0, 0),
                ),
            )
        )

        self.assertIn("Existing files with CCN changes: **1**; total delta: **-5**", report)
        self.assertIn("Net-new files: **2**; total CCN: **10**", report)
        self.assertIn("### Largest changes in existing files", report)
        self.assertIn("| `src/current.lua` (from `src/old.lua`) | 8 | 3 | -5 |", report)
        self.assertIn("### Net-new files", report)
        self.assertIn("| `src/new.lua` | 10 | 2 |", report)
        self.assertIn("| `src/empty.lua` | 0 | 0 |", report)

    @staticmethod
    def function(path: Path, name: str, ccn: int) -> FunctionMetric:
        return FunctionMetric(
            path=path.as_posix(),
            name=name,
            start_line=1,
            end_line=1,
            nloc=1,
            ccn=ccn,
            parameters=0,
        )

    @staticmethod
    def git(repository: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def write(repository: Path, relative_path: str, content: str) -> None:
        path = repository / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def lua_function(branches: int) -> str:
        lines = ["local function example(value)"]
        lines.extend(
            f"    if value == {index} then return {index} end" for index in range(branches)
        )
        lines.extend(["    return value", "end", ""])
        return "\n".join(lines)


if __name__ == "__main__":
    unittest.main()
