"""Verification of one RenderPlan + VoiceRender directory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

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
        return _verify_loaded(
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
    finally:
        if owns_verifier and entered:
            try:
                verifier.__exit__(None, None, None)
            except Exception as exc:
                raise HandoffError(f"SpeechScope backend failed to close: {exc}") from exc


def _load_handoff(plan_path: Path, render_root: Path) -> tuple[Any, Any]:
    try:
        from renderplan import load_manifest, load_plan
    except ImportError as exc:  # pragma: no cover - packaging/runtime guard
        raise HandoffError("renderplan is required to validate VoiceRender handoffs") from exc
    manifest_path = render_root / RENDER_MANIFEST_FILENAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise HandoffError(f"complete VoiceRender manifest is missing: {manifest_path}")
    try:
        parsed_plan = load_plan(plan_path)
        manifest = load_manifest(manifest_path)
        manifest.validate_against(parsed_plan)
    except Exception as exc:
        raise HandoffError(f"invalid RenderPlan / VoiceRender handoff: {exc}") from exc
    if not manifest.complete:
        raise HandoffError(
            "VoiceRender manifest is partial; validation requires a complete handoff"
        )
    return parsed_plan, manifest


def _preflight_loaded(
    plan: Any,
    manifest: Any,
    render_root: Path,
) -> tuple[tuple[Any, str, Path], ...]:
    """Validate all deterministic WAV paths and checksums before any ASR work."""
    by_key = {
        (artifact.segment_number, artifact.segment_id): artifact for artifact in manifest.speech
    }
    verified: list[tuple[Any, str, Path]] = []
    for speech in plan.speech:
        segment_id = speech.segment_id or speech.computed_id()
        key = (speech.number, segment_id)
        artifact = by_key.get(key)
        if artifact is None:
            raise HandoffError(
                f"manifest is missing speech occurrence {speech.number}:{segment_id}"
            )
        verified.append(
            (speech, segment_id, _verified_artifact_path(render_root, speech, artifact))
        )
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
    preflight: tuple[tuple[Any, str, Path], ...] | None = None,
) -> UnitVerifyResult:
    if output_dir.is_symlink():
        raise HandoffError(f"UtterVerify output directory must not be a symlink: {output_dir}")
    verified = (
        preflight if preflight is not None else _preflight_loaded(plan, manifest, render_root)
    )
    outcomes: list[SegmentOutcome] = []
    for speech, segment_id, wav in verified:
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
                    wav=wav,
                    report=report_path,
                    status=status,
                )
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append(
                SegmentOutcome(
                    number=speech.number,
                    segment_id=segment_id,
                    wav=wav,
                    report=None,
                    status="error",
                    message=str(exc),
                )
            )
            if config.fail_fast:
                break
    return UnitVerifyResult(
        id=unit_id,
        renderplan=plan_path,
        render_dir=render_root,
        output_dir=output_dir,
        segments=tuple(outcomes),
        status=_unit_status(outcomes),
    )


def _verified_artifact_path(render_root: Path, speech: Any, artifact: Any) -> Path:
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
    return resolved


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
