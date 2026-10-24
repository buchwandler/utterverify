"""Side-effect-free readiness checks for the selected validation stack."""

from __future__ import annotations

import importlib
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from .config import CONFIG_FILENAME, UNSET, VerifyConfig, _UnsetType, default_config, load_config
from .errors import BundleError, ConfigError, UtterVerifyError

CheckStatus = Literal["pass", "warn", "fail"]
ModelSource = Literal["cli", "config", "environment", "none"]


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorResult:
    ok: bool
    backend: str
    device: str
    model: str | None
    model_source: ModelSource
    checks: tuple[DoctorCheck, ...]
    input_error: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return the stable doctor JSON envelope."""
        return {
            "format": "utterverify.doctor",
            "schema_version": 1,
            "ok": self.ok,
            "backend": self.backend,
            "device": self.device,
            "model": self.model,
            "model_source": self.model_source,
            "checks": [asdict(check) for check in self.checks],
        }


def run_doctor(
    *,
    config_path: str | Path | None = None,
    config: VerifyConfig | None = None,
    backend: str | None = None,
    device: str | None = None,
    model: str | _UnsetType = UNSET,
) -> DoctorResult:
    """Check importability and local readiness without creating files or running ASR."""
    checks: list[DoctorCheck] = []
    input_error = False
    selected_config = config
    if selected_config is None and config_path is not None:
        try:
            selected_config = load_config(config_path)
            checks.append(DoctorCheck("config", "pass", f"loaded {Path(config_path)}"))
        except ConfigError as exc:
            selected_config = default_config()
            input_error = True
            checks.append(DoctorCheck("config", "fail", str(exc)))
    elif selected_config is not None:
        detail = (
            f"loaded {Path(config_path)}" if config_path is not None else "configuration is valid"
        )
        checks.append(DoctorCheck("config", "pass", detail))
    else:
        selected_config = default_config()
        checks.append(DoctorCheck("config", "pass", "using built-in defaults"))

    assert selected_config is not None
    effective_backend = backend if backend is not None else selected_config.backend
    effective_device = device if device is not None else selected_config.device
    if isinstance(model, str):
        effective_model = model
        model_source: ModelSource = "cli"
    elif selected_config.model is not None:
        effective_model = selected_config.model
        model_source = "config"
    elif effective_backend == "whispercpp" and os.environ.get("SPEECHSCOPE_WHISPERCPP_MODEL"):
        effective_model = os.environ["SPEECHSCOPE_WHISPERCPP_MODEL"]
        model_source = "environment"
    else:
        effective_model = None
        model_source = "none"

    _check_import("renderplan", checks, label="renderplan")
    speechscope = _import_speechscope(checks)
    backend_info = None
    if speechscope is None:
        checks.append(
            DoctorCheck(
                "backend",
                "fail",
                "SpeechScope is unavailable; backend metadata cannot be checked",
                "Install the utterverify package dependencies.",
            )
        )
        checks.append(DoctorCheck("device", "fail", "device support cannot be checked"))
    else:
        try:
            backend_info = speechscope.backend_info(effective_backend)
        except Exception as exc:  # noqa: BLE001
            checks.append(
                DoctorCheck(
                    "backend",
                    "fail",
                    f"unknown or unavailable SpeechScope backend {effective_backend!r}: {exc}",
                )
            )
            checks.append(DoctorCheck("device", "fail", "device support cannot be checked"))
        else:
            checks.append(
                DoctorCheck(
                    "backend",
                    "pass",
                    f"{backend_info.name}"
                    + (f" {backend_info.version}" if backend_info.version else ""),
                )
            )
            supported = tuple(backend_info.supported_devices)
            if effective_device in supported:
                checks.append(DoctorCheck("device", "pass", effective_device))
            else:
                checks.append(
                    DoctorCheck(
                        "device",
                        "fail",
                        f"{effective_backend} does not support device {effective_device!r}; "
                        f"supported: {', '.join(supported)}",
                    )
                )

    if speechscope is not None and backend_info is not None:
        if backend_info.name == "whispercpp":
            _check_whispercpp(speechscope, effective_model, model_source, checks)
        elif backend_info.name == "redux":
            _check_redux(backend_info, checks)
        else:
            checks.append(
                DoctorCheck(
                    "backend_readiness",
                    "warn",
                    f"deep readiness checks are specific to SpeechScope backend {backend_info.name!r}",
                )
            )

    ok = not input_error and all(check.status != "fail" for check in checks)
    return DoctorResult(
        ok=ok,
        backend=effective_backend,
        device=effective_device,
        model=effective_model,
        model_source=model_source,
        checks=tuple(checks),
        input_error=input_error,
    )


def doctor_bundle(
    bundle: str | Path,
    *,
    variant: str | None = None,
    backend: str | None = None,
    device: str | None = None,
    model: str | _UnsetType = UNSET,
) -> DoctorResult:
    """Run doctor with a validated bundle's config and selected input handoffs."""
    from .bundle import _contained_file, _load_bundle, _resolve_units, _resolve_variant
    from .core import _load_handoff, _preflight_loaded

    identity = _load_bundle(bundle)
    selected_variant = _resolve_variant(identity, variant)
    config_path = identity.root / "utterverify" / selected_variant / CONFIG_FILENAME
    bundle_checks: list[DoctorCheck] = [
        DoctorCheck("bundle_manifest", "pass", "canonical bundle manifest is valid"),
        DoctorCheck("bundle_variant", "pass", selected_variant),
    ]

    if config_path.exists() or config_path.is_symlink():
        if config_path.is_symlink():
            raise BundleError(f"bundle UtterVerify config must not be a symlink: {config_path}")
        config_file = _contained_file(config_path, identity.root)
        selected_config = load_config(config_file)
        bundle_checks.append(DoctorCheck("bundle_config", "pass", str(config_file)))
        result = run_doctor(
            config_path=config_file,
            config=selected_config,
            backend=backend,
            device=device,
            model=model,
        )
    else:
        selected_config = default_config()
        bundle_checks.append(
            DoctorCheck(
                "bundle_config",
                "warn",
                "config is missing; doctor uses defaults and bundle verify would create it",
            )
        )
        result = run_doctor(
            config=selected_config,
            backend=backend,
            device=device,
            model=model,
        )

    try:
        units = _resolve_units(identity, selected_variant, "all")
        for unit in units:
            handoff_plan, handoff_manifest = _load_handoff(unit.renderplan, unit.render_dir)
            _preflight_loaded(handoff_plan, handoff_manifest, unit.render_dir)
    except UtterVerifyError:
        raise
    except Exception as exc:
        raise BundleError(f"cannot preflight selected bundle inputs: {exc}") from exc
    bundle_checks.append(
        DoctorCheck("bundle_inputs", "pass", f"{len(units)} selected handoff(s) are complete")
    )
    checks = tuple(bundle_checks) + result.checks
    return replace(
        result,
        ok=result.ok and all(check.status != "fail" for check in bundle_checks),
        checks=checks,
    )


def _check_import(module_name: str, checks: list[DoctorCheck], *, label: str) -> None:
    try:
        importlib.import_module(module_name)
    except ImportError as exc:
        checks.append(
            DoctorCheck(
                label,
                "fail",
                f"{module_name} is not importable: {exc}",
                f"Install {module_name} in the UtterVerify environment.",
            )
        )
    else:
        checks.append(DoctorCheck(label, "pass", f"{module_name} is importable"))


def _import_speechscope(checks: list[DoctorCheck]):
    try:
        module = importlib.import_module("speechscope")
    except ImportError as exc:
        checks.append(
            DoctorCheck(
                "speechscope",
                "fail",
                f"speechscope is not importable: {exc}",
                "Install UtterVerify and its SpeechScope dependency.",
            )
        )
        return None
    version = getattr(module, "__version__", None)
    if not isinstance(version, str):
        try:
            from importlib.metadata import version as package_version

            version = package_version("speechscope")
        except Exception:  # noqa: BLE001
            version = None
    detail = f"speechscope {version}" if version else "speechscope is importable"
    checks.append(DoctorCheck("speechscope", "pass", detail))
    return module


def _check_redux(backend_info: object, checks: list[DoctorCheck]) -> None:
    version = getattr(backend_info, "version", None)
    if version is None:
        checks.append(
            DoctorCheck(
                "redux_dependencies",
                "fail",
                "SpeechScope reports that the Redux dependency is unavailable",
                "pip install 'utterverify[redux]'",
            )
        )
        return
    checks.append(DoctorCheck("redux_dependencies", "pass", f"dependency metadata: {version}"))
    checks.append(
        DoctorCheck(
            "redux_runtime_readiness",
            "warn",
            "metadata preflight passed; full model/device readiness is not proven without runtime initialization",
        )
    )


def _check_whispercpp(
    speechscope: object,
    model: str | None,
    model_source: ModelSource,
    checks: list[DoctorCheck],
) -> None:
    try:
        executable, version = _probe_whisper_executable()
    except Exception as exc:  # noqa: BLE001
        checks.append(
            DoctorCheck(
                "whisper_executable",
                "fail",
                str(exc),
                "Install whisper.cpp or set SPEECHSCOPE_WHISPERCPP_BIN.",
            )
        )
    else:
        detail = executable + (f" (whisper.cpp {version})" if version else "")
        checks.append(DoctorCheck("whisper_executable", "pass", detail))

    if model is None or not model.strip():
        checks.append(
            DoctorCheck(
                "model_selected",
                "fail",
                "No whisper.cpp model is selected.",
                "Configure a model with `utterverify bundle configure <bundle> --engine whispercpp --model whispercpp:<id>` or set SPEECHSCOPE_WHISPERCPP_MODEL.",
            )
        )
        return
    checks.append(DoctorCheck("model_selected", "pass", model))
    if model_source == "environment":
        checks.append(
            DoctorCheck(
                "model_pinned",
                "warn",
                "using SPEECHSCOPE_WHISPERCPP_MODEL; bundle config does not pin a target model",
            )
        )

    if model.startswith("whispercpp:"):
        _check_managed_model(speechscope, model, checks)
    else:
        _check_local_model(model, checks)


def _probe_whisper_executable() -> tuple[str, str | None]:
    # Adapter-internal SpeechScope helper; isolated here in case a public API appears.
    from speechscope.backends.whispercpp import probe_executable

    return probe_executable()


def _check_managed_model(speechscope: object, ref: str, checks: list[DoctorCheck]) -> None:
    manager_type = getattr(speechscope, "ModelManager", None)
    if manager_type is None:
        checks.append(
            DoctorCheck("model_catalogued", "fail", "SpeechScope ModelManager is unavailable")
        )
        checks.extend(
            DoctorCheck(name, "warn", "not checked")
            for name in ("model_managed", "model_installed", "model_integrity", "model_readable")
        )
        return
    try:
        manager = manager_type(packaged_catalog=True)
        metadata = manager.show(ref, backend="whispercpp")
    except Exception as exc:  # noqa: BLE001
        checks.append(DoctorCheck("model_catalogued", "fail", str(exc)))
        checks.extend(
            DoctorCheck(name, "warn", "not checked because model is not catalogued")
            for name in ("model_managed", "model_installed", "model_integrity", "model_readable")
        )
        return
    checks.append(DoctorCheck("model_catalogued", "pass", str(metadata.get("ref", ref))))

    acquisition = metadata.get("acquisition")
    managed = isinstance(acquisition, dict) and acquisition.get("mode") == "managed"
    if not managed:
        checks.append(DoctorCheck("model_managed", "fail", f"{ref} is not SpeechScope-managed"))
        checks.extend(
            DoctorCheck(name, "warn", "not checked for an unmanaged catalog model")
            for name in ("model_installed", "model_integrity", "model_readable")
        )
        return
    checks.append(DoctorCheck("model_managed", "pass", "managed by SpeechScope"))

    try:
        path = Path(manager.path(ref, backend="whispercpp"))
    except Exception as exc:  # noqa: BLE001
        error_name = type(exc).__name__
        if error_name == "ModelNotInstalledError":
            checks.append(
                DoctorCheck(
                    "model_installed", "fail", str(exc), f"speechscope models install {ref}"
                )
            )
            checks.extend(
                DoctorCheck(name, "warn", "not checked because model is not installed")
                for name in ("model_integrity", "model_readable")
            )
        elif error_name == "ModelIntegrityError":
            checks.append(DoctorCheck("model_installed", "pass", "managed install found"))
            checks.append(DoctorCheck("model_integrity", "fail", str(exc)))
            checks.append(DoctorCheck("model_readable", "fail", "integrity verification failed"))
        else:
            checks.append(
                DoctorCheck(
                    "model_installed", "fail", str(exc), f"speechscope models install {ref}"
                )
            )
            checks.extend(
                DoctorCheck(name, "warn", "not checked because managed model resolution failed")
                for name in ("model_integrity", "model_readable")
            )
        return

    checks.append(DoctorCheck("model_installed", "pass", str(path)))
    checks.append(
        DoctorCheck("model_integrity", "pass", "verified by SpeechScope ModelManager.path()")
    )
    if path.is_file() and os.access(path, os.R_OK):
        checks.append(DoctorCheck("model_readable", "pass", str(path)))
    else:
        checks.append(
            DoctorCheck("model_readable", "fail", f"model is not a readable regular file: {path}")
        )


def _check_local_model(value: str, checks: list[DoctorCheck]) -> None:
    path = Path(value).expanduser()
    try:
        if not path.is_file():
            raise OSError("path is not an existing regular file")
        with path.open("rb") as model_file:
            model_file.read(1)
    except OSError as exc:
        checks.append(
            DoctorCheck(
                "model_readable",
                "fail",
                f"whisper.cpp model is not a readable local file: {path} ({exc})",
            )
        )
    else:
        checks.append(DoctorCheck("model_readable", "pass", str(path.resolve())))
