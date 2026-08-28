#!/usr/bin/env python3

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import lizard

from .complexity import (
    FunctionMetric,
    git,
    markdown as markdown_code,
    parse_patch_path,
    resolve_ref,
)


MAX_EXISTING_FILE_CHANGES = 10


@dataclass(frozen=True)
class FileComplexity:
    path: str
    function_count: int
    total_ccn: int


@dataclass(frozen=True)
class FileComplexityChange:
    path: str
    base_path: str
    base_total_ccn: int
    total_ccn: int
    delta_ccn: int


@dataclass(frozen=True)
class ComplexityChanges:
    existing_files: tuple[FileComplexityChange, ...]
    new_files: tuple[FileComplexity, ...]


def file_complexities(
    source: Path, functions: tuple[FunctionMetric, ...]
) -> tuple[FileComplexity, ...]:
    functions_by_path: defaultdict[str, list[FunctionMetric]] = defaultdict(list)
    for function in functions:
        functions_by_path[function.path].append(function)
    return tuple(
        sorted(
            (
                FileComplexity(
                    path=path.as_posix(),
                    function_count=len(functions_by_path[path.as_posix()]),
                    total_ccn=sum(function.ccn for function in functions_by_path[path.as_posix()]),
                )
                for path in source.rglob("*.lua")
                if path.is_file()
            ),
            key=lambda metric: (-metric.total_ccn, metric.path),
        )
    )


def revision_file_complexities(source: Path, revision: str) -> dict[str, FileComplexity]:
    paths = sorted(
        path
        for path in git(
            "ls-tree", "-r", "--name-only", revision, "--", source.as_posix()
        ).splitlines()
        if path.endswith(".lua")
    )
    metrics: dict[str, FileComplexity] = {}
    for path in paths:
        result = lizard.analyze_file.analyze_source_code(path, git("show", f"{revision}:{path}"))
        metrics[path] = FileComplexity(
            path=path,
            function_count=len(result.function_list),
            total_ccn=sum(function.cyclomatic_complexity for function in result.function_list),
        )
    return metrics


def complexity_changes(source: Path, base_ref: str, head_ref: str) -> ComplexityChanges:
    base_revision = resolve_ref(base_ref)
    head_revision = resolve_ref(head_ref)
    merge_base = git("merge-base", base_revision, head_revision).strip()
    base_metrics = revision_file_complexities(source, merge_base)
    head_metrics = revision_file_complexities(source, head_revision)
    changed_paths = git(
        "diff",
        "--name-status",
        "--find-renames",
        merge_base,
        head_revision,
        "--",
        source.as_posix(),
    )
    existing_files: list[FileComplexityChange] = []
    new_files: list[FileComplexity] = []
    for line in changed_paths.splitlines():
        fields = line.split("\t")
        status = fields[0][0]
        if status == "A":
            path = parse_patch_path(fields[1])
            if path in head_metrics:
                new_files.append(head_metrics[path])
        elif status == "R":
            base_path = parse_patch_path(fields[1])
            path = parse_patch_path(fields[2])
            if base_path in base_metrics and path in head_metrics:
                base_total = base_metrics[base_path].total_ccn
                total = head_metrics[path].total_ccn
                if total != base_total:
                    existing_files.append(
                        FileComplexityChange(path, base_path, base_total, total, total - base_total)
                    )
            elif path in head_metrics:
                new_files.append(head_metrics[path])
        elif status not in {"C", "D"}:
            path = parse_patch_path(fields[1])
            if path in base_metrics and path in head_metrics:
                base_total = base_metrics[path].total_ccn
                total = head_metrics[path].total_ccn
                if total != base_total:
                    existing_files.append(
                        FileComplexityChange(path, path, base_total, total, total - base_total)
                    )
        elif status == "C":
            path = parse_patch_path(fields[2])
            if path in head_metrics:
                new_files.append(head_metrics[path])
    return ComplexityChanges(
        existing_files=tuple(
            sorted(
                existing_files,
                key=lambda metric: (-abs(metric.delta_ccn), -metric.delta_ccn, metric.path),
            )
        ),
        new_files=tuple(sorted(new_files, key=lambda metric: (-metric.total_ccn, metric.path))),
    )


def render_report(metrics: tuple[FileComplexity, ...]) -> str:
    lines = [
        "## Lua file complexity",
        "",
        "> Total CCN is the sum of function cyclomatic complexity numbers in each file.",
        "",
        "| File | Total CCN | Functions |",
        "|---|---:|---:|",
    ]
    lines.extend(
        f"| `{markdown_code(metric.path)}` | {metric.total_ccn} | {metric.function_count} |"
        for metric in metrics
    )
    return "\n".join(lines)


def render_change_report(changes: ComplexityChanges) -> str:
    existing_delta = sum(metric.delta_ccn for metric in changes.existing_files)
    new_total = sum(metric.total_ccn for metric in changes.new_files)
    lines = [
        "## Lua file complexity changes",
        "",
        "> Total CCN is the sum of function cyclomatic complexity numbers in each file.",
        "",
        f"- Existing files with CCN changes: **{len(changes.existing_files):,}**; total delta: **{existing_delta:+d}**.",
        f"- Net-new files: **{len(changes.new_files):,}**; total CCN: **{new_total:,}**.",
        "",
        "### Largest changes in existing files",
        "",
    ]
    if changes.existing_files:
        lines.extend(
            [
                "| File | Base CCN | PR CCN | Change |",
                "|---|---:|---:|---:|",
            ]
        )
        for metric in changes.existing_files[:MAX_EXISTING_FILE_CHANGES]:
            path = f"`{markdown_code(metric.path)}`"
            if metric.base_path != metric.path:
                path += f" (from `{markdown_code(metric.base_path)}`)"
            lines.append(
                f"| {path} | {metric.base_total_ccn} | {metric.total_ccn} | {metric.delta_ccn:+d} |"
            )
        omitted = len(changes.existing_files) - MAX_EXISTING_FILE_CHANGES
        if omitted > 0:
            lines.extend(["", f"{omitted:,} additional existing-file CCN changes are omitted."])
    else:
        lines.append("No existing production Lua file changed total CCN.")
    lines.extend(["", "### Net-new files", ""])
    if changes.new_files:
        lines.extend(
            [
                "| File | Total CCN | Functions |",
                "|---|---:|---:|",
            ]
        )
        lines.extend(
            f"| `{markdown_code(metric.path)}` | {metric.total_ccn} | {metric.function_count} |"
            for metric in changes.new_files
        )
    else:
        lines.append("No net-new production Lua files.")
    return "\n".join(lines)
