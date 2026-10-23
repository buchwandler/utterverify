"""Canonical SSMD bundle orchestration."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import tomlkit

from .config import CONFIG_FILENAME, VerifyConfig, ensure_config, write_config
from .core import _load_handoff, _open_verifier, _verify_loaded
from .errors import BundleError, HandoffError, UtterVerifyError
from .types import BundleVerifyResult, UnitVerifyResult

BOOK_FORMAT = "ssmdconvert.book"
DOCUMENT_FORMAT = "ssmdconvert.document"
REPORT_FILENAME = "utterverify-report.toml"


@dataclass(frozen=True, slots=True)
class _BundleIdentity:
    root: Path
    kind: Literal["book", "document"]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _Unit:
    id: str
    source_number: int
    renderplan: Path
    render_dir: Path


def init_bundle_config(
    bundle: str | Path,
    *,
    variant: str,
    config: VerifyConfig | None = None,
    overwrite: bool = False,
) -> Path:
    identity = _load_bundle(bundle)
    variant_name = _safe_variant(variant)
    out = identity.root / "utterverify" / variant_name
    out.mkdir(parents=True, exist_ok=True)
    path = out / CONFIG_FILENAME
    if path.exists() and not overwrite:
        return path
    write_config(path, config or VerifyConfig(), overwrite=overwrite)
    return path


def verify_bundle(
    bundle: str | Path,
    *,
    variant: str,
    chapters: str = "all",
) -> BundleVerifyResult:
    identity = _load_bundle(bundle)
    variant_name = _safe_variant(variant)
    output_dir = (identity.root / "utterverify" / variant_name).resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Deliberately first owned artifact: config exists before RenderPlan loading and
    # before SpeechScope backend/model initialization.
    config_path = output_dir / CONFIG_FILENAME
    config, created = ensure_config(config_path)

    units = _resolve_units(identity, variant_name, chapters)
    verifier = _open_verifier(config)
    results: list[UnitVerifyResult] = []
    try:
        with verifier:
            for unit in units:
                unit_output = output_dir / unit.id
                unit_output.mkdir(parents=True, exist_ok=True)
                plan, manifest = _load_handoff(unit.renderplan, unit.render_dir)
                result = _verify_loaded(
                    plan,
                    manifest,
                    plan_path=unit.renderplan,
                    render_root=unit.render_dir,
                    output_dir=unit_output,
                    config=config,
                    unit_id=unit.id,
                    verifier=verifier,
                )
                results.append(result)
                if config.fail_fast and result.status in {"fail", "error"}:
                    break
    except UtterVerifyError:
        raise
    except Exception as exc:
        raise HandoffError(f"SpeechScope backend failed: {exc}") from exc

    status = _bundle_status(results)
    report_path = output_dir / REPORT_FILENAME
    bundle_result = BundleVerifyResult(
        bundle=identity.root,
        kind=identity.kind,
        variant=variant_name,
        output_dir=output_dir,
        config_path=config_path,
        report_path=report_path,
        config_created=created,
        units=tuple(results),
        status=status,
        fail_on_review=config.fail_on_review,
    )
    _write_report(bundle_result)
    return bundle_result


def _load_bundle(value: str | Path) -> _BundleIdentity:
    path = Path(value).expanduser()
    if path.is_symlink():
        raise BundleError("bundle root must not be a symlink")
    try:
        root = path.resolve(strict=True)
    except OSError as exc:
        raise BundleError(f"cannot resolve bundle root: {path}") from exc
    if not root.is_dir() or not root.name.endswith(".ssmd-bundle"):
        raise BundleError("bundle input must be the canonical NAME.ssmd-bundle directory")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise BundleError("bundle manifest.json must be a regular non-symlink file")
    try:
        if manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise BundleError("bundle manifest.json exceeds the 2 MiB size limit")
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read bundle manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise BundleError("unsupported bundle manifest schema")
    fmt = data.get("format")
    if fmt == BOOK_FORMAT:
        kind: Literal["book", "document"] = "book"
    elif fmt == DOCUMENT_FORMAT:
        kind = "document"
    else:
        raise BundleError(f"unsupported bundle format: {fmt!r}")
    return _BundleIdentity(root=root, kind=kind, manifest=data)


def _resolve_units(identity: _BundleIdentity, variant: str, chapters: str) -> tuple[_Unit, ...]:
    renderplan_variant = _contained_dir(identity.root / "renderplan" / variant, identity.root)
    render_variant = _contained_dir(identity.root / "voicerender" / variant, identity.root)
    if identity.kind == "document":
        if chapters.strip().casefold() != "all":
            raise BundleError("document bundles only support --chapters all")
        document = identity.manifest.get("document")
        document_id = document.get("id") if isinstance(document, dict) else None
        if document_id != "document-0001":
            raise BundleError("document bundle must use canonical id document-0001")
        plan = _contained_file(renderplan_variant / "document.renderplan.toml", renderplan_variant)
        render_dir = _contained_dir(render_variant / document_id, render_variant)
        return (_Unit(document_id, 1, plan, render_dir),)

    set_path = _contained_file(renderplan_variant / "renderplan-set.toml", renderplan_variant)
    try:
        from renderplan import load_renderplan_set, validate_renderplan_set

        plan_set = load_renderplan_set(set_path)
        validate_renderplan_set(plan_set)
        chapter_entries = plan_set.chapters
    except Exception as exc:
        raise BundleError(f"invalid renderplan-set.toml: {exc}") from exc
    selected = _select_numbers(chapters, {item.source_number for item in chapter_entries})
    units: list[_Unit] = []
    for entry in chapter_entries:
        if entry.source_number not in selected:
            continue
        plan = _contained_posix_file(renderplan_variant, entry.renderplan)
        render_dir = _contained_dir(render_variant / entry.id, render_variant)
        units.append(_Unit(entry.id, entry.source_number, plan, render_dir))
    return tuple(units)


def _safe_variant(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or Path(value).name != value
        or PureWindowsPath(value).name != value
    ):
        raise BundleError("--variant must be an exact safe directory basename")
    return value


def _select_numbers(selector: str, available: set[int]) -> set[int]:
    if selector.strip().casefold() == "all":
        return set(available)
    result: set[int] = set()
    for part in selector.split(","):
        match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", part)
        if not match:
            raise BundleError(f"invalid chapter selector: {selector}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise BundleError(f"invalid chapter selector: {selector}")
        result.update(range(start, end + 1))
    missing = result - available
    if missing:
        raise BundleError(f"chapter selector names unavailable source numbers: {sorted(missing)}")
    return result


def _contained_dir(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BundleError(f"required directory is missing: {path}") from exc
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise BundleError(f"directory escapes bundle root: {path}") from exc
    if not resolved.is_dir():
        raise BundleError(f"expected directory: {path}")
    return resolved


def _contained_file(path: Path, root: Path) -> Path:
    if path.is_symlink():
        raise BundleError(f"required file must not be a symlink: {path}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise BundleError(f"required file is missing: {path}") from exc
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise BundleError(f"file escapes root: {path}") from exc
    if not resolved.is_file():
        raise BundleError(f"expected regular file: {path}")
    return resolved


def _contained_posix_file(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise BundleError("RenderPlan set contains an unsafe path")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise BundleError("RenderPlan set contains an unsafe path")
    return _contained_file(root.joinpath(*posix.parts), root)


def _bundle_status(units: list[UnitVerifyResult]) -> str:
    if any(unit.status == "error" for unit in units):
        return "error"
    if any(unit.status == "fail" for unit in units):
        return "fail"
    if any(unit.status == "review" for unit in units):
        return "review"
    return "pass"


def _write_report(result: BundleVerifyResult) -> None:
    doc = tomlkit.document()
    doc.add("format", "utterverify.bundle-report")
    doc.add("schema_version", 1)
    doc.add("kind", result.kind)
    doc.add("variant", result.variant)
    doc.add("status", result.status)
    doc.add("config", result.config_path.name)
    doc.add("segments", result.segments)
    doc.add("passed", result.passed)
    doc.add("reviewed", result.reviewed)
    doc.add("failed", result.failed)
    doc.add("errors", result.errors)
    doc.add("exit_code", result.exit_code)
    units = tomlkit.aot()
    for unit in result.units:
        table = tomlkit.table()
        table.add("id", unit.id)
        table.add("status", unit.status)
        table.add("renderplan", _rel_or_abs(unit.renderplan, result.bundle))
        table.add("render_dir", _rel_or_abs(unit.render_dir, result.bundle))
        table.add("output_dir", _rel_or_abs(unit.output_dir, result.bundle))
        table.add("segments", len(unit.segments))
        table.add("passed", unit.passed)
        table.add("reviewed", unit.reviewed)
        table.add("failed", unit.failed)
        table.add("errors", unit.errors)
        units.append(table)
    doc.add("units", units)
    _atomic_text(result.report_path, tomlkit.dumps(doc))


def _rel_or_abs(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


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
