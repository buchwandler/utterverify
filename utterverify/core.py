"""Verification of one RenderPlan + VoiceRender directory."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

import tomlkit

from .config import CONFIG_FILENAME, VerifyConfig, ensure_config, load_config
from .errors import HandoffError
from .types import SegmentOutcome, SegmentStatus, UnitStatus, UnitVerifyResult

RENDER_MANIFEST_FILENAME = "render-manifest.toml"


def verify_renderplan(
    plan: str | Path,
    *,
    render_dir: str | Path,
    output_dir: str | Path,
    config: VerifyConfig | None = None,
    unit_id: str = "document-0001",
    verifier: Any | None = None,
) -> UnitVerifyResult:
    """Verify every rendered speech occurrence in one complete RenderPlan handoff.

    ``utterverify.toml`` is created in ``output_dir`` before RenderPlan loading or
    SpeechScope backend initialization.
    """
    plan_path = Path(plan).expanduser().resolve(strict=True)
    render_root = Path(render_dir).expanduser().resolve(strict=True)
    requested_output = Path(output_dir).expanduser()
    if requested_output.is_symlink():
        raise HandoffError(
            f"UtterVerify output directory must not be a symlink: {requested_output}"
        )
    out = Path(output_dir).expanduser().resolve(strict=False)
    try:
        out.relative_to(render_root)
    except ValueError:
        pass
    else:
        raise HandoffError("UtterVerify output directory must not be inside the VoiceRender input")
    out.mkdir(parents=True, exist_ok=True)
    config_path = out / CONFIG_FILENAME
    if config_path.is_symlink():
        raise HandoffError(f"UtterVerify config must not be a symlink: {config_path}")
    if config is None:
        selected, _ = ensure_config(config_path)
    else:
        if config_path.exists():
            selected = load_config(config_path)
        else:
            selected, _ = ensure_config(config_path, config)

    parsed_plan, manifest = _load_handoff(plan_path, render_root)
    preflight = _preflight_loaded(parsed_plan, manifest, render_root)
    owns_verifier = verifier is None
    entered = False
    if owns_verifier:
        verifier = _open_verifier(selected)
        try:
            verifier.__enter__()
            entered = True
        except Exception as exc:
            raise HandoffError(f"SpeechScope backend failed: {exc}") from exc
    assert verifier is not None
    try:
        unit_result = _verify_loaded(
            parsed_plan,
            manifest,
            plan_path=plan_path,
            render_root=render_root,
            output_dir=out,
            config=selected,
            unit_id=unit_id,
            verifier=verifier,
            preflight=preflight,
        )
        _write_unit_report(unit_result)
        return unit_result
    finally:
        if owns_verifier and entered:
            try:
                verifier.__exit__(None, None, None)
            except Exception as exc:
                raise HandoffError(f"SpeechScope backend failed to close: {exc}") from exc


def _load_handoff(
    plan_path: Path,
    render_root: Path,
    *,
    plan_set: Any | None = None,
    set_entry: Any | None = None,
) -> tuple[Any, Any]:
    try:
        from renderplan import load_manifest, load_plan, validate_manifest_handoff
    except ImportError as exc:  # pragma: no cover - packaging/runtime guard
        raise HandoffError("renderplan is required to validate VoiceRender handoffs") from exc

    try:
        if (plan_set is None) != (set_entry is None):
            raise ValueError("RenderPlan set and chapter entry must be provided together")
        parsed_plan = load_plan(plan_path)
        if plan_set is not None and set_entry is not None:
            from renderplan import validate_set_chapter_plan

            validate_set_chapter_plan(plan_set, set_entry, parsed_plan)
        manifest_path = render_root / RENDER_MANIFEST_FILENAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise HandoffError(f"complete VoiceRender manifest is missing: {manifest_path}")
        manifest = load_manifest(manifest_path)
        validate_manifest_handoff(parsed_plan, manifest, require_complete=True)
    except Exception as exc:
        raise HandoffError(f"invalid RenderPlan / VoiceRender handoff: {exc}") from exc
    return parsed_plan, manifest


def _preflight_loaded(
    plan: Any,
    manifest: Any,
    render_root: Path,
) -> tuple[tuple[Any, str, Path, str], ...]:
    """Validate all deterministic WAV paths and checksums before any ASR work."""
    by_key = {
        (artifact.segment_number, artifact.segment_id): artifact for artifact in manifest.speech
    }
    verified: list[tuple[Any, str, Path, str]] = []
    for speech in plan.speech:
        segment_id = speech.segment_id or speech.computed_id()
        key = (speech.number, segment_id)
        artifact = by_key.get(key)
        if artifact is None:
            raise HandoffError(
                f"manifest is missing speech occurrence {speech.number}:{segment_id}"
            )
        wav, wav_sha256 = _verified_artifact_path(render_root, speech, artifact)
        verified.append((speech, segment_id, wav, wav_sha256))
    return tuple(verified)


def _verify_loaded(
    plan: Any,
    manifest: Any,
    *,
    plan_path: Path,
    render_root: Path,
    output_dir: Path,
    config: VerifyConfig,
    unit_id: str,
    verifier: Any,
    preflight: tuple[tuple[Any, str, Path, str], ...] | None = None,
) -> UnitVerifyResult:
    if output_dir.is_symlink():
        raise HandoffError(f"UtterVerify output directory must not be a symlink: {output_dir}")
    verified = (
        preflight if preflight is not None else _preflight_loaded(plan, manifest, render_root)
    )
    outcomes: list[SegmentOutcome] = []
    for speech, segment_id, wav, wav_sha256 in verified:
        report_path = output_dir / f"{speech.number:06d}.speechscope.json"
        if report_path.is_symlink():
            raise HandoffError(f"SpeechScope report output must not be a symlink: {report_path}")
        try:
            request = _verification_request(speech, wav, config)
            result = verifier.analyze(request)
            status_value = str(result.evaluation.get("status", "not_evaluated"))
            if status_value not in {"pass", "review", "fail", "not_evaluated"}:
                status: SegmentStatus = "review"
            else:
                status = cast(SegmentStatus, status_value)
            result.write_json(report_path)
            outcomes.append(
                SegmentOutcome(
                    number=speech.number,
                    segment_id=segment_id,
                    reference_text=speech.text,
                    wav=wav,
                    wav_sha256=wav_sha256,
                    report=report_path,
                    status=status,
                    language=speech.language,
                )
            )
            if config.fail_fast and status in {"fail", "error"}:
                break
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                SegmentOutcome(
                    number=speech.number,
                    segment_id=segment_id,
                    reference_text=speech.text,
                    wav=wav,
                    wav_sha256=wav_sha256,
                    report=None,
                    status="error",
                    language=speech.language,
                    message=str(exc),
                )
            )
            if config.fail_fast:
                break

    if config.fail_fast and len(outcomes) < len(verified):
        for speech, segment_id, wav, wav_sha256 in verified[len(outcomes) :]:
            outcomes.append(
                SegmentOutcome(
                    number=speech.number,
                    segment_id=segment_id,
                    reference_text=speech.text,
                    wav=wav,
                    wav_sha256=wav_sha256,
                    report=None,
                    status="not_evaluated",
                    language=speech.language,
                    message="not evaluated because fail-fast stopped after an earlier failure/error",
                )
            )

    return UnitVerifyResult(
        id=unit_id,
        renderplan=plan_path,
        render_plan_id=plan.plan_id or plan.computed_id(),
        source_plan_id=plan.source_plan_id,
        render_profile_id=plan.render_profile_id,
        render_dir=render_root,
        manifest_id=getattr(manifest, "manifest_id", None),
        output_dir=output_dir,
        segments=tuple(outcomes),
        status=_unit_status(outcomes),
    )


def _not_evaluated_loaded(
    plan: Any,
    manifest: Any,
    *,
    plan_path: Path,
    render_root: Path,
    output_dir: Path,
    unit_id: str,
    preflight: tuple[tuple[Any, str, Path, str], ...],
    message: str,
) -> UnitVerifyResult:
    outcomes = tuple(
        SegmentOutcome(
            number=speech.number,
            segment_id=segment_id,
            reference_text=speech.text,
            wav=wav,
            wav_sha256=wav_sha256,
            report=None,
            status="not_evaluated",
            language=speech.language,
            message=message,
        )
        for speech, segment_id, wav, wav_sha256 in preflight
    )
    return UnitVerifyResult(
        id=unit_id,
        renderplan=plan_path,
        render_plan_id=plan.plan_id or plan.computed_id(),
        source_plan_id=plan.source_plan_id,
        render_profile_id=plan.render_profile_id,
        render_dir=render_root,
        manifest_id=getattr(manifest, "manifest_id", None),
        output_dir=output_dir,
        segments=outcomes,
        status="review",
    )


def _verified_artifact_path(render_root: Path, speech: Any, artifact: Any) -> tuple[Path, str]:
    expected_name = f"{speech.number:06d}.wav"
    if artifact.wav != expected_name or getattr(speech, "wav", expected_name) != expected_name:
        raise HandoffError(f"unexpected WAV name for speech {speech.number}")
    candidate = render_root / artifact.wav
    if candidate.is_symlink():
        raise HandoffError(f"rendered WAV must not be a symlink: {candidate}")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise HandoffError(f"rendered WAV is missing: {candidate}") from exc
    try:
        resolved.relative_to(render_root)
    except ValueError as exc:
        raise HandoffError(f"rendered WAV escapes render directory: {candidate}") from exc
    if not resolved.is_file():
        raise HandoffError(f"rendered WAV is not a regular file: {candidate}")
    expected_hash = str(artifact.sha256).removeprefix("sha256:")
    got_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    if got_hash != expected_hash:
        raise HandoffError(f"rendered WAV checksum mismatch: {candidate.name}")
    return resolved, f"sha256:{got_hash}"


def _open_verifier(config: VerifyConfig) -> Any:
    try:
        from speechscope import Verifier
    except ImportError as exc:  # pragma: no cover - packaging/runtime guard
        raise HandoffError("speechscope is required for validation") from exc
    return Verifier(
        backend=config.backend,
        device=config.device,
        model=config.model,
    )


def _verification_request(speech: Any, wav: Path, config: VerifyConfig) -> Any:
    try:
        from speechscope import ThresholdPolicy, VerificationRequest
    except ImportError as exc:  # pragma: no cover
        raise HandoffError("speechscope is required for validation") from exc
    thresholds = ThresholdPolicy(**config.threshold_kwargs())
    return VerificationRequest(
        wav,
        reference_text=speech.text,
        backend=config.backend,
        language=speech.language if config.use_language_hint else None,
        device=config.device,
        model=config.model,
        normalization=config.normalization,
        thresholds=thresholds,
    )


def _unit_status(outcomes: list[SegmentOutcome]) -> UnitStatus:
    if any(item.status == "error" for item in outcomes):
        return "error"
    if any(item.status == "fail" for item in outcomes):
        return "fail"
    if any(item.status in {"review", "not_evaluated"} for item in outcomes):
        return "review"
    return "pass"


def segment_json_summary(outcome: SegmentOutcome) -> str:
    return json.dumps(
        {
            "number": outcome.number,
            "segment_id": outcome.segment_id,
            "wav": str(outcome.wav),
            "report": str(outcome.report) if outcome.report else None,
            "status": outcome.status,
            "message": outcome.message,
        },
        ensure_ascii=False,
    )


def _write_unit_report(result: UnitVerifyResult) -> Path:
    report_path = result.output_dir / "utterverify-report.toml"
    if report_path.is_symlink():
        raise HandoffError(f"UtterVerify report must not be a symlink: {report_path}")

    doc = tomlkit.document()
    doc.add("format", "utterverify.report")
    doc.add("schema_version", 1)
    doc.add("id", result.id)
    doc.add("status", result.status)
    doc.add("renderplan", str(result.renderplan))
    doc.add("render_plan_id", result.render_plan_id)
    doc.add("source_plan_id", result.source_plan_id)
    doc.add("render_profile_id", result.render_profile_id)
    doc.add("render_dir", str(result.render_dir))
    if result.manifest_id is not None:
        doc.add("manifest_id", result.manifest_id)
    doc.add("output_dir", str(result.output_dir))
    doc.add("segments", len(result.segments))
    doc.add("passed", result.passed)
    doc.add("reviewed", result.reviewed)
    doc.add("failed", result.failed)
    doc.add("errors", result.errors)

    segment_tables = tomlkit.aot()
    for segment in result.segments:
        table = tomlkit.table()
        table.add("number", segment.number)
        table.add("segment_id", segment.segment_id)
        table.add("reference_text", segment.reference_text)
        table.add("wav", str(segment.wav))
        table.add("wav_sha256", segment.wav_sha256)
        if segment.report is not None:
            table.add("report", str(segment.report))
        table.add("status", segment.status)
        if segment.language is not None:
            table.add("language", segment.language)
        if segment.message is not None:
            table.add("message", segment.message)
        segment_tables.append(table)
    doc.add("segment", segment_tables)
    _atomic_text(report_path, tomlkit.dumps(doc))
    return report_path


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
