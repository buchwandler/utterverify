from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from utterverify import doctor
from utterverify.config import VerifyConfig, write_config
from utterverify.errors import BundleError

_REAL_IMPORT_MODULE = doctor.importlib.import_module


def _install_speechscope(
    monkeypatch,
    *,
    backend: str = "whispercpp",
    version: str | None = None,
    manager_type=None,
) -> SimpleNamespace:
    info = SimpleNamespace(
        name=backend,
        version=version,
        supported_devices=("cpu", "auto"),
    )
    module = SimpleNamespace(
        __version__="test-version",
        backend_info=lambda name: (
            info if name == backend else (_ for _ in ()).throw(ValueError(name))
        ),
        ModelManager=manager_type,
    )

    def import_module(name: str):
        if name == "speechscope":
            return module
        return _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr(doctor.importlib, "import_module", import_module)
    return module


def _ready_managed_model(path: Path):
    class Manager:
        def __init__(self, *, packaged_catalog: bool) -> None:
            assert packaged_catalog is True

        def show(self, ref: str, *, backend: str) -> dict[str, object]:
            assert backend == "whispercpp"
            return {"ref": ref, "acquisition": {"mode": "managed"}}

        def path(self, ref: str, *, backend: str) -> Path:
            assert backend == "whispercpp"
            return path

    return Manager


def _check_map(result: doctor.DoctorResult) -> dict[str, doctor.DoctorCheck]:
    return {check.name: check for check in result.checks}


def test_whispercpp_managed_model_and_executable_are_ready(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "model.bin"
    model_path.write_bytes(b"weights")
    fake = _install_speechscope(monkeypatch, manager_type=_ready_managed_model(model_path))
    monkeypatch.setattr(
        doctor, "_probe_whisper_executable", lambda: ("/usr/bin/whisper-cli", "1.2")
    )

    result = doctor.run_doctor(
        config=VerifyConfig(backend="whispercpp", model="whispercpp:tiny.en-q5_1")
    )
    checks = _check_map(result)
    assert result.ok is True
    assert result.model_source == "config"
    assert checks["whisper_executable"].status == "pass"
    assert checks["model_installed"].status == "pass"
    assert checks["model_integrity"].status == "pass"
    assert checks["model_readable"].status == "pass"
    assert not hasattr(fake, "Verifier")


def test_whispercpp_missing_model_fails_even_if_executable_exists(monkeypatch) -> None:
    _install_speechscope(monkeypatch)
    monkeypatch.setattr(doctor, "_probe_whisper_executable", lambda: ("whisper-cli", None))

    result = doctor.run_doctor(config=VerifyConfig(backend="whispercpp"))
    checks = _check_map(result)
    assert result.ok is False
    assert checks["whisper_executable"].status == "pass"
    assert checks["model_selected"].status == "fail"


def test_whispercpp_reports_executable_and_model_failures_together(monkeypatch) -> None:
    _install_speechscope(monkeypatch)

    def missing_executable():
        raise RuntimeError("whisper-cli is missing")

    monkeypatch.setattr(doctor, "_probe_whisper_executable", missing_executable)
    result = doctor.run_doctor(
        config=VerifyConfig(backend="whispercpp", model="/missing/model.bin")
    )
    checks = _check_map(result)
    assert result.ok is False
    assert checks["whisper_executable"].status == "fail"
    assert checks["model_readable"].status == "fail"


def test_whispercpp_managed_missing_model_has_install_hint(monkeypatch) -> None:
    class ModelNotInstalledError(Exception):
        pass

    class Manager:
        def __init__(self, *, packaged_catalog: bool) -> None:
            pass

        def show(self, ref: str, *, backend: str) -> dict[str, object]:
            return {"ref": ref, "acquisition": {"mode": "managed"}}

        def path(self, ref: str, *, backend: str) -> Path:
            raise ModelNotInstalledError(f"{ref} is not installed")

    _install_speechscope(monkeypatch, manager_type=Manager)
    monkeypatch.setattr(doctor, "_probe_whisper_executable", lambda: ("whisper-cli", None))
    result = doctor.run_doctor(
        config=VerifyConfig(backend="whispercpp", model="whispercpp:tiny.en-q5_1")
    )
    check = _check_map(result)["model_installed"]
    assert check.status == "fail"
    assert check.hint == "speechscope models install whispercpp:tiny.en-q5_1"


def test_installed_managed_model_does_not_hide_missing_executable(
    monkeypatch, tmp_path: Path
) -> None:
    model_path = tmp_path / "installed.bin"
    model_path.write_bytes(b"weights")
    _install_speechscope(monkeypatch, manager_type=_ready_managed_model(model_path))

    def missing_executable():
        raise RuntimeError("whisper-cli is unavailable")

    monkeypatch.setattr(doctor, "_probe_whisper_executable", missing_executable)
    result = doctor.run_doctor(
        config=VerifyConfig(backend="whispercpp", model="whispercpp:tiny.en-q5_1")
    )
    checks = _check_map(result)
    assert result.ok is False
    assert checks["whisper_executable"].status == "fail"
    assert checks["model_installed"].status == "pass"
    assert checks["model_integrity"].status == "pass"
    assert checks["model_readable"].status == "pass"


def test_whispercpp_integrity_failure_is_reported(monkeypatch) -> None:
    class ModelIntegrityError(Exception):
        pass

    class Manager:
        def __init__(self, *, packaged_catalog: bool) -> None:
            pass

        def show(self, ref: str, *, backend: str) -> dict[str, object]:
            return {"ref": ref, "acquisition": {"mode": "managed"}}

        def path(self, ref: str, *, backend: str) -> Path:
            raise ModelIntegrityError("bad digest")

    _install_speechscope(monkeypatch, manager_type=Manager)
    monkeypatch.setattr(doctor, "_probe_whisper_executable", lambda: ("whisper-cli", None))
    result = doctor.run_doctor(
        config=VerifyConfig(backend="whispercpp", model="whispercpp:tiny.en-q5_1")
    )
    checks = _check_map(result)
    assert checks["model_integrity"].status == "fail"
    assert checks["model_readable"].status == "fail"


def test_whispercpp_environment_model_is_identified_and_warns(monkeypatch, tmp_path: Path) -> None:
    model_path = tmp_path / "environment-model.bin"
    model_path.write_bytes(b"weights")
    _install_speechscope(monkeypatch)
    monkeypatch.setattr(doctor, "_probe_whisper_executable", lambda: ("whisper-cli", None))
    monkeypatch.setenv("SPEECHSCOPE_WHISPERCPP_MODEL", str(model_path))

    result = doctor.run_doctor(config=VerifyConfig(backend="whispercpp"))
    checks = _check_map(result)
    assert result.ok is True
    assert result.model_source == "environment"
    assert checks["model_pinned"].status == "warn"
    assert checks["model_readable"].status == "pass"


def test_whispercpp_local_model_path_is_checked_without_inference(
    monkeypatch, tmp_path: Path
) -> None:
    model_path = tmp_path / "local.bin"
    model_path.write_bytes(b"weights")
    fake = _install_speechscope(monkeypatch)
    monkeypatch.setattr(doctor, "_probe_whisper_executable", lambda: ("whisper-cli", None))
    result = doctor.run_doctor(config=VerifyConfig(backend="whispercpp", model=str(model_path)))
    assert result.ok is True
    assert _check_map(result)["model_readable"].detail == str(model_path.resolve())
    assert not hasattr(fake, "Verifier")


def test_whispercpp_executable_probe_respects_speechscope_environment(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "whisper-cli"
    executable.write_text("#!/bin/sh\necho 'whisper.cpp version: test-1'\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("SPEECHSCOPE_WHISPERCPP_BIN", str(executable))

    path, version = doctor._probe_whisper_executable()
    assert path == str(executable)
    assert version == "test-1"


def test_generic_backend_device_and_plugin_readiness(monkeypatch) -> None:
    _install_speechscope(monkeypatch, backend="plugin-one", version="0.3")
    result = doctor.run_doctor(config=VerifyConfig(backend="plugin-one", device="gpu"))
    checks = _check_map(result)
    assert result.ok is False
    assert checks["backend"].status == "pass"
    assert checks["device"].status == "fail"
    assert checks["backend_readiness"].status == "warn"


def test_redux_metadata_passes_with_deep_readiness_warning(monkeypatch) -> None:
    _install_speechscope(monkeypatch, backend="redux", version="0.2.0")
    result = doctor.run_doctor(config=VerifyConfig(backend="redux", model="configured-model"))
    checks = _check_map(result)
    assert result.ok is True
    assert result.model == "configured-model"
    assert checks["redux_dependencies"].status == "pass"
    assert checks["redux_runtime_readiness"].status == "warn"


def test_redux_missing_dependency_has_install_hint(monkeypatch) -> None:
    _install_speechscope(monkeypatch, backend="redux", version=None)
    result = doctor.run_doctor(config=VerifyConfig(backend="redux"))
    check = _check_map(result)["redux_dependencies"]
    assert result.ok is False
    assert check.hint == "pip install 'utterverify[redux]'"


def test_missing_speechscope_and_invalid_backend_are_structured(monkeypatch) -> None:
    real_import = _REAL_IMPORT_MODULE

    def no_speechscope(name: str):
        if name == "speechscope":
            raise ImportError("deliberately unavailable")
        return real_import(name)

    monkeypatch.setattr(doctor.importlib, "import_module", no_speechscope)
    missing = doctor.run_doctor(config=VerifyConfig(backend="redux"))
    assert _check_map(missing)["speechscope"].status == "fail"
    assert _check_map(missing)["backend"].status == "fail"

    _install_speechscope(monkeypatch, backend="redux")
    unknown = doctor.run_doctor(config=VerifyConfig(backend="unregistered"))
    assert _check_map(unknown)["backend"].status == "fail"


def test_doctor_json_is_stable_and_does_not_modify_config(tmp_path: Path, monkeypatch) -> None:
    _install_speechscope(monkeypatch, backend="redux", version="0.2.0")
    path = tmp_path / "utterverify.toml"
    write_config(path, VerifyConfig(backend="redux"))
    before = path.read_bytes()
    result = doctor.run_doctor(config_path=path)

    payload = result.to_dict()
    assert list(payload) == [
        "format",
        "schema_version",
        "ok",
        "backend",
        "device",
        "model",
        "model_source",
        "checks",
    ]
    assert payload["format"] == "utterverify.doctor"
    assert json.dumps(payload, sort_keys=True) == json.dumps(result.to_dict(), sort_keys=True)
    assert path.read_bytes() == before


def test_invalid_config_is_input_error_and_never_created(tmp_path: Path, monkeypatch) -> None:
    _install_speechscope(monkeypatch, backend="redux", version="0.2.0")
    path = tmp_path / "absent.toml"
    result = doctor.run_doctor(config_path=path)
    assert result.input_error is True
    assert result.ok is False
    assert path.exists() is False


def _document_bundle(root: Path, variant: str = "narration--abc") -> Path:
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format": "ssmdconvert.document",
                "schema_version": 1,
                "document": {"id": "document-0001", "path": "document.ssmd.md"},
            }
        ),
        encoding="utf-8",
    )
    plan_dir = root / "renderplan" / variant
    render_dir = root / "voicerender" / variant / "document-0001"
    plan_dir.mkdir(parents=True)
    render_dir.mkdir(parents=True)
    (plan_dir / "document.renderplan.toml").write_text("format = 'renderplan'\n", encoding="utf-8")
    (render_dir / "render-manifest.toml").write_text("format = 'manifest'\n", encoding="utf-8")
    return root


def _stub_bundle_handoff(monkeypatch) -> None:
    import utterverify.core

    monkeypatch.setattr(
        utterverify.core,
        "_load_handoff",
        lambda *_args: (SimpleNamespace(speech=()), SimpleNamespace(speech=())),
    )


def test_bundle_doctor_uses_defaults_without_creating_config(monkeypatch, tmp_path: Path) -> None:
    _install_speechscope(monkeypatch, backend="redux", version="0.2.0")
    _stub_bundle_handoff(monkeypatch)
    root = _document_bundle(tmp_path / "novel.ssmd-bundle")

    result = doctor.doctor_bundle(root)
    checks = _check_map(result)
    config_path = root / "utterverify" / "narration--abc" / "utterverify.toml"
    assert result.ok is True
    assert checks["bundle_config"].status == "warn"
    assert checks["bundle_inputs"].status == "pass"
    assert config_path.exists() is False


def test_bundle_doctor_uses_existing_config_and_resolves_variant(
    monkeypatch, tmp_path: Path
) -> None:
    _install_speechscope(monkeypatch, backend="redux", version="0.2.0")
    _stub_bundle_handoff(monkeypatch)
    root = _document_bundle(tmp_path / "novel.ssmd-bundle")
    config_path = root / "utterverify" / "narration--abc" / "utterverify.toml"
    write_config(config_path, VerifyConfig(backend="redux", model="catalog-model"))

    result = doctor.doctor_bundle(root)
    assert result.ok is True
    assert result.backend == "redux"
    assert result.model == "catalog-model"
    assert _check_map(result)["bundle_config"].status == "pass"


def test_bundle_doctor_rejects_ambiguous_or_missing_inputs(monkeypatch, tmp_path: Path) -> None:
    _install_speechscope(monkeypatch, backend="redux", version="0.2.0")
    _stub_bundle_handoff(monkeypatch)
    root = _document_bundle(tmp_path / "novel.ssmd-bundle")
    for component in ("renderplan", "voicerender"):
        (root / component / "alternate").mkdir()
    with pytest.raises(BundleError, match="multiple rendered variants"):
        doctor.doctor_bundle(root)

    with pytest.raises(BundleError, match="required file is missing"):
        doctor.doctor_bundle(root, variant="alternate")
