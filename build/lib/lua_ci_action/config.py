from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FormatConfig:
    enabled: bool
    paths: tuple[Path, ...]
    config: Path | None


@dataclass(frozen=True)
class SyntaxConfig:
    enabled: bool
    paths: tuple[Path, ...]


@dataclass(frozen=True)
class ComplexityConfig:
    enabled: bool
    source: Path
    ccn_warning: int
    parameter_warning: int
    healthy_ccn: int
    healthy_parameters: int


@dataclass(frozen=True)
class SourcePolicyConfig:
    enabled: bool
    source: Path
    allowed_global_writes: frozenset[str]
    max_control_nesting: int
    max_parameters: int
    max_function_nloc: int
    baseline: Path | None


@dataclass(frozen=True)
class Config:
    format: FormatConfig
    syntax: SyntaxConfig
    complexity: ComplexityConfig
    source_policy: SourcePolicyConfig


TOP_LEVEL_KEYS = frozenset({"schema_version", "format", "syntax", "complexity", "source_policy"})
FORMAT_KEYS = frozenset({"enabled", "paths", "config"})
SYNTAX_KEYS = frozenset({"enabled", "paths"})
COMPLEXITY_KEYS = frozenset(
    {"enabled", "source", "ccn_warning", "parameter_warning", "healthy_ccn", "healthy_parameters"}
)
SOURCE_POLICY_KEYS = frozenset(
    {
        "enabled",
        "source",
        "allowed_global_writes",
        "max_control_nesting",
        "max_parameters",
        "max_function_nloc",
        "baseline",
    }
)


def load_config(path: Path, workspace: Path) -> Config:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"configuration file does not exist: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"configuration is not valid JSON: {error}") from error
    root = object_value(payload, "configuration")
    reject_unknown(root, TOP_LEVEL_KEYS, "configuration")
    if root.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    format_data = section(root, "format")
    syntax_data = section(root, "syntax")
    complexity_data = section(root, "complexity")
    policy_data = section(root, "source_policy")
    reject_unknown(format_data, FORMAT_KEYS, "format")
    reject_unknown(syntax_data, SYNTAX_KEYS, "syntax")
    reject_unknown(complexity_data, COMPLEXITY_KEYS, "complexity")
    reject_unknown(policy_data, SOURCE_POLICY_KEYS, "source_policy")
    config = Config(
        format=FormatConfig(
            enabled=boolean(format_data, "enabled", True),
            paths=paths(format_data, "paths", ("src", "tests"), workspace),
            config=optional_path(format_data, "config", workspace),
        ),
        syntax=SyntaxConfig(
            enabled=boolean(syntax_data, "enabled", True),
            paths=paths(syntax_data, "paths", ("src", "tests"), workspace),
        ),
        complexity=ComplexityConfig(
            enabled=boolean(complexity_data, "enabled", True),
            source=path_value(complexity_data, "source", "src", workspace),
            ccn_warning=positive_integer(complexity_data, "ccn_warning", 17),
            parameter_warning=positive_integer(complexity_data, "parameter_warning", 8),
            healthy_ccn=positive_integer(complexity_data, "healthy_ccn", 10),
            healthy_parameters=positive_integer(complexity_data, "healthy_parameters", 5),
        ),
        source_policy=SourcePolicyConfig(
            enabled=boolean(policy_data, "enabled", True),
            source=path_value(policy_data, "source", "src", workspace),
            allowed_global_writes=string_set(policy_data, "allowed_global_writes"),
            max_control_nesting=positive_integer(policy_data, "max_control_nesting", 5),
            max_parameters=positive_integer(policy_data, "max_parameters", 9),
            max_function_nloc=positive_integer(policy_data, "max_function_nloc", 100),
            baseline=optional_path(policy_data, "baseline", workspace),
        ),
    )
    if config.complexity.healthy_ccn > config.complexity.ccn_warning:
        raise ValueError("healthy_ccn must not exceed ccn_warning")
    if config.complexity.healthy_parameters > config.complexity.parameter_warning:
        raise ValueError("healthy_parameters must not exceed parameter_warning")
    return config


def object_value(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object")
    return value


def section(root: dict[str, Any], key: str) -> dict[str, Any]:
    return object_value(root.get(key, {}), key)


def reject_unknown(value: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} has unknown field(s): {', '.join(unknown)}")


def boolean(value: dict[str, Any], key: str, default: bool) -> bool:
    result = value.get(key, default)
    if not isinstance(result, bool):
        raise ValueError(f"{key} must be a boolean")
    return result


def positive_integer(value: dict[str, Any], key: str, default: int) -> int:
    result = value.get(key, default)
    if not isinstance(result, int) or isinstance(result, bool) or result < 1:
        raise ValueError(f"{key} must be a positive integer")
    return result


def safe_path(raw: str, workspace: Path) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError("paths must be non-empty strings")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise ValueError(f"path must be relative to the workspace: {raw}")
    resolved = (workspace / candidate).resolve()
    try:
        resolved.relative_to(workspace.resolve())
    except ValueError as error:
        raise ValueError(f"path escapes the workspace: {raw}") from error
    return candidate


def path_value(value: dict[str, Any], key: str, default: str, workspace: Path) -> Path:
    return safe_path(value.get(key, default), workspace)


def optional_path(value: dict[str, Any], key: str, workspace: Path) -> Path | None:
    raw = value.get(key)
    return None if raw is None else safe_path(raw, workspace)


def paths(
    value: dict[str, Any], key: str, default: tuple[str, ...], workspace: Path
) -> tuple[Path, ...]:
    raw = value.get(key, list(default))
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{key} must be a non-empty array")
    return tuple(safe_path(item, workspace) for item in raw)


def string_set(value: dict[str, Any], key: str) -> frozenset[str]:
    raw = value.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) and item for item in raw):
        raise ValueError(f"{key} must be an array of non-empty strings")
    if len(raw) != len(set(raw)):
        raise ValueError(f"{key} must not contain duplicates")
    return frozenset(raw)
