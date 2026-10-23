"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .bundle import init_bundle_config, verify_bundle
from .config import CONFIG_FILENAME, VerifyConfig, load_config
from .core import verify_renderplan
from .errors import UtterVerifyError


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
    init.add_argument("--variant", required=True)
    init.add_argument("--backend", default="redux")
    init.add_argument("--device", default="cpu")
    init.add_argument("--model")
    init.add_argument("--normalization", default="basic-v1")
    init.add_argument("--use-language-hint", action="store_true")
    init.add_argument("--fail-on-review", action="store_true")
    init.add_argument("--fail-fast", action="store_true")
    init.add_argument("--force", action="store_true")

    verify = bundle_commands.add_parser("verify", help="Verify a rendered bundle variant")
    verify.add_argument("bundle", type=Path)
    verify.add_argument("--variant", required=True)
    verify.add_argument("--chapters", default="all")
    verify.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "verify":
            result = verify_renderplan(
                args.plan,
                render_dir=args.render_dir,
                output_dir=args.output_dir,
            )
            payload = {
                "id": result.id,
                "status": result.status,
                "segments": len(result.segments),
                "passed": result.passed,
                "reviewed": result.reviewed,
                "failed": result.failed,
                "errors": result.errors,
                "output": str(result.output_dir),
            }
            if args.json:
                print(json.dumps(payload, ensure_ascii=False))
            else:
                _print_unit(payload)
            config = load_config(args.output_dir / CONFIG_FILENAME)
            if result.status == "error":
                return 2
            if result.status == "fail" or (result.status == "review" and config.fail_on_review):
                return 1
            return 0

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

        result = verify_bundle(args.bundle, variant=args.variant, chapters=args.chapters)
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
            print("UtterVerify bundle validation")
            print(f"kind:      {result.kind}")
            print(f"variant:   {result.variant}")
            print(f"status:    {result.status}")
            print(f"segments:  {result.segments}")
            print(f"pass:      {result.passed}")
            print(f"review:    {result.reviewed}")
            print(f"fail:      {result.failed}")
            print(f"errors:    {result.errors}")
            print(f"config:    {result.config_path}")
            print(f"report:    {result.report_path}")
        return result.exit_code
    except UtterVerifyError as exc:
        print(f"utterverify: {exc}", file=sys.stderr)
        return 2


def _print_unit(payload: dict[str, object]) -> None:
    print("UtterVerify RenderPlan validation")
    for name in ("status", "segments", "passed", "reviewed", "failed", "errors", "output"):
        print(f"{name + ':':10} {payload[name]}")


if __name__ == "__main__":
    raise SystemExit(main())
