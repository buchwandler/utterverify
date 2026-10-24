"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .bundle import configure_bundle, init_bundle_config, verify_bundle
from .config import CONFIG_FILENAME, UNSET, VerifyConfig, load_config
from .core import verify_renderplan
from .doctor import DoctorResult, doctor_bundle, run_doctor
from .errors import BundleError, UtterVerifyError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="utterverify",
        description="Validate VoiceRender speech WAVs against RenderPlan text with SpeechScope.",
    )
    parser.add_argument("--version", action="version", version=f"utterverify {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            unit_result = verify_renderplan(
                args.plan,
                render_dir=args.render_dir,
                output_dir=args.output_dir,
            )
            payload = {
                "id": unit_result.id,
                "status": unit_result.status,
                "segments": len(unit_result.segments),
                "passed": unit_result.passed,
                "reviewed": unit_result.reviewed,
                "failed": unit_result.failed,
                "errors": unit_result.errors,
                "output": str(unit_result.output_dir),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                _print_unit(payload)
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
        payload = {
            "bundle": str(result.bundle),
            "kind": result.kind,
            "variant": result.variant,
            "status": result.status,
            "segments": result.segments,
            "passed": result.passed,
            "reviewed": result.reviewed,
            "failed": result.failed,
            "errors": result.errors,
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


def _print_unit(payload: dict[str, object]) -> None:
    print("UtterVerify RenderPlan validation")
    for name in ("status", "segments", "passed", "reviewed", "failed", "errors", "output"):
        print(f"{name + ':':10} {payload[name]}")


if __name__ == "__main__":
    raise SystemExit(main())
