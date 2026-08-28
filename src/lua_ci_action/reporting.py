from __future__ import annotations

from dataclasses import dataclass

from . import complexity, file_complexity


PROJECT_MARKER = "<!-- lua-ci-action:project -->"
PULL_REQUEST_MARKER = "<!-- lua-ci-action:pull-request -->"
MAX_REPORT_ROWS = 50
MAX_PROJECT_ROWS = 10


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    check: str
    message: str
    count: int = 1


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    checked_paths: tuple[str, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class ComplexityResult:
    analysis: complexity.AnalysisResult
    limits: complexity.Limits
    files: tuple[file_complexity.FileComplexity, ...]
    changes: file_complexity.ComplexityChanges | None


@dataclass(frozen=True)
class ReportData:
    format: CheckResult | None
    syntax: CheckResult | None
    source_policy: CheckResult | None
    complexity: ComplexityResult | None
    changed_paths: tuple[str, ...]


def render_project(data: ReportData) -> str:
    lines = [PROJECT_MARKER, "## Lua CI Project", "", *_check_table(_checks(data))]
    if data.complexity is not None:
        lines.extend(["", *_project_complexity(data.complexity)])
    return "\n".join(lines).rstrip() + "\n"


def render_pull_request(data: ReportData) -> str:
    changed = frozenset(data.changed_paths)
    lines = [
        PULL_REQUEST_MARKER,
        "## Lua CI Pull Request",
        "",
        "| Lua files |",
        "|---:|",
        f"| {len(changed):,} |",
        "",
        *_changed_check_table(_checks(data), changed),
    ]
    findings = tuple(
        finding for check in _checks(data) for finding in check.findings if finding.path in changed
    )
    if findings:
        lines.extend(["", *_finding_table(findings)])
    if data.complexity is not None:
        lines.extend(["", *_pull_request_complexity(data.complexity, changed)])
    return "\n".join(lines).rstrip() + "\n"


def _checks(data: ReportData) -> tuple[CheckResult, ...]:
    return tuple(
        check for check in (data.format, data.syntax, data.source_policy) if check is not None
    )


def _check_table(checks: tuple[CheckResult, ...]) -> list[str]:
    lines = [
        "| Check | Status | Files | Findings |",
        "|---|---|---:|---:|",
    ]
    lines.extend(
        f"| {check.name} | {_status(check.passed)} | {len(check.checked_paths):,} | "
        f"{_finding_count(check.findings):,} |"
        for check in checks
    )
    return lines


def _changed_check_table(checks: tuple[CheckResult, ...], changed: frozenset[str]) -> list[str]:
    lines = [
        "| Check | Status | Files | Findings |",
        "|---|---|---:|---:|",
    ]
    for check in checks:
        paths = changed.intersection(check.checked_paths)
        findings = tuple(finding for finding in check.findings if finding.path in changed)
        status = _status(not findings) if paths else "—"
        lines.append(f"| {check.name} | {status} | {len(paths):,} | {_finding_count(findings):,} |")
    return lines


def _finding_table(findings: tuple[Finding, ...]) -> list[str]:
    lines = [
        "### Findings",
        "",
        "| File | Line | Check | Finding |",
        "|---|---:|---|---|",
    ]
    for finding in sorted(
        findings,
        key=lambda item: (item.path, item.line or 0, item.check, item.message),
    )[:MAX_REPORT_ROWS]:
        message = markdown(finding.message)
        if finding.count > 1:
            message = f"{message} ×{finding.count}"
        lines.append(
            f"| `{markdown(finding.path)}` | {finding.line or '—'} | "
            f"{markdown(finding.check)} | {message} |"
        )
    return lines


def _project_complexity(result: ComplexityResult) -> list[str]:
    analysis = result.analysis
    limits = result.limits
    functions = analysis.functions
    ccn_warnings = sum(function.ccn > limits.ccn_warning for function in functions)
    parameter_warnings = sum(
        function.parameters > limits.parameter_warning for function in functions
    )
    lines = [
        "## Complexity",
        "",
        "| Files | Functions | NLOC | Duplication |",
        "|---:|---:|---:|---:|",
        f"| {analysis.file_count:,} | {analysis.function_count:,} | "
        f"{analysis.source_nloc:,} | {analysis.duplicate_rate_percent:.2f}% |",
        "",
        "| Metric | Target | Within | Warnings |",
        "|---|---:|---:|---:|",
        f"| CCN | ≤ {limits.healthy_ccn} | "
        f"{sum(function.ccn <= limits.healthy_ccn for function in functions):,} | "
        f"{ccn_warnings:,} |",
        f"| Parameters | ≤ {limits.healthy_parameters} | "
        f"{sum(function.parameters <= limits.healthy_parameters for function in functions):,} | "
        f"{parameter_warnings:,} |",
    ]
    if functions:
        lines.extend(
            [
                "",
                "### Largest functions",
                "",
                "| File | Function | CCN | NLOC | Parameters |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for function in complexity.sorted_outliers(functions, limits)[:MAX_PROJECT_ROWS]:
            lines.append(
                f"| `{markdown(function.path)}:{function.start_line}` | "
                f"`{markdown(function.name)}` | {function.ccn} | {function.nloc} | "
                f"{function.parameters} |"
            )
    if result.files:
        lines.extend(
            [
                "",
                "### Largest files",
                "",
                "| File | Total CCN | Functions |",
                "|---|---:|---:|",
            ]
        )
        lines.extend(
            f"| `{markdown(metric.path)}` | {metric.total_ccn} | {metric.function_count} |"
            for metric in result.files[:MAX_PROJECT_ROWS]
        )
    return lines


def _pull_request_complexity(result: ComplexityResult, changed: frozenset[str]) -> list[str]:
    changed_files = tuple(metric for metric in result.files if metric.path in changed)
    changed_warnings = tuple(
        function
        for function in result.analysis.functions
        if function.path in changed and complexity.violations(function, result.limits)
    )
    lines = [
        "## Complexity",
        "",
        "| Files | Functions over limits |",
        "|---:|---:|",
        f"| {len(changed_files):,} | {len(changed_warnings):,} |",
    ]
    if changed_files:
        existing = {
            metric.path: metric
            for metric in (result.changes.existing_files if result.changes else ())
        }
        new = {metric.path for metric in (result.changes.new_files if result.changes else ())}
        lines.extend(
            [
                "",
                "### Files",
                "",
                "| File | Base CCN | Current CCN | Change | Functions |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for metric in changed_files[:MAX_REPORT_ROWS]:
            change = existing.get(metric.path)
            if change is not None:
                base_ccn = str(change.base_total_ccn)
                delta = f"{change.delta_ccn:+d}"
            elif metric.path in new:
                base_ccn = "—"
                delta = f"+{metric.total_ccn}"
            else:
                base_ccn = str(metric.total_ccn)
                delta = "0"
            lines.append(
                f"| `{markdown(metric.path)}` | {base_ccn} | {metric.total_ccn} | "
                f"{delta} | {metric.function_count} |"
            )
    if changed_warnings:
        lines.extend(
            [
                "",
                "### Functions over limits",
                "",
                "| File | Function | CCN | NLOC | Parameters |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for function in complexity.sorted_outliers(changed_warnings, result.limits)[
            :MAX_REPORT_ROWS
        ]:
            lines.append(
                f"| `{markdown(function.path)}:{function.start_line}` | "
                f"`{markdown(function.name)}` | {function.ccn} | {function.nloc} | "
                f"{function.parameters} |"
            )
    return lines


def _status(passed: bool) -> str:
    return "Pass" if passed else "Fail"


def _finding_count(findings: tuple[Finding, ...]) -> int:
    return sum(finding.count for finding in findings)


def markdown(value: str) -> str:
    return complexity.markdown(value)
