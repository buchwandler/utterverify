from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from utterverify import cli
from utterverify.config import UNSET, VerifyConfig, load_config
from utterverify.doctor import DoctorCheck, DoctorResult
from utterverify.types import SegmentOutcome


def test_cli_parses_optional_variants_and_backend_engine_aliases() -> None:
    parser = cli._parser()
    init = parser.parse_args(["bundle", "init", "novel.ssmd-bundle", "--engine", "whispercpp"])
    verify = parser.parse_args(["bundle", "verify", "novel.ssmd-bundle"])
    configure = parser.parse_args(
        ["bundle", "configure", "novel.ssmd-bundle", "--backend", "whispercpp", "--no-fail-fast"]
    )
    direct_config = parser.parse_args(
        [
            "configure",
            "verify/chapter",
            "--engine",
            "whispercpp",
            "--model",
            "whispercpp:tiny.en-q5_1",
        ]
    )
    doctor = parser.parse_args(["doctor", "--engine", "whispercpp", "--model", "local.bin"])

    assert init.variant is None and init.backend == "whispercpp"
    assert verify.variant is None
    assert configure.backend == "whispercpp" and configure.fail_fast is False
    assert doctor.bundle is None and doctor.backend == "whispercpp"
    assert direct_config.directory == Path("verify/chapter")
    assert direct_config.backend == "whispercpp"


def test_direct_config_persists_backend_and_model(tmp_path: Path, capsys) -> None:
    directory = tmp_path / "verify" / "chapter"
    assert (
        cli.main(
            [
                "configure",
                str(directory),
                "--engine",
                "whispercpp",
                "--model",
                "whispercpp:tiny.en-q5_1",
                "--no-use-language-hint",
                "--fail-on-review",
            ]
        )
        == 0
    )
    config_path = directory / "utterverify.toml"
    assert capsys.readouterr().out.strip() == str(config_path)
    config = load_config(config_path)
    assert config.backend == "whispercpp"
    assert config.model == "whispercpp:tiny.en-q5_1"
    assert config.use_language_hint is False
    assert config.fail_on_review is True


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


def test_direct_doctor_accepts_config_from_configure(monkeypatch, capsys, tmp_path: Path) -> None:
    config_path = tmp_path / "utterverify.toml"
    received = {}
    result = DoctorResult(
        ok=True,
        backend="whispercpp",
        device="cpu",
        model="whispercpp:tiny.en-q5_1",
        model_source="config",
        checks=(DoctorCheck("model_installed", "pass", "available"),),
    )

    def run_doctor(**kwargs):
        received.update(kwargs)
        return result

    monkeypatch.setattr(cli, "run_doctor", run_doctor)
    assert cli.main(["doctor", "--config", str(config_path), "--json"]) == 0
    assert received["config_path"] == config_path
    assert json.loads(capsys.readouterr().out)["model"] == "whispercpp:tiny.en-q5_1"


def test_direct_doctor_forwards_existing_local_model_without_installing(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    model_path = tmp_path / "ggml-tiny.en-q5_1.bin"
    received = {}
    result = DoctorResult(
        ok=True,
        backend="whispercpp",
        device="cpu",
        model=str(model_path),
        model_source="cli",
        checks=(DoctorCheck("model_readable", "pass", str(model_path)),),
    )

    def run_doctor(**kwargs):
        received.update(kwargs)
        return result

    monkeypatch.setattr(cli, "run_doctor", run_doctor)
    assert cli.main(["doctor", "--engine", "whispercpp", "--model", str(model_path), "--json"]) == 0
    assert received["backend"] == "whispercpp"
    assert received["model"] == str(model_path)
    assert json.loads(capsys.readouterr().out)["model_source"] == "cli"


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


def test_cli_help_lists_workflows_and_version_is_available(capsys) -> None:
    help_text = cli._parser().format_help()
    assert "configure" in help_text
    assert "verify" in help_text
    assert "doctor" in help_text

    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.startswith("utterverify ")


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
        segment = SegmentOutcome(
            number=5,
            segment_id="sha256:" + "1" * 64,
            reference_text="Chapter five.",
            wav=Path("chapter-0005/000005.wav"),
            wav_sha256="sha256:" + "2" * 64,
            report=Path("chapter-0005/000005.speechscope.json"),
            status="pass",
        )
        unit = SimpleNamespace(
            id="chapter-0005",
            status="pass",
            renderplan=Path("chapter-0005.renderplan.toml"),
            render_plan_id="sha256:" + "3" * 64,
            source_plan_id="sha256:" + "4" * 64,
            render_profile_id="sha256:" + "5" * 64,
            manifest_id="sha256:" + "6" * 64,
            segments=(segment,),
        )
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
            units=(unit,),
            exit_code=0,
        )

    monkeypatch.setattr(cli, "verify_bundle", fake_verify)
    assert cli.main(["bundle", "verify", "novel.ssmd-bundle", "--chapters", "5-6", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert payload["variant"] == "narration--abc"
    assert payload["segment_count"] == 1
    assert payload["segments"][0]["unit_id"] == "chapter-0005"
    assert payload["segments"][0]["reference_text"] == "Chapter five."
    assert "[1/2] chapter-0005: pass" in captured.err
    assert "UtterVerify bundle verify" in captured.err


def test_direct_verify_json_and_human_output_include_segment_details(
    monkeypatch, capsys, tmp_path: Path
) -> None:

    statuses = ("fail", "review", "error", "not_evaluated")
    outcomes = tuple(
        SegmentOutcome(
            number=17 + index,
            segment_id="sha256:" + str(index + 1) * 64,
            reference_text="Expected narration.",
            wav=tmp_path / f"{17 + index:06d}.wav",
            wav_sha256="sha256:" + str(index + 2) * 64,
            report=None,
            status=status,
            message=f"{status} detail",
        )
        for index, status in enumerate(statuses)
    )
    unit = SimpleNamespace(
        id="chapter-0001",
        status="error",
        render_plan_id="sha256:" + "3" * 64,
        source_plan_id="sha256:" + "4" * 64,
        render_profile_id="sha256:" + "5" * 64,
        manifest_id="sha256:" + "6" * 64,
        segments=outcomes,
        passed=0,
        reviewed=1,
        failed=1,
        errors=1,
        output_dir=tmp_path,
    )
    monkeypatch.setattr(cli, "verify_renderplan", lambda *_args, **_kwargs: unit)
    monkeypatch.setattr(cli, "load_config", lambda _path: VerifyConfig(backend="redux"))
    args = [
        "verify",
        "chapter.renderplan.toml",
        "--render-dir",
        "renders/chapter",
        "--output-dir",
        str(tmp_path),
    ]

    assert cli.main([*args, "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["segment_count"] == 4
    assert [segment["status"] for segment in payload["segments"]] == list(statuses)
    assert payload["segments"][0]["reference_text"] == "Expected narration."
    assert payload["segments"][0]["wav_sha256"] == "sha256:" + "2" * 64
    assert payload["segments"][0]["status"] == "fail"

    assert cli.main(args) == 2
    human = capsys.readouterr().out
    assert "FAIL" in human
    assert "000017" in human
    assert "fail detail" in human
    assert all(status.upper() in human for status in statuses)


def test_direct_verify_exit_codes_include_review_policy(
    monkeypatch, capsys, tmp_path: Path
) -> None:
    unit = SimpleNamespace(
        id="direct",
        status="pass",
        render_plan_id="sha256:" + "1" * 64,
        source_plan_id="sha256:" + "2" * 64,
        render_profile_id="sha256:" + "3" * 64,
        manifest_id="sha256:" + "4" * 64,
        segments=(),
        passed=0,
        reviewed=0,
        failed=0,
        errors=0,
        output_dir=tmp_path,
    )
    policy = {"fail_on_review": False}
    monkeypatch.setattr(cli, "verify_renderplan", lambda *_args, **_kwargs: unit)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path: VerifyConfig(backend="redux", fail_on_review=policy["fail_on_review"]),
    )
    args = [
        "verify",
        "chapter.renderplan.toml",
        "--render-dir",
        "renders/chapter",
        "--output-dir",
        str(tmp_path),
        "--json",
    ]

    for status, fail_on_review, expected in (
        ("pass", False, 0),
        ("fail", False, 1),
        ("error", False, 2),
        ("review", False, 0),
        ("review", True, 1),
    ):
        unit.status = status
        policy["fail_on_review"] = fail_on_review
        assert cli.main(args) == expected
        assert json.loads(capsys.readouterr().out)["status"] == status
