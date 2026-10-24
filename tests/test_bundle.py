from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

from utterverify.bundle import (
    configure_bundle,
    discover_bundle_variants,
    resolve_bundle_variant,
    verify_bundle,
)


class _Speech:
    number = 1
    segment_id = "sha256:" + "1" * 64
    text = "Hello there"
    language = "en-US"
    wav = "000001.wav"


class _Plan:
    speech = (_Speech(),)
    plan_id = "sha256:" + "2" * 64


class _Manifest:
    complete = True

    def __init__(self, wav_hash: str) -> None:
        self.speech = (
            SimpleNamespace(
                segment_number=1,
                segment_id=_Speech.segment_id,
                wav="000001.wav",
                sha256="sha256:" + wav_hash,
            ),
        )

    def validate_against(self, plan) -> None:
        assert plan is _PLAN


_PLAN = _Plan()


def _install_fake_renderplan(monkeypatch, wav_hash: str) -> None:
    module = ModuleType("renderplan")
    module.load_plan = lambda _path: _PLAN
    module.load_manifest = lambda _path: _Manifest(wav_hash)
    monkeypatch.setitem(sys.modules, "renderplan", module)


def _install_fake_speechscope(monkeypatch, config_path: Path) -> None:
    module = ModuleType("speechscope")

    class ThresholdPolicy:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class VerificationRequest:
        def __init__(self, audio, **kwargs):
            self.audio = audio
            self.kwargs = kwargs

    class Report:
        def __init__(self):
            self.evaluation = {"status": "pass", "rules": []}

        def write_json(self, path):
            Path(path).write_text(json.dumps({"evaluation": self.evaluation}), encoding="utf-8")

    class Verifier:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            assert config_path.is_file(), "config must exist before backend initialization"
            return self

        def __exit__(self, *_args):
            return False

        def analyze(self, _request):
            return Report()

    module.ThresholdPolicy = ThresholdPolicy
    module.VerificationRequest = VerificationRequest
    module.Verifier = Verifier
    monkeypatch.setitem(sys.modules, "speechscope", module)


def _document_bundle(tmp_path: Path) -> tuple[Path, str, Path]:
    root = tmp_path / "story.ssmd-bundle"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format": "ssmdconvert.document",
                "schema_version": 1,
                "ssmd_version": "0.9",
                "document": {"id": "document-0001", "path": "document.ssmd.md"},
            }
        ),
        encoding="utf-8",
    )
    variant = "narration--abcdef123456"
    plan_dir = root / "renderplan" / variant
    render_dir = root / "voicerender" / variant / "document-0001"
    plan_dir.mkdir(parents=True)
    render_dir.mkdir(parents=True)
    (plan_dir / "document.renderplan.toml").write_text("format = 'renderplan'\n", encoding="utf-8")
    (render_dir / "render-manifest.toml").write_text(
        "format = 'render-manifest'\n", encoding="utf-8"
    )
    wav = render_dir / "000001.wav"
    wav.write_bytes(b"fake-wave-for-contract-test")
    return root, variant, wav


def test_bundle_creates_config_before_speechscope_backend(monkeypatch, tmp_path: Path) -> None:
    root, variant, wav = _document_bundle(tmp_path)
    digest = hashlib.sha256(wav.read_bytes()).hexdigest()
    _install_fake_renderplan(monkeypatch, digest)
    config_path = root / "utterverify" / variant / "utterverify.toml"
    _install_fake_speechscope(monkeypatch, config_path)

    starts = []
    progress = []
    result = verify_bundle(root, on_start=starts.append, on_progress=progress.append)
    assert starts[0].selected_unit_ids == ("document-0001",)
    assert progress[0].id == "document-0001" and progress[0].status == "pass"

    assert result.config_created is True
    assert result.status == "pass"
    assert result.exit_code == 0
    assert result.report_path.is_file()
    report = tomllib.loads(result.report_path.read_text(encoding="utf-8"))
    speechscope = report["speechscope"]
    assert speechscope["backend"] == "redux"
    assert speechscope["device"] == "cpu"
    assert "model" not in speechscope
    assert speechscope["config_sha256"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    assert (result.output_dir / "document-0001" / "000001.speechscope.json").is_file()


def test_checksum_mismatch_fails_before_asr(monkeypatch, tmp_path: Path) -> None:
    root, variant, _wav = _document_bundle(tmp_path)
    _install_fake_renderplan(monkeypatch, "0" * 64)
    config_path = root / "utterverify" / variant / "utterverify.toml"
    _install_fake_speechscope(monkeypatch, config_path)
    import utterverify.bundle as bundle_module

    monkeypatch.setattr(
        bundle_module,
        "_open_verifier",
        lambda _config: pytest.fail("verifier must not open before all WAV checksums pass"),
    )

    import pytest

    with pytest.raises(Exception, match="checksum mismatch"):
        verify_bundle(root, variant=variant)
    assert config_path.is_file()


def test_book_bundle_selects_source_number(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "novel.ssmd-bundle"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format": "ssmdconvert.book",
                "schema_version": 1,
                "ssmd_version": "0.9",
                "chapters": [],
            }
        ),
        encoding="utf-8",
    )
    variant = "narration--abcdef123456"
    plan_dir = root / "renderplan" / variant
    render_variant = root / "voicerender" / variant
    plan_dir.mkdir(parents=True)
    render_variant.mkdir(parents=True)
    (plan_dir / "renderplan-set.toml").write_text("format = 'renderplan-set'\n", encoding="utf-8")

    chapters = []
    manifests = {}
    plans = {}
    for source_number, chapter_id in ((5, "chapter-0005"), (6, "chapter-0006")):
        plan_path = plan_dir / f"{chapter_id}.renderplan.toml"
        plan_path.write_text("format = 'renderplan'\n", encoding="utf-8")
        render_dir = render_variant / chapter_id
        render_dir.mkdir()
        (render_dir / "render-manifest.toml").write_text(
            "format = 'render-manifest'\n", encoding="utf-8"
        )
        wav = render_dir / "000001.wav"
        wav.write_bytes(f"wave-{source_number}".encode())
        digest = hashlib.sha256(wav.read_bytes()).hexdigest()
        chapters.append(
            SimpleNamespace(source_number=source_number, id=chapter_id, renderplan=plan_path.name)
        )
        plans[str(plan_path.resolve())] = _PLAN
        manifests[str((render_dir / "render-manifest.toml").resolve())] = _Manifest(digest)

    module = ModuleType("renderplan")
    module.load_renderplan_set = lambda _path: SimpleNamespace(chapters=tuple(chapters))
    module.validate_renderplan_set = lambda _value: None
    module.load_plan = lambda path: plans[str(Path(path).resolve())]
    module.load_manifest = lambda path: manifests[str(Path(path).resolve())]
    monkeypatch.setitem(sys.modules, "renderplan", module)

    config_path = root / "utterverify" / variant / "utterverify.toml"
    _install_fake_speechscope(monkeypatch, config_path)
    result = verify_bundle(root, variant=variant, chapters="6")

    assert [unit.id for unit in result.units] == ["chapter-0006"]
    assert result.status == "pass"

    import pytest

    import utterverify.bundle as bundle_module

    bad_manifest = str((render_variant / "chapter-0006" / "render-manifest.toml").resolve())
    manifests[bad_manifest] = _Manifest("0" * 64)
    monkeypatch.setattr(
        bundle_module,
        "_open_verifier",
        lambda _config: pytest.fail("all selected handoffs must preflight before backend open"),
    )
    with pytest.raises(Exception, match="checksum mismatch"):
        verify_bundle(root, variant=variant, chapters="all")


def test_bundle_variant_discovery_selects_only_complete_rendered_variants(tmp_path: Path) -> None:
    root, variant, _wav = _document_bundle(tmp_path)
    assert discover_bundle_variants(root) == (variant,)
    assert resolve_bundle_variant(root, None) == variant
    assert resolve_bundle_variant(root, variant) == variant


def test_bundle_variant_discovery_rejects_zero_common_variants(tmp_path: Path) -> None:
    import pytest

    from utterverify.errors import BundleError

    root, variant, _wav = _document_bundle(tmp_path)
    (root / "voicerender" / variant).rename(root / "voicerender" / "other")
    with pytest.raises(BundleError, match="no complete rendered variant"):
        resolve_bundle_variant(root, None)


def test_bundle_variant_discovery_reports_ambiguity(tmp_path: Path) -> None:
    import pytest

    from utterverify.errors import BundleError

    root, _variant, _wav = _document_bundle(tmp_path)
    for component in ("renderplan", "voicerender"):
        (root / component / "alternate").mkdir()
    with pytest.raises(BundleError, match="multiple rendered variants are available"):
        resolve_bundle_variant(root, None)


def test_variant_symlinks_and_stale_utterverify_outputs_do_not_select_inputs(
    tmp_path: Path,
) -> None:
    import pytest

    from utterverify.errors import BundleError

    root, variant, _wav = _document_bundle(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (root / "renderplan" / "linked").symlink_to(external, target_is_directory=True)
    (root / "voicerender" / "linked").symlink_to(external, target_is_directory=True)
    assert discover_bundle_variants(root) == (variant,)
    with pytest.raises(BundleError, match="must not be a symlink"):
        resolve_bundle_variant(root, "linked")

    (root / "voicerender" / variant).rename(root / "voicerender" / "orphan")
    (root / "utterverify" / variant).mkdir(parents=True)
    with pytest.raises(BundleError, match="no complete rendered variant"):
        resolve_bundle_variant(root, None)


def test_configure_bundle_canonicalizes_whispercpp_alias(monkeypatch, tmp_path: Path) -> None:
    import speechscope

    root, variant, _wav = _document_bundle(tmp_path)

    class FakeModelManager:
        def __init__(self, *, packaged_catalog: bool) -> None:
            assert packaged_catalog is True

        def show(self, value: str, *, backend: str) -> dict[str, str]:
            assert value == "tiny-alias"
            assert backend == "whispercpp"
            return {"ref": "whispercpp:tiny.en-q5_1"}

    monkeypatch.setattr(speechscope, "ModelManager", FakeModelManager)
    path = configure_bundle(root, variant=variant, backend="whispercpp", model="tiny-alias")
    from utterverify.config import load_config

    assert load_config(path).model == "whispercpp:tiny.en-q5_1"


def test_configure_bundle_requires_action_for_stale_model(tmp_path: Path) -> None:
    import pytest

    from utterverify.bundle import init_bundle_config
    from utterverify.config import VerifyConfig
    from utterverify.errors import BundleError

    root, variant, _wav = _document_bundle(tmp_path)
    init_bundle_config(root, variant=variant, config=VerifyConfig(model="old-model"))
    with pytest.raises(BundleError, match="requires --model or --unset-model"):
        configure_bundle(root, variant=variant, backend="whispercpp")
    config_path = configure_bundle(root, variant=variant, backend="whispercpp", unset_model=True)
    from utterverify.config import load_config

    assert load_config(config_path).model is None


def test_configure_bundle_persists_local_whisper_model_as_absolute_path(tmp_path: Path) -> None:
    from utterverify.config import load_config

    root, variant, _wav = _document_bundle(tmp_path)
    model = tmp_path / "weights.bin"
    model.write_bytes(b"model")
    config_path = configure_bundle(root, variant=variant, backend="whispercpp", model=str(model))
    assert load_config(config_path).model == str(model.resolve())


def test_bundle_output_symlink_cannot_escape_bundle(tmp_path: Path) -> None:
    import pytest

    from utterverify.bundle import init_bundle_config
    from utterverify.errors import BundleError

    root, variant, _wav = _document_bundle(tmp_path)
    external = tmp_path / "external-output"
    external.mkdir()
    (root / "utterverify").symlink_to(external, target_is_directory=True)

    with pytest.raises(BundleError, match="must not be a symlink"):
        init_bundle_config(root, variant=variant)
    assert list(external.iterdir()) == []
