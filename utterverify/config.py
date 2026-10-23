"""Persisted validation configuration."""

from __future__ import annotations

import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from .errors import ConfigError

CONFIG_FILENAME = "utterverify.toml"
CONFIG_FORMAT = "utterverify.config"
CONFIG_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class VerifyConfig:
    backend: str = "redux"
    device: str = "cpu"
    model: str | None = None
    normalization: str = "basic-v1"
    use_language_hint: bool = False
    max_wer: float | None = None
    max_compound_wer: float | None = 0.0
    max_cer: float | None = 0.0
    max_mer: float | None = None
    min_reference_timestamp_coverage: float | None = None
    max_invalid_timestamp_count: int | None = None
    missing_timing: str = "review"
    fail_on_review: bool = False
    fail_fast: bool = False
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def threshold_kwargs(self) -> dict[str, Any]:
        values: dict[str, Any] = {"schema_version": "1", "missing_timing": self.missing_timing}
        for name in (
            "max_wer",
            "max_compound_wer",
            "max_cer",
            "max_mer",
            "min_reference_timestamp_coverage",
            "max_invalid_timestamp_count",
        ):
            value = getattr(self, name)
            if value is not None:
                values[name] = value
        return values


def default_config() -> VerifyConfig:
    return VerifyConfig()


def ensure_config(
    path: str | Path, config: VerifyConfig | None = None
) -> tuple[VerifyConfig, bool]:
    target = Path(path)
    if target.exists():
        return load_config(target), False
    selected = config or default_config()
    write_config(target, selected)
    return selected, True


def write_config(path: str | Path, config: VerifyConfig, *, overwrite: bool = False) -> Path:
    target = Path(path)
    if target.exists() and not overwrite:
        raise ConfigError(f"configuration already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc.add("format", CONFIG_FORMAT)
    doc.add("schema_version", CONFIG_SCHEMA_VERSION)
    doc.add(tomlkit.nl())

    scope = tomlkit.table()
    scope.add("backend", config.backend)
    scope.add("device", config.device)
    if config.model is not None:
        scope.add("model", config.model)
    scope.add("normalization", config.normalization)
    scope.add("use_language_hint", config.use_language_hint)
    doc.add("speechscope", scope)

    thresholds = tomlkit.table()
    thresholds.add("schema_version", "1")
    for name in (
        "max_wer",
        "max_compound_wer",
        "max_cer",
        "max_mer",
        "min_reference_timestamp_coverage",
        "max_invalid_timestamp_count",
    ):
        value = getattr(config, name)
        if value is not None:
            thresholds.add(name, value)
    thresholds.add("missing_timing", config.missing_timing)
    doc.add("thresholds", thresholds)

    run = tomlkit.table()
    run.add("fail_on_review", config.fail_on_review)
    run.add("fail_fast", config.fail_fast)
    doc.add("run", run)
    _atomic_text(target, tomlkit.dumps(doc))
    return target


def load_config(path: str | Path) -> VerifyConfig:
    source = Path(path)
    try:
        data = tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read configuration {source}: {exc}") from exc
    if data.get("format") != CONFIG_FORMAT or data.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ConfigError("unsupported utterverify.toml format or schema_version")
    speechscope = data.get("speechscope")
    thresholds = data.get("thresholds")
    run = data.get("run", {})
    if (
        not isinstance(speechscope, dict)
        or not isinstance(thresholds, dict)
        or not isinstance(run, dict)
    ):
        raise ConfigError("speechscope, thresholds, and run must be TOML tables")

    backend = _nonempty_text(speechscope.get("backend"), "speechscope.backend")
    device = _nonempty_text(speechscope.get("device"), "speechscope.device")
    normalization = _nonempty_text(speechscope.get("normalization"), "speechscope.normalization")
    model = speechscope.get("model")
    if model is not None:
        model = _nonempty_text(model, "speechscope.model")
    use_language_hint = speechscope.get("use_language_hint", False)
    if type(use_language_hint) is not bool:
        raise ConfigError("speechscope.use_language_hint must be boolean")
    if thresholds.get("schema_version", "1") != "1":
        raise ConfigError("thresholds.schema_version must be '1'")
    missing_timing = thresholds.get("missing_timing", "review")
    if missing_timing not in {"review", "fail"}:
        raise ConfigError("thresholds.missing_timing must be 'review' or 'fail'")

    numbers: dict[str, Any] = {}
    for name in (
        "max_wer",
        "max_compound_wer",
        "max_cer",
        "max_mer",
        "min_reference_timestamp_coverage",
    ):
        value = thresholds.get(name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ConfigError(f"thresholds.{name} must be a non-negative number")
            if name == "min_reference_timestamp_coverage" and value > 1:
                raise ConfigError("thresholds.min_reference_timestamp_coverage must be <= 1")
            numbers[name] = float(value)
        else:
            numbers[name] = None
    invalid_count = thresholds.get("max_invalid_timestamp_count")
    if invalid_count is not None and (type(invalid_count) is not int or invalid_count < 0):
        raise ConfigError("thresholds.max_invalid_timestamp_count must be a non-negative integer")
    for flag in ("fail_on_review", "fail_fast"):
        if flag in run and type(run[flag]) is not bool:
            raise ConfigError(f"run.{flag} must be boolean")

    return VerifyConfig(
        backend=backend,
        device=device,
        model=model,
        normalization=normalization,
        use_language_hint=use_language_hint,
        max_invalid_timestamp_count=invalid_count,
        missing_timing=missing_timing,
        fail_on_review=run.get("fail_on_review", False),
        fail_fast=run.get("fail_fast", False),
        **numbers,
    )


def _nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be non-empty text")
    return value


def _atomic_text(path: Path, content: str) -> None:
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(content)
        os.replace(temp_name, path)
    finally:
        if temp_name is not None and os.path.exists(temp_name):
            os.unlink(temp_name)
