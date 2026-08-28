from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import complexity, file_complexity, github_comments, reporting, source_policy
from .config import Config, load_config, safe_path


SYNTAX_LINE = re.compile(r"^(?:[^:]+:\s+)?(.+?):(\d+):\s*(.*)$")


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run reusable Lua code-quality checks")
    parser.add_argument(
        "--config-path",
        default=os.environ.get("INPUT_CONFIG-PATH", ".lua-ci-actionrc"),
    )
    parser.add_argument("--base-ref", default=os.environ.get("INPUT_BASE-REF", ""))
    parser.add_argument("--head-ref", default=os.environ.get("INPUT_HEAD-REF", "HEAD"))
    parser.add_argument(
        "--github-token",
        default=os.environ.get("INPUT_GITHUB-TOKEN", ""),
    )
    inputs = parser.parse_args(arguments)
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
    try:
        config_path = workspace / safe_path(inputs.config_path, workspace)
        config = load_config(config_path, workspace)
        os.chdir(workspace)
        target = github_comments.target_from_environment(os.environ)
        base_ref = inputs.base_ref or (target.base_sha if target is not None else "")
        return run(config, base_ref, inputs.head_ref, inputs.github_token, target)
    except (OSError, ValueError, StopIteration, subprocess.CalledProcessError) as error:
        detail = (
            error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        )
        annotation("error", "Lua CI configuration", detail)
        print(f"Lua CI failed: {detail}", file=sys.stderr)
        return 1


def run(
    config: Config,
    base_ref: str = "",
    head_ref: str = "HEAD",
    github_token: str = "",
    target: github_comments.PullRequestTarget | None = None,
) -> int:
    base_ref = normalize_base_ref(base_ref)
    head_ref = head_ref.strip() or "HEAD"
    changed_paths = complexity.changed_lua_paths(base_ref, head_ref) if base_ref else ()
    failed = False
    format_result: reporting.CheckResult | None = None
    if config.format.enabled:
        format_result = check_format(config)
        failed |= not format_result.passed
    syntax_result: reporting.CheckResult | None = None
    if config.syntax.enabled:
        syntax_result = check_syntax(config)
        failed |= not syntax_result.passed
    complexity_result: reporting.ComplexityResult | None = None
    if config.complexity.enabled:
        complexity_result = check_complexity(config, base_ref, head_ref)
    source_policy_result: reporting.CheckResult | None = None
    if config.source_policy.enabled:
        source_policy_result = check_source_policy(config)
        failed |= not source_policy_result.passed
    report_data = reporting.ReportData(
        format=format_result,
        syntax=syntax_result,
        source_policy=source_policy_result,
        complexity=complexity_result,
        changed_paths=changed_paths,
    )
    project_report = reporting.render_project(report_data)
    pull_request_report = reporting.render_pull_request(report_data) if base_ref else ""
    write_summary(project_report + (f"\n{pull_request_report}" if pull_request_report else ""))
    if github_token and target is not None and pull_request_report:
        try:
            github_comments.GitHubCommentClient(github_token, target).publish(
                project_report, pull_request_report
            )
        except (OSError, ValueError, RuntimeError) as error:
            annotation("warning", "Lua CI comments", str(error))
            print(f"Lua CI comments failed: {error}", file=sys.stderr)
    if failed:
        print("Lua CI found blocking quality problems.", file=sys.stderr)
        return 1
    print("Lua CI passed.")
    return 0


def check_format(config: Config) -> reporting.CheckResult:
    require_paths(config.format.paths)
    files = lua_files(config.format.paths)
    command = format_command(config, config.format.paths)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        return reporting.CheckResult(
            name="Format",
            passed=True,
            checked_paths=tuple(path.as_posix() for path in files),
            findings=(),
        )
    findings: list[reporting.Finding] = []
    for path in files:
        file_result = subprocess.run(
            format_command(config, (path,)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if file_result.returncode == 0:
            continue
        annotation("error", "StyLua format", "Run StyLua and commit the result.", path)
        findings.append(reporting.Finding(path.as_posix(), None, "Format", "StyLua"))
    if not findings:
        annotation("error", "StyLua format", "StyLua failed.")
        findings.append(reporting.Finding("", None, "Format", "StyLua failed"))
    return reporting.CheckResult(
        name="Format",
        passed=False,
        checked_paths=tuple(path.as_posix() for path in files),
        findings=tuple(findings),
    )


def format_command(config: Config, paths: tuple[Path, ...]) -> list[str]:
    command = ["stylua", "--check", "--color", "never"]
    if config.format.config is not None:
        require_file(config.format.config)
        command.extend(["--config-path", config.format.config.as_posix()])
    command.extend(path.as_posix() for path in paths)
    return command


def check_syntax(config: Config) -> reporting.CheckResult:
    files = lua_files(config.syntax.paths)
    findings: list[reporting.Finding] = []
    for path in files:
        result = subprocess.run(
            ["luac5.1", "-p", path.as_posix()],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode == 0:
            continue
        message = result.stdout.strip() or "Lua 5.1 parser rejected the file"
        match = SYNTAX_LINE.match(message)
        if match:
            line = int(match.group(2))
            detail = match.group(3)
            annotation("error", "Lua 5.1 syntax", detail, path, line)
        else:
            line = None
            detail = message
            annotation("error", "Lua 5.1 syntax", message, path)
        findings.append(reporting.Finding(path.as_posix(), line, "Syntax", detail))
    if findings:
        print("\n".join(finding.message for finding in findings), file=sys.stderr)
    return reporting.CheckResult(
        name="Syntax",
        passed=not findings,
        checked_paths=tuple(path.as_posix() for path in files),
        findings=tuple(findings),
    )


def check_complexity(config: Config, base_ref: str, head_ref: str) -> reporting.ComplexityResult:
    require_directory(config.complexity.source)
    limits = complexity.Limits(
        ccn_warning=config.complexity.ccn_warning,
        parameter_warning=config.complexity.parameter_warning,
        healthy_ccn=config.complexity.healthy_ccn,
        healthy_parameters=config.complexity.healthy_parameters,
    )
    result = complexity.analyze_source(config.complexity.source)
    changed = (
        complexity.changed_line_ranges(base_ref, head_ref, config.complexity.source)
        if base_ref
        else {}
    )
    function_report = complexity.render_report(result, changed, limits)
    files = file_complexity.file_complexities(config.complexity.source, result.functions)
    changes = (
        file_complexity.complexity_changes(config.complexity.source, base_ref, head_ref)
        if base_ref
        else None
    )
    file_report = (
        file_complexity.render_change_report(changes)
        if changes is not None
        else file_complexity.render_report(files)
    )
    report = f"{function_report}\n\n{file_report}"
    print(report)
    if base_ref:
        for function in result.functions:
            findings = complexity.violations(function, limits)
            if findings and complexity.overlaps_changed_lines(function, changed):
                annotation(
                    "warning",
                    "Lua complexity",
                    f"{function.name}: {'; '.join(findings)}",
                    Path(function.path),
                    function.start_line,
                    function.end_line,
                )
    return reporting.ComplexityResult(result, limits, files, changes)


def check_source_policy(config: Config) -> reporting.CheckResult:
    policy_config = config.source_policy
    require_directory(policy_config.source)
    policy = source_policy.Policy(
        allowed_global_writes=policy_config.allowed_global_writes,
        max_control_nesting=policy_config.max_control_nesting,
        max_parameters=policy_config.max_parameters,
        max_function_nloc=policy_config.max_function_nloc,
    )
    violations = source_policy.audit_source(policy_config.source, policy)
    baseline: Counter[tuple[str, str, str]] = Counter()
    if policy_config.baseline is not None:
        require_file(policy_config.baseline)
        baseline = source_policy.load_baseline(policy_config.baseline)
    new, stale = source_policy.compare_baseline(violations, baseline)
    findings: list[reporting.Finding] = []
    for violation in new:
        annotation(
            "error",
            f"Lua source policy {violation.rule}",
            violation.message,
            Path(violation.path),
            violation.line,
        )
        print(f"{violation.path}:{violation.line}: {violation.rule}: {violation.message}")
        findings.append(
            reporting.Finding(
                violation.path,
                violation.line,
                violation.rule,
                violation.message,
            )
        )
    for (path, rule, fingerprint), count in sorted(stale.items()):
        message = f"stale baseline {fingerprint} x{count}; regenerate the baseline"
        annotation("error", f"Lua source policy {rule}", message, Path(path))
        print(f"{path}: {rule}: {message}")
        findings.append(reporting.Finding(path, None, rule, f"Stale baseline {fingerprint}", count))
    checked_paths = tuple(
        sorted(path.as_posix() for path in policy_config.source.rglob("*.lua") if path.is_file())
    )
    return reporting.CheckResult(
        name="Source policy",
        passed=not new and not stale,
        checked_paths=checked_paths,
        findings=tuple(findings),
    )


def lua_files(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    require_paths(paths)
    files: set[Path] = set()
    for path in paths:
        if path.is_file() and path.suffix == ".lua":
            files.add(path)
        elif path.is_dir():
            files.update(candidate for candidate in path.rglob("*.lua") if candidate.is_file())
    if not files:
        raise ValueError("configured syntax paths contain no Lua files")
    return tuple(sorted(files))


def normalize_base_ref(base_ref: str) -> str:
    value = base_ref.strip()
    return "" if value and set(value) == {"0"} else value


def require_paths(paths: tuple[Path, ...]) -> None:
    missing = [path.as_posix() for path in paths if not path.exists()]
    if missing:
        raise ValueError(f"configured path(s) do not exist: {', '.join(missing)}")


def require_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"configured file does not exist: {path}")


def require_directory(path: Path) -> None:
    if not path.is_dir():
        raise ValueError(f"configured directory does not exist: {path}")


def write_summary(content: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(content)


def annotation(
    level: str,
    title: str,
    message: str,
    path: Path | None = None,
    line: int | None = None,
    end_line: int | None = None,
) -> None:
    properties = [f"title={escape_property(title)}"]
    if path is not None:
        properties.append(f"file={escape_property(path.as_posix())}")
    if line is not None:
        properties.append(f"line={line}")
    if end_line is not None:
        properties.append(f"endLine={end_line}")
    print(f"::{level} {','.join(properties)}::{escape_data(message)}")


def escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")
