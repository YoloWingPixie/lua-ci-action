#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import lizard
from luaparser import ast, astnodes

from .lua_tokens import tokenize


DYNAMIC_CODE_NAMES = frozenset(
    {"dofile", "getfenv", "load", "loadfile", "loadstring", "module", "setfenv"}
)
ESCAPE_FUNCTION_NAMES = frozenset({"rawget", "rawset"})
FUNCTION_NODES = (
    astnodes.AnonymousFunction,
    astnodes.Function,
    astnodes.LocalFunction,
    astnodes.Method,
)
CONTROL_NODES = (astnodes.Fornum, astnodes.Forin, astnodes.If, astnodes.Repeat, astnodes.While)
CONTROL_BODY_NODES = CONTROL_NODES + (astnodes.Do, astnodes.ElseIf)
MULTI_RESULT_NODES = (astnodes.Call, astnodes.Invoke, astnodes.Varargs)
MAGICAL_METAMETHODS = frozenset(
    {
        "__add",
        "__call",
        "__concat",
        "__div",
        "__eq",
        "__gc",
        "__le",
        "__len",
        "__lt",
        "__metatable",
        "__mod",
        "__mode",
        "__mul",
        "__newindex",
        "__pow",
        "__sub",
        "__tostring",
        "__unm",
    }
)


@dataclass(frozen=True)
class Policy:
    allowed_global_writes: frozenset[str]
    max_control_nesting: int
    max_parameters: int
    max_function_nloc: int


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str
    message: str
    evidence: str

    @property
    def fingerprint(self) -> str:
        source = "\0".join((self.rule, self.message, self.evidence.strip()))
        return hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]


@dataclass
class Scope:
    parent: Scope | None
    names: set[str]

    def contains(self, name: str) -> bool:
        scope: Scope | None = self
        while scope is not None:
            if name in scope.names:
                return True
            scope = scope.parent
        return False


class FileAudit:
    def __init__(self, path: Path, text: str, tree: astnodes.Chunk, policy: Policy):
        self.path = path.as_posix()
        self.text = text
        self.lines = text.splitlines()
        self.tree = tree
        self.policy = policy
        self.violations: list[Violation] = []
        self.parents: dict[int, astnodes.Node] = {}
        self.imported_names: set[str] = set()
        self._index_tree(tree)

    def line_of(self, node: astnodes.Node) -> int:
        offset = getattr(node, "start_char", None)
        if offset is None:
            return 1
        return self.text.count("\n", 0, offset) + 1

    def end_line_of(self, node: astnodes.Node) -> int:
        offset = getattr(node, "stop_char", None)
        if offset is None:
            return self.line_of(node)
        return self.text.count("\n", 0, offset + 1) + 1

    def add(
        self, rule: str, node: astnodes.Node | None, message: str, line: int | None = None
    ) -> None:
        resolved_line = line or (self.line_of(node) if node is not None else 1)
        evidence = (
            self.lines[resolved_line - 1].strip() if 0 < resolved_line <= len(self.lines) else ""
        )
        self.violations.append(Violation(self.path, resolved_line, rule, message, evidence))

    def audit(self) -> list[Violation]:
        self._audit_tokens()
        self._audit_nodes()
        self._audit_scopes()
        self._audit_return_arities()
        self._audit_nesting()
        return sorted(set(self.violations))

    def _index_tree(self, node: astnodes.Node) -> None:
        for child in child_nodes(node):
            self.parents[id(child)] = node
            self._index_tree(child)

    def _audit_tokens(self) -> None:
        for token in tokenize(self.text):
            if token.value == ";":
                self.add("B04", None, "semicolons are not permitted", token.line)
        for block in (node for node in walk(self.tree) if isinstance(node, astnodes.Block)):
            statements: defaultdict[int, list[astnodes.Node]] = defaultdict(list)
            for statement in block.body:
                statements[self.line_of(statement)].append(statement)
            for line, same_line in statements.items():
                if len(same_line) > 1:
                    self.add("B04", same_line[1], "write one statement per line", line)

    def _audit_nodes(self) -> None:
        for node in walk(self.tree):
            self._audit_node(node)

    def _audit_node(self, node: astnodes.Node) -> None:
        if isinstance(node, FUNCTION_NODES + CONTROL_BODY_NODES) and self.line_of(
            node
        ) == self.end_line_of(node):
            self.add("B05", node, "control and function bodies must span multiple lines")
        if isinstance(node, CONTROL_BODY_NODES) and not node.body.body:
            self.add("B07", node, "empty control-flow body")
        if (
            isinstance(node, (astnodes.If, astnodes.ElseIf))
            and isinstance(node.orelse, astnodes.Block)
            and not node.orelse.body
        ):
            self.add("B07", node.orelse, "empty else body")
        if isinstance(node, (astnodes.Call, astnodes.Invoke)):
            self._audit_call(node)
        if isinstance(node, astnodes.Name) and node.id == "debug":
            parent = self.parents.get(id(node))
            if isinstance(parent, astnodes.Index) and parent.value is node:
                self.add("B17", node, "the debug library is not permitted in production source")
        if isinstance(node, astnodes.Table):
            self._audit_table(node)
        if isinstance(node, astnodes.Assign):
            self._audit_metamethod_assignments(node)
        if isinstance(node, astnodes.AriOp) and (
            isinstance(node.left, astnodes.String) or isinstance(node.right, astnodes.String)
        ):
            self.add("B21", node, "do not rely on implicit string-to-number coercion")
        if isinstance(node, astnodes.UMinusOp) and isinstance(node.operand, astnodes.String):
            self.add("B21", node, "do not rely on implicit string-to-number coercion")
        if isinstance(node, astnodes.Concat) and (
            isinstance(node.left, astnodes.Number) or isinstance(node.right, astnodes.Number)
        ):
            self.add("B21", node, "convert numbers explicitly before concatenation")
        if isinstance(node, astnodes.Forin):
            self._audit_iteration_mutation(node)

    def _audit_call(self, node: astnodes.Call | astnodes.Invoke) -> None:
        if getattr(node.style, "name", "DEFAULT") != "DEFAULT":
            self.add("B06", node, "function calls require parentheses")
        name = direct_call_name(node)
        if name in DYNAMIC_CODE_NAMES:
            self.add("B02", node, f"{name} is not permitted in production source")
        if name in ESCAPE_FUNCTION_NAMES:
            self.add("B17", node, f"{name} bypasses ordinary table behavior")
        if name == "require":
            if len(node.args) != 1 or not isinstance(node.args[0], astnodes.String):
                self.add("B11", node, "require needs one literal module path")
            if not self._is_top_level(node):
                self.add("B20", node, "require belongs at module top level")

    def _audit_table(self, node: astnodes.Table) -> None:
        implicit_count = 0
        list_like = False
        record_like = False
        keys: dict[tuple[str, object], astnodes.Field] = {}
        for field in node.fields:
            if field.key is None:
                implicit_count += 1
                list_like = True
                key = ("number", implicit_count)
            else:
                key = static_table_key(field)
                if key and key[0] == "number":
                    list_like = True
                else:
                    record_like = True
                metamethod = string_table_key(field)
                if metamethod in MAGICAL_METAMETHODS or (
                    metamethod and metamethod.startswith("__") and metamethod != "__index"
                ):
                    self.add("B18", field, f"metamethod {metamethod} hides ordinary table behavior")
                if metamethod == "__index" and isinstance(field.value, astnodes.AnonymousFunction):
                    self.add("B18", field, "function-valued __index hides ordinary table reads")
            if key is not None:
                if key in keys:
                    self.add("B08", field, f"duplicate table key {key[1]!r}")
                keys[key] = field
        if list_like and record_like:
            self.add("B09", node, "table constructors cannot mix list and dictionary fields")

    def _audit_metamethod_assignments(self, node: astnodes.Assign) -> None:
        for index, target in enumerate(node.targets):
            metamethod = string_index_key(target)
            if metamethod in MAGICAL_METAMETHODS or (
                metamethod and metamethod.startswith("__") and metamethod != "__index"
            ):
                self.add("B18", target, f"metamethod {metamethod} hides ordinary table behavior")
            assigned = node.values[index] if index < len(node.values) else None
            if metamethod == "__index" and isinstance(assigned, astnodes.AnonymousFunction):
                self.add("B18", target, "function-valued __index hides ordinary table reads")

    def _audit_iteration_mutation(self, node: astnodes.Forin) -> None:
        table_path = iterated_table_path(node)
        if table_path is None:
            return
        for descendant in walk_without_functions(node.body):
            if isinstance(descendant, astnodes.Assign):
                for index, target in enumerate(descendant.targets):
                    assigned = descendant.values[index] if index < len(descendant.values) else None
                    if isinstance(assigned, astnodes.Nil):
                        continue
                    if (
                        isinstance(target, astnodes.Index)
                        and static_access_path(target.value) == table_path
                    ):
                        self.add(
                            "B25",
                            target,
                            f"do not mutate {'.'.join(table_path)} while traversing it",
                        )
            elif (
                isinstance(descendant, astnodes.Call)
                and mutating_call_target(descendant) == table_path
            ):
                self.add(
                    "B25", descendant, f"do not mutate {'.'.join(table_path)} while traversing it"
                )

    def _is_top_level(self, node: astnodes.Node) -> bool:
        current = node
        while id(current) in self.parents:
            parent = self.parents[id(current)]
            if isinstance(parent, astnodes.Block):
                return isinstance(self.parents.get(id(parent)), astnodes.Chunk)
            if isinstance(parent, FUNCTION_NODES + CONTROL_BODY_NODES + (astnodes.Do,)):
                return False
            current = parent
        return False

    def _audit_scopes(self) -> None:
        ScopeAudit(self).visit_block(self.tree.body, Scope(None, set()))

    def _audit_return_arities(self) -> None:
        for function in (node for node in walk(self.tree) if isinstance(node, FUNCTION_NODES)):
            arities: set[str] = set()
            for node in walk_without_functions(function.body):
                if not isinstance(node, astnodes.Return):
                    continue
                if (
                    node.values
                    and isinstance(node.values[-1], MULTI_RESULT_NODES)
                    and not node.values[-1].wrapped
                ):
                    arities.add("variable")
                else:
                    arities.add(str(len(node.values)))
            if len(arities) > 1:
                ordered = ", ".join(sorted(arities))
                self.add(
                    "B27", function, f"function has inconsistent explicit return arities: {ordered}"
                )

    def _audit_nesting(self) -> None:
        self._visit_nesting(self.tree.body, 0)

    def _visit_nesting(self, node: astnodes.Node, depth: int) -> None:
        if isinstance(node, FUNCTION_NODES):
            self._visit_nesting(node.body, 0)
            return
        if isinstance(node, astnodes.If):
            nested = depth + 1
            if nested > self.policy.max_control_nesting:
                self.add(
                    "B37",
                    node,
                    f"control nesting depth {nested} exceeds maximum {self.policy.max_control_nesting}",
                )
            self._visit_nesting(node.test, depth)
            self._visit_nesting(node.body, nested)
            if isinstance(node.orelse, astnodes.ElseIf):
                self._visit_else_if_nesting(node.orelse, depth, nested)
            elif node.orelse is not None:
                self._visit_nesting(node.orelse, nested)
            return
        if isinstance(node, (astnodes.Fornum, astnodes.Forin, astnodes.Repeat, astnodes.While)):
            nested = depth + 1
            if nested > self.policy.max_control_nesting:
                self.add(
                    "B37",
                    node,
                    f"control nesting depth {nested} exceeds maximum {self.policy.max_control_nesting}",
                )
            for child in child_nodes(node):
                self._visit_nesting(child, nested if child is node.body else depth)
            return
        for child in child_nodes(node):
            self._visit_nesting(child, depth)

    def _visit_else_if_nesting(
        self, node: astnodes.ElseIf, condition_depth: int, body_depth: int
    ) -> None:
        self._visit_nesting(node.test, condition_depth)
        self._visit_nesting(node.body, body_depth)
        if isinstance(node.orelse, astnodes.ElseIf):
            self._visit_else_if_nesting(node.orelse, condition_depth, body_depth)
        elif node.orelse is not None:
            self._visit_nesting(node.orelse, body_depth)


class ScopeAudit:
    def __init__(self, audit: FileAudit):
        self.audit = audit

    def visit_block(self, block: astnodes.Block, scope: Scope) -> None:
        for statement in block.body:
            self.visit_statement(statement, scope)

    def visit_statement(self, node: astnodes.Node, scope: Scope) -> None:
        if isinstance(node, astnodes.LocalAssign):
            self.visit_expressions(node.values, scope)
            if (
                len(node.targets) == 1
                and len(node.values) == 1
                and direct_call_name(node.values[0]) == "require"
            ):
                imported = name_value(node.targets[0])
                if imported is not None:
                    self.audit.imported_names.add(imported)
            for target in node.targets:
                name = name_value(target)
                if name is not None:
                    self.declare(name, node, scope)
            return
        if isinstance(node, astnodes.Assign):
            self.visit_expressions(node.values, scope)
            for target in node.targets:
                root = assignment_root(target)
                if (
                    root is not None
                    and not scope.contains(root)
                    and root not in self.audit.policy.allowed_global_writes
                ):
                    self.audit.add("B01", node, f"assignment writes undeclared global {root}")
                if isinstance(target, astnodes.Index) and root in self.audit.imported_names:
                    self.audit.add("B19", target, f"do not monkey-patch imported module {root}")
                self.visit_expression(target, scope)
            return
        if isinstance(node, astnodes.LocalFunction):
            self.declare(node.name.id, node, scope)
            self.visit_function(node, scope)
            return
        if isinstance(node, astnodes.Function):
            root = assignment_root(node.name)
            self.audit_function_owner(node, root, scope)
            self.visit_function(node, scope)
            return
        if isinstance(node, astnodes.Method):
            root = assignment_root(node.source)
            self.audit_function_owner(node, root, scope)
            self.visit_function(node, scope, implicit_self=True)
            return
        if isinstance(node, astnodes.If):
            self.visit_expression(node.test, scope)
            self.visit_block(node.body, Scope(scope, set()))
            self.visit_else(node.orelse, scope)
            return
        if isinstance(node, astnodes.While):
            self.visit_expression(node.test, scope)
            self.visit_block(node.body, Scope(scope, set()))
            return
        if isinstance(node, astnodes.Repeat):
            child = Scope(scope, set())
            self.visit_block(node.body, child)
            self.visit_expression(node.test, child)
            return
        if isinstance(node, astnodes.Fornum):
            self.visit_expression(node.start, scope)
            self.visit_expression(node.stop, scope)
            if isinstance(node.step, astnodes.Node):
                self.visit_expression(node.step, scope)
            child = Scope(scope, set())
            self.declare(node.target.id, node, child)
            self.visit_block(node.body, child)
            return
        if isinstance(node, astnodes.Forin):
            self.visit_expressions(node.iter, scope)
            child = Scope(scope, set())
            for target in node.targets:
                self.declare(target.id, node, child)
            self.visit_block(node.body, child)
            return
        if isinstance(node, astnodes.Do):
            self.visit_block(node.body, Scope(scope, set()))
            return
        for child in child_nodes(node):
            if isinstance(child, astnodes.Block):
                self.visit_block(child, Scope(scope, set()))
            else:
                self.visit_expression(child, scope)

    def visit_else(self, node: astnodes.Node | None, scope: Scope) -> None:
        if isinstance(node, astnodes.ElseIf):
            self.visit_expression(node.test, scope)
            self.visit_block(node.body, Scope(scope, set()))
            self.visit_else(node.orelse, scope)
        elif isinstance(node, astnodes.Block):
            self.visit_block(node, Scope(scope, set()))

    def visit_function(
        self, node: astnodes.Node, scope: Scope, implicit_self: bool = False
    ) -> None:
        child = Scope(scope, set())
        if implicit_self:
            child.names.add("self")
        for argument in node.args:
            name = name_value(argument)
            if name is not None:
                self.declare(name, node, child)
        self.visit_block(node.body, child)

    def visit_expressions(self, nodes: Iterable[astnodes.Node], scope: Scope) -> None:
        for node in nodes:
            self.visit_expression(node, scope)

    def visit_expression(self, node: astnodes.Node, scope: Scope) -> None:
        if isinstance(node, astnodes.AnonymousFunction):
            self.visit_function(node, scope)
            return
        for child in child_nodes(node):
            self.visit_expression(child, scope)

    def declare(self, name: str, node: astnodes.Node, scope: Scope) -> None:
        if name != "_" and scope.contains(name):
            self.audit.add("B10", node, f"local {name} shadows an existing binding")
        scope.names.add(name)

    def audit_function_owner(self, node: astnodes.Node, root: str | None, scope: Scope) -> None:
        if (
            root is not None
            and not scope.contains(root)
            and root not in self.audit.policy.allowed_global_writes
        ):
            self.audit.add("B01", node, f"function declaration writes undeclared global {root}")
        if root in self.audit.imported_names:
            self.audit.add("B19", node, f"do not monkey-patch imported module {root}")


def child_nodes(node: astnodes.Node) -> Iterable[astnodes.Node]:
    for value in vars(node).values():
        if isinstance(value, astnodes.Node):
            yield value
        elif isinstance(value, list):
            yield from (item for item in value if isinstance(item, astnodes.Node))


def walk(node: astnodes.Node) -> Iterable[astnodes.Node]:
    yield node
    for child in child_nodes(node):
        yield from walk(child)


def walk_without_functions(node: astnodes.Node) -> Iterable[astnodes.Node]:
    for child in child_nodes(node):
        if isinstance(child, FUNCTION_NODES):
            continue
        yield child
        yield from walk_without_functions(child)


def name_value(node: astnodes.Node) -> str | None:
    return node.id if isinstance(node, astnodes.Name) else None


def assignment_root(node: astnodes.Node) -> str | None:
    if isinstance(node, astnodes.Index):
        return root_name(node)
    return name_value(node)


def root_name(node: astnodes.Node) -> str | None:
    current = node
    while isinstance(current, astnodes.Index):
        current = current.value
    return name_value(current)


def direct_call_name(node: astnodes.Node) -> str | None:
    if not isinstance(node, astnodes.Call):
        return None
    name = name_value(node.func)
    if name is not None:
        return name
    path = static_access_path(node.func)
    return path[1] if path and len(path) == 2 and path[0] == "_G" else None


def string_table_key(field: astnodes.Field) -> str | None:
    if isinstance(field.key, astnodes.Name) and not field.between_brackets:
        return field.key.id
    if isinstance(field.key, astnodes.String):
        return field.key.raw
    return None


def string_index_key(node: astnodes.Node) -> str | None:
    if not isinstance(node, astnodes.Index):
        return None
    if getattr(node.notation, "name", "") == "DOT" and isinstance(node.idx, astnodes.Name):
        return node.idx.id
    if isinstance(node.idx, astnodes.String):
        return node.idx.raw
    return None


def static_access_path(node: astnodes.Node) -> tuple[str, ...] | None:
    if isinstance(node, astnodes.Name):
        return (node.id,)
    if not isinstance(node, astnodes.Index):
        return None
    parent = static_access_path(node.value)
    key = string_index_key(node)
    if parent is None or key is None:
        return None
    return parent + (key,)


def iterated_table_path(node: astnodes.Forin) -> tuple[str, ...] | None:
    if len(node.iter) == 1 and isinstance(node.iter[0], astnodes.Call):
        iterator = node.iter[0]
        if direct_call_name(iterator) in {"ipairs", "next", "pairs"} and iterator.args:
            return static_access_path(iterator.args[0])
    if len(node.iter) >= 2 and name_value(node.iter[0]) == "next":
        return static_access_path(node.iter[1])
    return None


def mutating_call_target(node: astnodes.Call) -> tuple[str, ...] | None:
    name = direct_call_name(node)
    if name == "rawset" and node.args:
        return static_access_path(node.args[0])
    path = static_access_path(node.func)
    if path in {("table", "insert"), ("table", "remove"), ("table", "sort")} and node.args:
        return static_access_path(node.args[0])
    return None


def static_table_key(field: astnodes.Field) -> tuple[str, object] | None:
    string_key = string_table_key(field)
    if string_key is not None:
        return "string", string_key
    if isinstance(field.key, astnodes.Number):
        return "number", field.key.n
    if isinstance(field.key, astnodes.TrueExpr):
        return "boolean", True
    if isinstance(field.key, astnodes.FalseExpr):
        return "boolean", False
    return None


def audit_metrics(source: Path, policy: Policy) -> list[Violation]:
    violations: list[Violation] = []
    source_files = sorted(path.as_posix() for path in source.rglob("*.lua") if path.is_file())
    for file_result in lizard.analyze(source_files, lans=["lua"]):
        lines = Path(file_result.filename).read_text(encoding="utf-8").splitlines()
        for function in file_result.function_list:
            evidence = (
                lines[function.start_line - 1].strip() if function.start_line <= len(lines) else ""
            )
            path = Path(function.filename).as_posix()
            if function.parameter_count > policy.max_parameters:
                violations.append(
                    Violation(
                        path,
                        function.start_line,
                        "B40",
                        f"function has {function.parameter_count} parameters; maximum is {policy.max_parameters}",
                        evidence,
                    )
                )
            if function.nloc > policy.max_function_nloc:
                violations.append(
                    Violation(
                        path,
                        function.start_line,
                        "B41",
                        f"function has {function.nloc} NLOC; maximum is {policy.max_function_nloc}",
                        evidence,
                    )
                )
    return violations


def audit_source(source: Path, policy: Policy) -> list[Violation]:
    violations: list[Violation] = []
    paths = sorted(path for path in source.rglob("*.lua") if path.is_file())
    if not paths:
        raise ValueError(f"no Lua files found under {source}")
    for path in paths:
        text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except Exception as error:
            violations.append(
                Violation(path.as_posix(), 1, "PARSE", f"Lua parse failed: {error}", "")
            )
            continue
        violations.extend(FileAudit(path, text, tree, policy).audit())
    violations.extend(audit_metrics(source, policy))
    return sorted(set(violations))


def baseline_counts(violations: Iterable[Violation]) -> Counter[tuple[str, str, str]]:
    return Counter(
        (violation.path, violation.rule, violation.fingerprint) for violation in violations
    )


def load_baseline(path: Path) -> Counter[tuple[str, str, str]]:
    if not path.exists():
        return Counter()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"unsupported baseline schema in {path}")
    entries = payload.get("violations")
    if not isinstance(entries, list):
        raise ValueError(f"baseline violations must be an array in {path}")
    counts: Counter[tuple[str, str, str]] = Counter()
    for item in entries:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or not all(isinstance(value, str) for value in item[:3])
            or not isinstance(item[3], int)
            or isinstance(item[3], bool)
            or item[3] < 1
        ):
            raise ValueError(f"invalid baseline violation entry in {path}: {item!r}")
        key = (item[0], item[1], item[2])
        if key in counts:
            raise ValueError(f"duplicate baseline violation entry in {path}: {item!r}")
        counts[key] = item[3]
    return counts


def write_baseline(path: Path, violations: Iterable[Violation]) -> None:
    counts = baseline_counts(violations)
    entries = [
        "    " + json.dumps([key[0], key[1], key[2], count], separators=(",", ":"))
        for key, count in sorted(counts.items())
    ]
    content = '{\n  "schema_version": 1,\n  "violations": [\n'
    content += ",\n".join(entries)
    content += "\n  ]\n}\n"
    path.write_text(content, encoding="utf-8")


def compare_baseline(
    violations: list[Violation], baseline: Counter[tuple[str, str, str]]
) -> tuple[list[Violation], Counter[tuple[str, str, str]]]:
    remaining = baseline.copy()
    new: list[Violation] = []
    for violation in violations:
        key = (violation.path, violation.rule, violation.fingerprint)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            new.append(violation)
    return new, +remaining
