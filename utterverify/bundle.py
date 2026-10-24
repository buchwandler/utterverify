"""Canonical SSMD bundle orchestration."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import tomlkit

from .config import (
    CONFIG_FILENAME,
    UNSET,
    VerifyConfig,
    _UnsetType,
    default_config,
    ensure_config,
    load_config,
    update_config,
    write_config,
)
from .core import _load_handoff, _open_verifier, _preflight_loaded, _verify_loaded
from .errors import BundleError, HandoffError, UtterVerifyError
from .types import BundleVerifyResult, UnitStatus, UnitVerifyResult

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


@dataclass(frozen=True, slots=True)
class BundleVerifyStart:
    workspace: Path
    kind: Literal["book", "document"]
    variant: str
    selector: str
    selected_unit_ids: tuple[str, ...]
    config_path: Path
    output_dir: Path
    backend: str
    model: str | None


@dataclass(frozen=True, slots=True)
class BundleVerifyProgress:
    id: str
    source_number: int
    index: int
    total: int
    status: str


def _bundle_output_directory(identity: _BundleIdentity, variant: str) -> Path:
    """Create a safe UtterVerify output directory confined to the bundle."""
    variant_name = _safe_variant(variant)
    parent = identity.root / "utterverify"
    if parent.is_symlink():
        raise BundleError("bundle utterverify output directory must not be a symlink")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BundleError(f"cannot create UtterVerify output directory: {parent}") from exc
    if not parent.is_dir():
        raise BundleError(f"bundle utterverify output path is not a directory: {parent}")

    output = parent / variant_name
    if output.is_symlink():
        raise BundleError(f"UtterVerify variant output directory must not be a symlink: {output}")
    try:
        output.mkdir(parents=True, exist_ok=True)
        resolved = output.resolve(strict=True)
        resolved.relative_to(identity.root)
    except (OSError, ValueError) as exc:
        raise BundleError(f"UtterVerify output directory escapes bundle: {output}") from exc
    if output.is_symlink() or not resolved.is_dir():
        raise BundleError(f"invalid UtterVerify output directory: {output}")
    return resolved


def discover_bundle_variants(bundle: str | Path) -> tuple[str, ...]:
    """Return complete safe variants present in both upstream bundle components."""
    return _discover_variants(_load_bundle(bundle))


def resolve_bundle_variant(bundle: str | Path, variant: str | None) -> str:
    """Select an explicit variant or the only complete variant in a bundle."""
    return _resolve_variant(_load_bundle(bundle), variant)


def _discover_variants(identity: _BundleIdentity) -> tuple[str, ...]:
    renderplan = _variant_directories(identity, "renderplan")
    voicerender = _variant_directories(identity, "voicerender")
    candidates = renderplan & voicerender
    for candidate in candidates:
        _safe_variant(candidate)
    return tuple(sorted(candidates))


def _variant_directories(identity: _BundleIdentity, component: str) -> set[str]:
    parent = identity.root / component
    if parent.is_symlink():
        raise BundleError(f"{component} directory must not be a symlink")
    if not parent.exists():
        return set()
    try:
        resolved = parent.resolve(strict=True)
        resolved.relative_to(identity.root)
    except (OSError, ValueError) as exc:
        raise BundleError(f"{component} directory is missing or escapes the bundle") from exc
    if not resolved.is_dir():
        raise BundleError(f"{component} path is not a directory")
    return {child.name for child in resolved.iterdir() if not child.is_symlink() and child.is_dir()}


def _resolve_variant(identity: _BundleIdentity, variant: str | None) -> str:
    if variant is not None:
        selected = _safe_variant(variant)
        for component in ("renderplan", "voicerender"):
            path = identity.root / component / selected
            if path.is_symlink():
                raise BundleError(
                    f"{component} variant directory must not be a symlink: {selected}"
                )
            _contained_dir(path, identity.root)
        return selected

    candidates = _discover_variants(identity)
    if not candidates:
        raise BundleError(
            "no complete rendered variant exists under both renderplan/ and voicerender/"
        )
    if len(candidates) > 1:
        choices = "\n".join(f"  {candidate}" for candidate in candidates)
        raise BundleError(
            f"multiple rendered variants are available:\n{choices}\nselect one with --variant"
        )
    return candidates[0]


def init_bundle_config(
    bundle: str | Path,
    *,
    variant: str | None = None,
    config: VerifyConfig | None = None,
    overwrite: bool = False,
) -> Path:
    identity = _load_bundle(bundle)
    variant_name = _resolve_variant(identity, variant)
    out = _bundle_output_directory(identity, variant_name)
    path = out / CONFIG_FILENAME
    if path.is_symlink():
        raise BundleError(f"UtterVerify config must not be a symlink: {path}")
    if path.exists() and not overwrite:
        return path
    write_config(path, config or default_config(), overwrite=overwrite)
    return path


def configure_bundle(
    bundle: str | Path,
    *,
    variant: str | None = None,
    backend: str | None = None,
    device: str | None = None,
    model: str | None | _UnsetType = UNSET,
    unset_model: bool = False,
    normalization: str | None = None,
    use_language_hint: bool | None = None,
    fail_on_review: bool | None = None,
    fail_fast: bool | None = None,
) -> Path:
    """Create or update the selected variant's UtterVerify configuration."""
    if model is not UNSET and unset_model:
        raise BundleError("--model and --unset-model cannot be used together")
    identity = _load_bundle(bundle)
    variant_name = _resolve_variant(identity, variant)
    config_path = _bundle_output_directory(identity, variant_name) / CONFIG_FILENAME
    if config_path.is_symlink():
        raise BundleError(f"UtterVerify config must not be a symlink: {config_path}")
    existing = load_config(config_path) if config_path.exists() else default_config()
    if (
        backend is not None
        and backend != existing.backend
        and existing.model is not None
        and model is UNSET
        and not unset_model
    ):
        raise BundleError(
            "changing the backend while a model is configured requires --model or --unset-model"
        )

    effective_backend = backend or existing.backend
    selected_model = model
    if unset_model:
        selected_model = None
    elif isinstance(model, str) and effective_backend == "whispercpp":
        selected_model = _canonicalize_whisper_model(model)

    update_config(
        config_path,
        backend=backend,
        device=device,
        model=selected_model,
        normalization=normalization,
        use_language_hint=use_language_hint,
        fail_on_review=fail_on_review,
        fail_fast=fail_fast,
    )
    return config_path


def _canonicalize_whisper_model(value: str) -> str:
    candidate = Path(value).expanduser()
    if candidate.exists():
        if not candidate.is_file():
            raise BundleError(f"whisper.cpp model path is not a regular file: {candidate}")
        return str(candidate.resolve())
    try:
        from speechscope import ModelManager

        model = ModelManager(packaged_catalog=True).show(value, backend="whispercpp")
        ref = model.get("ref")
        if not isinstance(ref, str) or not ref.startswith("whispercpp:"):
            raise ValueError("catalog returned a non-canonical whisper.cpp model reference")
        return ref
    except Exception as exc:
        raise BundleError(
            f"cannot resolve whisper.cpp model {value!r} from a local file or SpeechScope catalog: {exc}"
        ) from exc


def verify_bundle(
    bundle: str | Path,
    *,
    variant: str | None = None,
    chapters: str = "all",
    on_start: Callable[[BundleVerifyStart], None] | None = None,
    on_progress: Callable[[BundleVerifyProgress], None] | None = None,
) -> BundleVerifyResult:
    identity = _load_bundle(bundle)
    variant_name = _resolve_variant(identity, variant)
    output_dir = _bundle_output_directory(identity, variant_name)

    # Deliberately first owned artifact: config exists before RenderPlan loading and
    # before SpeechScope backend/model initialization.
    config_path = output_dir / CONFIG_FILENAME
    if config_path.is_symlink():
        raise BundleError(f"UtterVerify config must not be a symlink: {config_path}")
    config, created = ensure_config(config_path)
    config_sha256 = hashlib.sha256(config_path.read_bytes()).hexdigest()

    units = _resolve_units(identity, variant_name, chapters)
    prepared: list[tuple[_Unit, Any, Any, tuple[tuple[Any, str, Path], ...], Path]] = []
    for unit in units:
        unit_name = _safe_variant(unit.id)
        plan, manifest = _load_handoff(unit.renderplan, unit.render_dir)
        verified_wavs = _preflight_loaded(plan, manifest, unit.render_dir)
        unit_output = output_dir / unit_name
        if unit_output.is_symlink():
            raise BundleError(
                f"UtterVerify unit output directory must not be a symlink: {unit_output}"
            )
        try:
            unit_output.mkdir(parents=True, exist_ok=True)
            unit_output.resolve(strict=True).relative_to(output_dir)
        except (OSError, ValueError) as exc:
            raise BundleError(
                f"UtterVerify unit output escapes output directory: {unit_output}"
            ) from exc
        prepared.append((unit, plan, manifest, verified_wavs, unit_output))

    if on_start is not None:
        on_start(
            BundleVerifyStart(
                workspace=identity.root,
                kind=identity.kind,
                variant=variant_name,
                selector=chapters,
                selected_unit_ids=tuple(unit.id for unit in units),
                config_path=config_path,
                output_dir=output_dir,
                backend=config.backend,
                model=config.model,
            )
        )
    verifier = _open_verifier(config)
    results: list[UnitVerifyResult] = []
    try:
        with verifier:
            for index, (unit, plan, manifest, verified_wavs, unit_output) in enumerate(
                prepared, start=1
            ):
                result = _verify_loaded(
                    plan,
                    manifest,
                    plan_path=unit.renderplan,
                    render_root=unit.render_dir,
                    output_dir=unit_output,
                    config=config,
                    unit_id=unit.id,
                    verifier=verifier,
                    preflight=verified_wavs,
                )
                results.append(result)
                if on_progress is not None:
                    on_progress(
                        BundleVerifyProgress(
                            id=unit.id,
                            source_number=unit.source_number,
                            index=index,
                            total=len(units),
                            status=result.status,
                        )
                    )
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
        speechscope_backend=config.backend,
        speechscope_device=config.device,
        speechscope_model=config.model,
        normalization=config.normalization,
        use_language_hint=config.use_language_hint,
        config_sha256=config_sha256,
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


def _bundle_status(units: list[UnitVerifyResult]) -> UnitStatus:
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
    speechscope = tomlkit.table()
    speechscope.add("backend", result.speechscope_backend)
    speechscope.add("device", result.speechscope_device)
    if result.speechscope_model is not None:
        speechscope.add("model", result.speechscope_model)
    speechscope.add("normalization", result.normalization)
    speechscope.add("use_language_hint", result.use_language_hint)
    if result.config_sha256 is not None:
        speechscope.add("config_sha256", result.config_sha256)
    doc.add("speechscope", speechscope)
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
