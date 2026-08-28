from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import lizard


MAX_CHANGED_ROWS = 50
TOP_OUTLIER_COUNT = 10
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Limits:
    ccn_warning: int
    parameter_warning: int
    healthy_ccn: int
    healthy_parameters: int


@dataclass(frozen=True)
class FunctionMetric:
    path: str
    name: str
    start_line: int
    end_line: int
    nloc: int
    ccn: int
    parameters: int


@dataclass(frozen=True)
class AnalysisResult:
    file_count: int
    function_count: int
    source_nloc: int
    duplicate_rate_percent: float
    functions: tuple[FunctionMetric, ...]


def analyze_source(source: Path) -> AnalysisResult:
    source_files = sorted(path.as_posix() for path in source.rglob("*.lua") if path.is_file())
    if not source_files:
        raise ValueError(f"no Lua files found under {source}")
    extensions = lizard.get_extensions(["duplicate"])
    duplicate = next(extension for extension in extensions if hasattr(extension, "get_duplicates"))
    file_results = list(lizard.analyze(source_files, exts=extensions, lans=["lua"]))
    list(duplicate.get_duplicates())
    functions = tuple(
        sorted(
            (
                FunctionMetric(
                    path=Path(function.filename).as_posix(),
                    name=function.name,
                    start_line=function.start_line,
                    end_line=function.end_line,
                    nloc=function.nloc,
                    ccn=function.cyclomatic_complexity,
                    parameters=function.parameter_count,
                )
                for file_result in file_results
                for function in file_result.function_list
            ),
            key=lambda function: (function.path, function.start_line, function.name),
        )
    )
    return AnalysisResult(
        file_count=len(file_results),
        function_count=len(functions),
        source_nloc=sum(file_result.nloc for file_result in file_results),
        duplicate_rate_percent=(duplicate.duplicate_rate() or 0.0) * 100,
        functions=functions,
    )


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={Path.cwd()}", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout


def resolve_ref(ref: str) -> str:
    revision = git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
        raise ValueError(f"Git ref did not resolve to a commit: {ref}")
    return revision


def changed_line_ranges(
    base_ref: str, head_ref: str, source: Path
) -> dict[str, tuple[tuple[int, int], ...]]:
    base_revision = resolve_ref(base_ref)
    head_revision = resolve_ref(head_ref)
    merge_base = git("merge-base", base_revision, head_revision).strip()
    patch = git(
        "diff",
        "--find-renames",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        merge_base,
        head_revision,
        "--",
        source.as_posix(),
    )
    ranges: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    current_path: str | None = None
    for line in patch.splitlines():
        if line.startswith("+++ "):
            current_path = parse_patch_path(line[4:])
            continue
        match = HUNK_RE.match(line)
        if not match or not current_path:
            continue
        start = max(1, int(match.group(3)))
        length = int(match.group(4) or 1)
        ranges[current_path].append((start, start if length == 0 else start + length - 1))
    return {path: tuple(path_ranges) for path, path_ranges in ranges.items()}


def parse_patch_path(value: str) -> str | None:
    path = value.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if path.startswith('"') and path.endswith('"'):
        path = bytes(path[1:-1], "utf-8").decode("unicode_escape")
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return Path(path).as_posix()


def violations(function: FunctionMetric, limits: Limits) -> tuple[str, ...]:
    result: list[str] = []
    if function.ccn > limits.ccn_warning:
        result.append(f"CCN {function.ccn} > {limits.ccn_warning}")
    if function.parameters > limits.parameter_warning:
        result.append(f"parameters {function.parameters} > {limits.parameter_warning}")
    return tuple(result)


def overlaps_changed_lines(
    function: FunctionMetric, changed: dict[str, tuple[tuple[int, int], ...]]
) -> bool:
    return any(
        function.start_line <= end and start <= function.end_line
        for start, end in changed.get(function.path, ())
    )


def sorted_outliers(functions: tuple[FunctionMetric, ...], limits: Limits) -> list[FunctionMetric]:
    return sorted(
        functions,
        key=lambda function: (
            -max(function.ccn / limits.ccn_warning, function.parameters / limits.parameter_warning),
            -function.ccn,
            -function.nloc,
            -function.parameters,
            function.path,
            function.start_line,
        ),
    )


def render_report(
    result: AnalysisResult,
    changed: dict[str, tuple[tuple[int, int], ...]],
    limits: Limits,
) -> str:
    warning_functions = tuple(
        function for function in result.functions if violations(function, limits)
    )
    changed_warnings = tuple(
        function for function in warning_functions if overlaps_changed_lines(function, changed)
    )
    lines = [
        "## Lua complexity",
        "",
        "> Advisory only. Complexity findings do not fail the action.",
        "",
        f"Analyzed **{result.function_count:,} functions** across **{result.file_count:,} files** and "
        f"**{result.source_nloc:,} source NLOC**.",
        "",
        "| Metric | Healthy target | Within target | Warning ceiling | Warnings |",
        "|---|---:|---:|---:|---:|",
        f"| Cyclomatic complexity | ≤ {limits.healthy_ccn} | "
        f"{sum(function.ccn <= limits.healthy_ccn for function in result.functions):,} | "
        f"> {limits.ccn_warning} | {sum(function.ccn > limits.ccn_warning for function in result.functions):,} |",
        f"| Parameters | ≤ {limits.healthy_parameters} | "
        f"{sum(function.parameters <= limits.healthy_parameters for function in result.functions):,} | "
        f"> {limits.parameter_warning} | "
        f"{sum(function.parameters > limits.parameter_warning for function in result.functions):,} |",
        "",
        f"- Functions above a warning ceiling: **{len(warning_functions):,}**",
        f"- Changed functions above a warning ceiling: **{len(changed_warnings):,}**",
        f"- Duplicate code: **{result.duplicate_rate_percent:.2f}%**",
        "",
        "### Largest current outliers",
        "",
        "| Location | Function | CCN | NLOC | Parameters |",
        "|---|---|---:|---:|---:|",
    ]
    for function in sorted_outliers(result.functions, limits)[:TOP_OUTLIER_COUNT]:
        lines.append(
            f"| `{markdown(function.path)}:{function.start_line}` | `{markdown(function.name)}` | "
            f"{function.ccn} | {function.nloc} | {function.parameters} |"
        )
    if changed_warnings:
        lines.extend(
            ["", "### Changed warnings", "", "| Location | Function | Finding |", "|---|---|---|"]
        )
        for function in sorted_outliers(changed_warnings, limits)[:MAX_CHANGED_ROWS]:
            finding = ", ".join(violations(function, limits))
            lines.append(
                f"| `{markdown(function.path)}:{function.start_line}` | `{markdown(function.name)}` | {finding} |"
            )
    return "\n".join(lines)


def markdown(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("`", "'").replace("|", "\\|")
