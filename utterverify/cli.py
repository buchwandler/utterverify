"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from . import __version__
from .bundle import configure_bundle, init_bundle_config, verify_bundle
from .config import (
    CONFIG_FILENAME,
    UNSET,
    VerifyConfig,
    _UnsetType,
    default_config,
    load_config,
    update_config,
)
from .core import verify_renderplan
from .doctor import DoctorResult, doctor_bundle, run_doctor
from .errors import BundleError, ConfigError, UtterVerifyError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="utterverify",
        description="Validate VoiceRender speech WAVs against RenderPlan text with SpeechScope.",
    )
    parser.add_argument("--version", action="version", version=f"utterverify {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    direct_config = commands.add_parser(
        "configure", help="Create or update a direct verification config"
    )
    direct_config.add_argument("directory", type=Path)
    direct_config.add_argument("--backend", "--engine", dest="backend")
    direct_config.add_argument("--device")
    direct_config.add_argument("--model")
    direct_config.add_argument("--unset-model", action="store_true")
    direct_config.add_argument("--normalization")
    direct_config.add_argument(
        "--use-language-hint", action=argparse.BooleanOptionalAction, default=None
    )
    direct_config.add_argument(
        "--fail-on-review", action=argparse.BooleanOptionalAction, default=None
    )
    direct_config.add_argument("--fail-fast", action=argparse.BooleanOptionalAction, default=None)

    direct = commands.add_parser("verify", help="Verify one RenderPlan handoff")
    direct.add_argument("plan", type=Path)
    direct.add_argument("--render-dir", type=Path, required=True)
    direct.add_argument("--output-dir", type=Path, required=True)
    direct.add_argument("--json", action="store_true")

    bundle = commands.add_parser("bundle", help="Canonical .ssmd-bundle workflows")
    bundle_commands = bundle.add_subparsers(dest="bundle_command", required=True)

    init = bundle_commands.add_parser("init", help="Create utterverify/<variant>/utterverify.toml")
    init.add_argument("bundle", type=Path)
    init.add_argument("--variant")
    init.add_argument("--backend", "--engine", dest="backend", default="redux")
    init.add_argument("--device", default="cpu")
    init.add_argument("--model")
    init.add_argument("--normalization", default="basic-v1")
    init.add_argument("--use-language-hint", action="store_true")
    init.add_argument("--fail-on-review", action="store_true")
    init.add_argument("--fail-fast", action="store_true")
    init.add_argument("--force", action="store_true")

    configure = bundle_commands.add_parser("configure", help="Update a bundle validation config")
    configure.add_argument("bundle", type=Path)
    configure.add_argument("--variant")
    configure.add_argument("--backend", "--engine", dest="backend")
    configure.add_argument("--device")
    configure.add_argument("--model")
    configure.add_argument("--unset-model", action="store_true")
    configure.add_argument("--normalization")
    configure.add_argument(
        "--use-language-hint", action=argparse.BooleanOptionalAction, default=None
    )
    configure.add_argument("--fail-on-review", action=argparse.BooleanOptionalAction, default=None)
    configure.add_argument("--fail-fast", action=argparse.BooleanOptionalAction, default=None)

    verify = bundle_commands.add_parser("verify", help="Verify a rendered bundle variant")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--variant")
    verify.add_argument("--chapters", default="all")
    verify.add_argument("--json", action="store_true")

    doctor = commands.add_parser("doctor", help="Check local validation-stack readiness")
    doctor.add_argument("bundle", type=Path, nargs="?")
    doctor.add_argument("--variant")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--backend", "--engine", dest="backend")
    doctor.add_argument("--device")
    doctor.add_argument("--model")
    doctor.add_argument("--json", action="store_true")
    return parser


def _configure_direct(args: argparse.Namespace) -> Path:
    if args.model is not None and args.unset_model:
        raise ConfigError("--model and --unset-model cannot be used together")
    directory = Path(args.directory).expanduser()
    if directory.is_symlink():
        raise ConfigError(f"verification config directory must not be a symlink: {directory}")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create verification config directory: {directory}") from exc
    if not directory.is_dir():
        raise ConfigError(f"verification config path is not a directory: {directory}")

    config_path = directory / CONFIG_FILENAME
    if config_path.is_symlink():
        raise ConfigError(f"verification config must not be a symlink: {config_path}")
    existing = load_config(config_path) if config_path.exists() else default_config()
    if (
        args.backend is not None
        and args.backend != existing.backend
        and existing.model is not None
        and args.model is None
        and not args.unset_model
    ):
        raise ConfigError(
            "changing the backend while a model is configured requires --model or --unset-model"
        )

    effective_backend = args.backend or existing.backend
    model: str | None | _UnsetType = UNSET
    if args.unset_model:
        model = None
    elif args.model is not None:
        candidate = Path(args.model).expanduser()
        if candidate.exists():
            if not candidate.is_file():
                raise ConfigError(f"whisper.cpp model path is not a regular file: {candidate}")
            model = str(candidate.resolve())
        elif effective_backend == "whispercpp" and not args.model.startswith("whispercpp:"):
            raise ConfigError(
                "whisper.cpp model must be an existing local file or a canonical "
                "SpeechScope model reference such as whispercpp:tiny.en-q5_1"
            )
        else:
            model = args.model

    update_config(
        config_path,
        backend=args.backend,
        device=args.device,
        model=model,
        normalization=args.normalization,
        use_language_hint=args.use_language_hint,
        fail_on_review=args.fail_on_review,
        fail_fast=args.fail_fast,
    )
    return config_path


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "configure":
            path = _configure_direct(args)
            print(path)
            return 0

        if args.command == "verify":
            unit_result = verify_renderplan(
                args.plan,
                render_dir=args.render_dir,
                output_dir=args.output_dir,
            )
            payload: dict[str, object] = {
                "id": unit_result.id,
                "status": unit_result.status,
                "render_plan_id": unit_result.render_plan_id,
                "source_plan_id": unit_result.source_plan_id,
                "render_profile_id": unit_result.render_profile_id,
                "manifest_id": unit_result.manifest_id,
                "segments": [_segment_payload(segment) for segment in unit_result.segments],
                "segment_count": len(unit_result.segments),
                "passed": unit_result.passed,
                "reviewed": unit_result.reviewed,
                "failed": unit_result.failed,
                "errors": unit_result.errors,
                "output": str(unit_result.output_dir),
                "report": str(unit_result.output_dir / "utterverify-report.toml"),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                _print_unit(payload, unit_result.segments)
            config = load_config(args.output_dir / CONFIG_FILENAME)
            if unit_result.status == "error":
                return 2
            if unit_result.status == "fail" or (
                unit_result.status == "review" and config.fail_on_review
            ):
                return 1
            return 0

        if args.command == "doctor":
            if args.bundle is not None and args.config is not None:
                raise BundleError("doctor BUNDLE and --config are mutually exclusive")
            if args.variant is not None and args.bundle is None:
                raise BundleError("--variant is valid only when doctor is given a bundle")
            if args.bundle is not None:
                doctor_result = doctor_bundle(
                    args.bundle,
                    variant=args.variant,
                    backend=args.backend,
                    device=args.device,
                    model=args.model if args.model is not None else UNSET,
                )
            else:
                doctor_result = run_doctor(
                    config_path=args.config,
                    backend=args.backend,
                    device=args.device,
                    model=args.model if args.model is not None else UNSET,
                )
            _print_doctor(doctor_result, json_output=args.json)
            if doctor_result.input_error:
                return 2
            return 0 if doctor_result.ok else 3

        if args.bundle_command == "init":
            config = VerifyConfig(
                backend=args.backend,
                device=args.device,
                model=args.model,
                normalization=args.normalization,
                use_language_hint=args.use_language_hint,
                fail_on_review=args.fail_on_review,
                fail_fast=args.fail_fast,
            )
            path = init_bundle_config(
                args.bundle,
                variant=args.variant,
                config=config,
                overwrite=args.force,
            )
            print(path)
            return 0

        if args.bundle_command == "configure":
            path = configure_bundle(
                args.bundle,
                variant=args.variant,
                backend=args.backend,
                device=args.device,
                model=args.model if args.model is not None else UNSET,
                unset_model=args.unset_model,
                normalization=args.normalization,
                use_language_hint=args.use_language_hint,
                fail_on_review=args.fail_on_review,
                fail_fast=args.fail_fast,
            )
            print(path)
            return 0

        result = verify_bundle(
            args.bundle,
            variant=args.variant,
            chapters=args.chapters,
            on_start=_print_bundle_start,
            on_progress=_print_bundle_progress,
        )
        unit_payloads: list[dict[str, object]] = []
        segment_payloads: list[dict[str, object]] = []
        for unit in result.units:
            unit_segments = [_segment_payload(segment) for segment in unit.segments]
            unit_payloads.append(
                {
                    "id": unit.id,
                    "status": unit.status,
                    "renderplan": str(unit.renderplan),
                    "render_plan_id": unit.render_plan_id,
                    "source_plan_id": unit.source_plan_id,
                    "render_profile_id": unit.render_profile_id,
                    "manifest_id": unit.manifest_id,
                    "segments": unit_segments,
                }
            )
            segment_payloads.extend({"unit_id": unit.id, **segment} for segment in unit_segments)
        payload = {
            "bundle": str(result.bundle),
            "kind": result.kind,
            "variant": result.variant,
            "status": result.status,
            "segments": segment_payloads,
            "segment_count": result.segments,
            "passed": result.passed,
            "reviewed": result.reviewed,
            "failed": result.failed,
            "errors": result.errors,
            "units": unit_payloads,
            "config": str(result.config_path),
            "config_created": result.config_created,
            "report": str(result.report_path),
            "exit_code": result.exit_code,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print("UtterVerify bundle summary", file=sys.stderr)
            print(f"status:    {result.status}", file=sys.stderr)
            print(f"segments:  {result.segments}", file=sys.stderr)
            print(f"pass:      {result.passed}", file=sys.stderr)
            print(f"review:    {result.reviewed}", file=sys.stderr)
            print(f"fail:      {result.failed}", file=sys.stderr)
            print(f"errors:    {result.errors}", file=sys.stderr)
            print(f"config:    {result.config_path}", file=sys.stderr)
            print(f"report:    {result.report_path}", file=sys.stderr)
            for unit in result.units:
                for segment in unit.segments:
                    _print_segment(segment, unit_id=unit.id, file=sys.stderr)
        return result.exit_code
    except UtterVerifyError as exc:
        print(f"utterverify: {exc}", file=sys.stderr)
        return 2


def _print_doctor(result: DoctorResult, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return
    print("UtterVerify doctor")
    print(f"backend: {result.backend}")
    print(f"device:  {result.device}")
    model = result.model if result.model else "<none>"
    source = f" ({result.model_source})" if result.model_source != "none" else ""
    print(f"model:   {model}{source}")
    print()
    for check in result.checks:
        print(f"{check.status.upper():4} {check.name.replace('_', '-'):24} {check.detail}")
        if check.hint:
            print(f"     hint: {check.hint}")
    print("\nready" if result.ok else "\nnot ready")


def _print_bundle_start(start) -> None:
    print("UtterVerify bundle verify", file=sys.stderr)
    print(f"  workspace: {start.workspace}", file=sys.stderr)
    print(f"  kind:      {start.kind}", file=sys.stderr)
    print(f"  variant:   {start.variant}", file=sys.stderr)
    print(f"  chapters:  {start.selector}", file=sys.stderr)
    print(f"  backend:   {start.backend}", file=sys.stderr)
    print(f"  model:     {start.model or '<none>'}", file=sys.stderr)
    print(file=sys.stderr)


def _print_bundle_progress(progress) -> None:
    print(
        f"[{progress.index}/{progress.total}] {progress.id}: {progress.status}",
        file=sys.stderr,
    )


def _segment_payload(segment) -> dict[str, object]:
    return {
        "number": segment.number,
        "segment_id": segment.segment_id,
        "reference_text": segment.reference_text,
        "wav": str(segment.wav),
        "wav_sha256": segment.wav_sha256,
        "report": str(segment.report) if segment.report is not None else None,
        "status": segment.status,
        "language": segment.language,
        "message": segment.message,
    }


def _print_segment(segment, *, unit_id: str | None = None, file=None) -> None:
    prefix = f"{unit_id} " if unit_id is not None else ""
    message = f" — {segment.message}" if segment.message else ""
    print(
        f"{segment.status.upper():12} {prefix}{segment.number:06d} "
        f"{segment.segment_id} {segment.wav_sha256}{message}",
        file=sys.stdout if file is None else file,
    )


def _print_unit(payload: Mapping[str, object], segments) -> None:
    print("UtterVerify RenderPlan validation")
    for name in (
        "status",
        "segment_count",
        "passed",
        "reviewed",
        "failed",
        "errors",
        "output",
        "report",
    ):
        print(f"{name + ':':10} {payload[name]}")
    for segment in segments:
        _print_segment(segment)


if __name__ == "__main__":
    raise SystemExit(main())
