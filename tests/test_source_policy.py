import json
import tempfile
import unittest
from pathlib import Path

from lua_ci_action import source_policy as policy


class LuaSourcePolicyTest(unittest.TestCase):
    policy = policy.Policy(
        allowed_global_writes=frozenset({"Medusa"}),
        max_control_nesting=5,
        max_parameters=9,
        max_function_nloc=100,
    )

    def audit(self, source_text: str) -> set[str]:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            source.mkdir()
            (source / "sample.lua").write_text(source_text, encoding="utf-8")
            return {violation.rule for violation in policy.audit_source(source, self.policy)}

    def assert_rejects(self, rule: str, source_text: str) -> None:
        self.assertIn(rule, self.audit(source_text))

    def assert_allows(self, rule: str, source_text: str) -> None:
        self.assertNotIn(rule, self.audit(source_text))

    def test_global_policy_allows_medusa_and_rejects_other_implicit_writes(self) -> None:
        self.assert_allows("B01", "Medusa = Medusa or {}\nMedusa.Value = 1\n")
        self.assert_rejects("B01", "function run()\n    accidental = 1\nend\n")
        self.assert_rejects("B01", "Accidental.value = 1\n")
        self.assert_rejects("B01", "function Accidental.run()\nend\n")
        self.assert_rejects("B01", "function Accidental:run()\nend\n")
        self.assert_allows("B01", "local Value = {}\nValue.item = 1\nfunction Value.run()\nend\n")

    def test_global_allowlist_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "src"
            source.mkdir()
            (source / "sample.lua").write_text("Project = Project or {}\n", encoding="utf-8")
            configured = policy.Policy(frozenset({"Project"}), 5, 9, 100)

            rules = {violation.rule for violation in policy.audit_source(source, configured)}

            self.assertNotIn("B01", rules)

    def test_banned_dynamic_code(self) -> None:
        for name in policy.DYNAMIC_CODE_NAMES:
            with self.subTest(name=name):
                self.assert_rejects("B02", f"local value = {name}('x')\n")

    def test_statement_layout(self) -> None:
        self.assert_rejects("B04", "local a = 1; local b = 2\n")
        self.assert_rejects("B04", "local a = 1 local b = 2\n")
        self.assert_rejects("B05", "if ready then return end\n")
        self.assert_allows("B05", "if ready then\n    return\nend\n")

    def test_calls_require_parentheses(self) -> None:
        self.assert_rejects("B06", "run 'value'\n")
        self.assert_allows("B06", "run('value')\n")

    def test_empty_control_bodies(self) -> None:
        self.assert_rejects("B07", "do\nend\n")
        self.assert_rejects("B07", "if ready then\nend\n")
        self.assert_rejects("B07", "if ready then\n    run()\nelseif waiting then\nend\n")
        self.assert_rejects("B07", "while ready do\nend\n")

    def test_table_constructor_rules(self) -> None:
        self.assert_rejects("B08", "local value = { key = 1, ['key'] = 2 }\n")
        self.assert_rejects("B09", "local value = { 1, key = 2 }\n")
        self.assert_rejects("B09", "local value = { [1] = 1, key = 2 }\n")
        self.assert_allows("B09", "local list = { 1, 2 }\nlocal record = { key = 2 }\n")

    def test_shadowing(self) -> None:
        self.assert_rejects("B10", "local value = 1\nif ready then\n    local value = 2\nend\n")
        self.assert_allows("B10", "for _, value in pairs(values) do\n    consume(value)\nend\n")

    def test_require_is_literal_and_top_level(self) -> None:
        self.assert_rejects("B11", "require(module_name)\n")
        self.assert_rejects("B20", "local function load_module()\n    require('module')\nend\n")
        self.assert_allows("B20", "require('module')\n")

    def test_debug_and_raw_table_escape_hatches(self) -> None:
        self.assert_rejects("B17", "local value = rawget(record, 'key')\n")
        self.assert_rejects("B17", "local value = debug.traceback()\n")
        self.assert_allows("B17", "logger:debug('message')\n")
        self.assert_allows("B17", "local function debug(message)\n    logger:debug(message)\nend\n")

    def test_magical_metatables(self) -> None:
        self.assert_rejects("B18", "return setmetatable({}, { __call = run })\n")
        self.assert_rejects(
            "B18", "return setmetatable({}, { __index = function() return nil end })\n"
        )
        self.assert_rejects("B18", "local meta = {}\nmeta.__call = run\n")
        self.assert_rejects(
            "B18", "local meta = {}\nmeta.__index = function()\n    return nil\nend\n"
        )
        self.assert_allows("B18", "return setmetatable({}, { __index = Methods })\n")
        self.assert_allows("B18", "local Class = {}\nClass.__index = Class\n")

    def test_imported_modules_are_not_monkey_patched(self) -> None:
        self.assert_rejects("B19", "local module = require('module')\nmodule.value = 1\n")
        self.assert_rejects("B19", "local module = require('module')\nfunction module.run()\nend\n")

    def test_implicit_literal_coercion(self) -> None:
        self.assert_rejects("B21", "local value = '2' + 1\n")
        self.assert_rejects("B21", "local value = 'count: ' .. 2\n")

    def test_iteration_does_not_mutate_the_traversed_table(self) -> None:
        self.assert_rejects("B25", "for key in pairs(values) do\n    values[key] = 1\nend\n")
        self.assert_rejects("B25", "for key in next, values do\n    values[key] = 1\nend\n")
        self.assert_rejects(
            "B25", "for key in pairs(self.values) do\n    self.values[key] = 1\nend\n"
        )
        self.assert_rejects(
            "B25", "for key in pairs(values) do\n    table.remove(values, key)\nend\n"
        )
        self.assert_allows("B25", "for key in pairs(values) do\n    values[key] = nil\nend\n")
        self.assert_allows(
            "B25", "for key in pairs(values) do\n    output[key] = values[key]\nend\n"
        )

    def test_explicit_return_arity_is_consistent(self) -> None:
        self.assert_rejects(
            "B27",
            "local function run(value)\n    if value then\n        return nil\n    end\n    return nil, 'reason'\nend\n",
        )
        self.assert_allows(
            "B27",
            "local function run(value)\n    if value then\n        return nil, nil\n    end\n    return nil, 'reason'\nend\n",
        )

    def test_control_nesting_rejects_depth_six(self) -> None:
        self.assert_allows("B37", self.nested_conditions(5))
        self.assert_rejects("B37", self.nested_conditions(6))

    def test_function_limits(self) -> None:
        parameters = ", ".join(f"p{index}" for index in range(9))
        self.assert_allows("B40", f"local function run({parameters})\n    return nil\nend\n")
        parameters = ", ".join(f"p{index}" for index in range(10))
        self.assert_rejects("B40", f"local function run({parameters})\n    return nil\nend\n")
        body = "\n".join(f"    local value_{index} = {index}" for index in range(98))
        self.assert_allows("B41", f"local function run()\n{body}\nend\n")
        body = "\n".join(f"    local value_{index} = {index}" for index in range(101))
        self.assert_rejects("B41", f"local function run()\n{body}\nend\n")

    def nested_conditions(self, count: int) -> str:
        source = ""
        for depth in range(count):
            source += "    " * depth + "if ready then\n"
        source += "    " * count + "run()\n"
        for depth in reversed(range(count)):
            source += "    " * depth + "end\n"
        return source

    def test_baseline_tracks_exact_occurrence_counts_and_rejects_stale_entries(self) -> None:
        violation = policy.Violation("src/a.lua", 1, "B04", "message", "local x = 1; local y = 2")
        baseline = policy.baseline_counts([violation])

        new, stale = policy.compare_baseline([violation, violation], baseline)

        self.assertEqual([violation], new)
        self.assertFalse(stale)
        new, stale = policy.compare_baseline([], baseline)
        self.assertFalse(new)
        self.assertEqual(1, sum(stale.values()))

    def test_baseline_round_trip(self) -> None:
        violation = policy.Violation("src/a.lua", 1, "B04", "message", "local x = 1; local y = 2")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"

            policy.write_baseline(path, [violation])

            self.assertEqual(policy.baseline_counts([violation]), policy.load_baseline(path))
            self.assertEqual(1, json.loads(path.read_text(encoding="utf-8"))["schema_version"])

    def test_baseline_rejects_invalid_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "baseline.json"
            path.write_text(
                json.dumps({"schema_version": 1, "violations": [["src/a.lua", "B01"]]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid baseline violation entry"):
                policy.load_baseline(path)


if __name__ == "__main__":
    unittest.main()
