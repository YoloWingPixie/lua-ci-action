from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

from . import complexity, file_complexity, source_policy
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
    inputs = parser.parse_args(arguments)
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", Path.cwd())).resolve()
    try:
        config_path = workspace / safe_path(inputs.config_path, workspace)
        config = load_config(config_path, workspace)
        os.chdir(workspace)
        return run(config, inputs.base_ref, inputs.head_ref)
    except (OSError, ValueError, StopIteration, subprocess.CalledProcessError) as error:
        detail = (
            error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
        )
        annotation("error", "Lua CI configuration", detail)
        print(f"Lua CI failed: {detail}", file=sys.stderr)
        return 1


def run(config: Config, base_ref: str = "", head_ref: str = "HEAD") -> int:
    summary: list[str] = ["# Lua CI", ""]
    failed = False
    if config.format.enabled:
        passed, detail = check_format(config)
        failed |= not passed
        summary.extend(check_summary("StyLua format", passed, detail))
    if config.syntax.enabled:
        passed, detail = check_syntax(config)
        failed |= not passed
        summary.extend(check_summary("Lua 5.1 syntax", passed, detail))
    if config.complexity.enabled:
        report = check_complexity(config, base_ref, head_ref)
        summary.extend([report, ""])
    if config.source_policy.enabled:
        passed, detail = check_source_policy(config)
        failed |= not passed
        summary.extend(check_summary("Lua source policy", passed, detail))
    write_summary("\n".join(summary).rstrip() + "\n")
    if failed:
        print("Lua CI found blocking quality problems.", file=sys.stderr)
        return 1
    print("Lua CI passed.")
    return 0


def check_format(config: Config) -> tuple[bool, str]:
    require_paths(config.format.paths)
    command = ["stylua", "--check", "--color", "never"]
    if config.format.config is not None:
        require_file(config.format.config)
        command.extend(["--config-path", config.format.config.as_posix()])
    command.extend(path.as_posix() for path in config.format.paths)
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode == 0:
        return True, f"Checked {len(config.format.paths)} configured path(s)."
    annotation("error", "StyLua format", "Run StyLua locally and commit the formatted files.")
    return False, "StyLua found files that need formatting."


def check_syntax(config: Config) -> tuple[bool, str]:
    files = lua_files(config.syntax.paths)
    failures: list[str] = []
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
        failures.append(message)
        match = SYNTAX_LINE.match(message)
        if match:
            annotation(
                "error", "Lua 5.1 syntax", match.group(3), Path(match.group(1)), int(match.group(2))
            )
        else:
            annotation("error", "Lua 5.1 syntax", message, path)
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return False, f"{len(failures)} of {len(files)} file(s) failed to parse."
    return True, f"Parsed {len(files)} file(s) with Lua 5.1."


def check_complexity(config: Config, base_ref: str, head_ref: str) -> str:
    require_directory(config.complexity.source)
    limits = complexity.Limits(
        ccn_warning=config.complexity.ccn_warning,
        parameter_warning=config.complexity.parameter_warning,
        healthy_ccn=config.complexity.healthy_ccn,
        healthy_parameters=config.complexity.healthy_parameters,
    )
    result = complexity.analyze_source(config.complexity.source)
    base_ref = base_ref.strip()
    if base_ref and set(base_ref) == {"0"}:
        base_ref = ""
    head_ref = head_ref.strip() or "HEAD"
    changed = (
        complexity.changed_line_ranges(base_ref, head_ref, config.complexity.source)
        if base_ref
        else {}
    )
    function_report = complexity.render_report(result, changed, limits)
    file_report = (
        file_complexity.render_change_report(
            file_complexity.complexity_changes(config.complexity.source, base_ref, head_ref)
        )
        if base_ref
        else file_complexity.render_report(
            file_complexity.file_complexities(config.complexity.source, result.functions)
        )
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
    return report


def check_source_policy(config: Config) -> tuple[bool, str]:
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
    for violation in new:
        annotation(
            "error",
            f"Lua source policy {violation.rule}",
            violation.message,
            Path(violation.path),
            violation.line,
        )
        print(f"{violation.path}:{violation.line}: {violation.rule}: {violation.message}")
    for (path, rule, fingerprint), count in sorted(stale.items()):
        message = f"stale baseline {fingerprint} x{count}; regenerate the baseline"
        annotation("error", f"Lua source policy {rule}", message, Path(path))
        print(f"{path}: {rule}: {message}")
    if new or stale:
        return False, f"{len(new)} new and {sum(stale.values())} stale violation(s)."
    return True, f"No new violations. The baseline tracks {len(violations)} legacy violation(s)."


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


def check_summary(name: str, passed: bool, detail: str) -> list[str]:
    status = "Passed" if passed else "Failed"
    return [f"## {name}: {status}", "", detail, ""]


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
