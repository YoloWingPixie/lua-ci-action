import json
import tempfile
import unittest
from pathlib import Path

from lua_ci_action.config import load_config


class ConfigTest(unittest.TestCase):
    def load(self, payload: object):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            path = workspace / ".lua-ci-actionrc"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_config(path, workspace)

    def test_loads_defaults(self) -> None:
        config = self.load({"schema_version": 1})

        self.assertEqual((Path("src"), Path("tests")), config.format.paths)
        self.assertEqual(17, config.complexity.ccn_warning)
        self.assertEqual(frozenset(), config.source_policy.allowed_global_writes)

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown field"):
            self.load({"schema_version": 1, "format": {"command": "anything"}})

    def test_rejects_paths_outside_workspace(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes the workspace"):
            self.load({"schema_version": 1, "syntax": {"paths": ["../source"]}})

    def test_rejects_non_positive_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self.load({"schema_version": 1, "complexity": {"ccn_warning": 0}})

    def test_rejects_healthy_targets_above_warning_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "healthy_ccn"):
            self.load(
                {
                    "schema_version": 1,
                    "complexity": {"healthy_ccn": 18, "ccn_warning": 17},
                }
            )

    def test_rejects_duplicate_global_names(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicates"):
            self.load(
                {
                    "schema_version": 1,
                    "source_policy": {"allowed_global_writes": ["Project", "Project"]},
                }
            )


if __name__ == "__main__":
    unittest.main()
