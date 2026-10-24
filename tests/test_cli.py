from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from utterverify import cli
from utterverify.config import UNSET
from utterverify.doctor import DoctorCheck, DoctorResult


def test_cli_parses_optional_variants_and_backend_engine_aliases() -> None:
    parser = cli._parser()
    init = parser.parse_args(["bundle", "init", "novel.ssmd-bundle", "--engine", "whispercpp"])
    verify = parser.parse_args(["bundle", "verify", "novel.ssmd-bundle"])
    configure = parser.parse_args(
        ["bundle", "configure", "novel.ssmd-bundle", "--backend", "whispercpp", "--no-fail-fast"]
    )
    doctor = parser.parse_args(["doctor", "--engine", "whispercpp", "--model", "local.bin"])

    assert init.variant is None and init.backend == "whispercpp"
    assert verify.variant is None
    assert configure.backend == "whispercpp" and configure.fail_fast is False
    assert doctor.bundle is None and doctor.backend == "whispercpp"


def test_cli_doctor_json_is_single_stdout_document(monkeypatch, capsys) -> None:
    result = DoctorResult(
        ok=True,
        backend="redux",
        device="cpu",
        model=None,
        model_source="none",
        checks=(DoctorCheck("backend", "pass", "redux 0.2"),),
    )
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: result)

    assert cli.main(["doctor", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["format"] == "utterverify.doctor"
    assert payload["checks"][0]["name"] == "backend"


def test_cli_doctor_exit_codes_and_bundle_option_rules(monkeypatch, capsys) -> None:
    failed = DoctorResult(
        ok=False,
        backend="whispercpp",
        device="cpu",
        model=None,
        model_source="none",
        checks=(DoctorCheck("model_selected", "fail", "missing"),),
    )
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: failed)
    assert cli.main(["doctor", "--engine", "whispercpp"]) == 3
    assert "not ready" in capsys.readouterr().out

    invalid = DoctorResult(
        ok=False,
        backend="redux",
        device="cpu",
        model=None,
        model_source="none",
        checks=(),
        input_error=True,
    )
    monkeypatch.setattr(cli, "run_doctor", lambda **_kwargs: invalid)
    assert cli.main(["doctor", "--json"]) == 2
    json.loads(capsys.readouterr().out)

    assert cli.main(["doctor", "novel.ssmd-bundle", "--config", "x.toml"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err
    assert cli.main(["doctor", "--variant", "narration"]) == 2
    assert "only when" in capsys.readouterr().err


def test_cli_configure_passes_alias_boolean_and_unset_options(monkeypatch, capsys) -> None:
    calls = {}

    def configure(bundle, **kwargs):
        calls["bundle"] = bundle
        calls.update(kwargs)
        return Path("config/utterverify.toml")

    monkeypatch.setattr(cli, "configure_bundle", configure)
    code = cli.main(
        [
            "bundle",
            "configure",
            "novel.ssmd-bundle",
            "--engine",
            "whispercpp",
            "--model",
            "whispercpp:tiny.en-q5_1",
            "--no-use-language-hint",
            "--fail-on-review",
        ]
    )
    assert code == 0
    assert calls["backend"] == "whispercpp"
    assert calls["model"] == "whispercpp:tiny.en-q5_1"
    assert calls["use_language_hint"] is False
    assert calls["fail_on_review"] is True
    assert calls["unset_model"] is False
    assert capsys.readouterr().out.strip() == "config/utterverify.toml"

    calls.clear()
    cli.main(["bundle", "configure", "novel.ssmd-bundle", "--unset-model"])
    assert calls["model"] is UNSET
    assert calls["unset_model"] is True


def test_bundle_verify_routes_progress_to_stderr_and_keeps_json_clean(monkeypatch, capsys) -> None:
    def fake_verify(_bundle, **kwargs):
        start = SimpleNamespace(
            workspace=Path("novel.ssmd-bundle"),
            kind="book",
            variant="narration--abc",
            selector="5-6",
            backend="whispercpp",
            model="whispercpp:tiny.en-q5_1",
        )
        progress = SimpleNamespace(index=1, total=2, id="chapter-0005", status="pass")
        kwargs["on_start"](start)
        kwargs["on_progress"](progress)
        return SimpleNamespace(
            bundle=Path("novel.ssmd-bundle"),
            kind="book",
            variant="narration--abc",
            status="pass",
            segments=1,
            passed=1,
            reviewed=0,
            failed=0,
            errors=0,
            config_path=Path("config.toml"),
            config_created=False,
            report_path=Path("report.toml"),
            exit_code=0,
        )

    monkeypatch.setattr(cli, "verify_bundle", fake_verify)
    assert cli.main(["bundle", "verify", "novel.ssmd-bundle", "--chapters", "5-6", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["variant"] == "narration--abc"
    assert "[1/2] chapter-0005: pass" in captured.err
    assert "UtterVerify bundle verify" in captured.err
