from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from utterverify.bundle import verify_bundle


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
        evaluation = {"status": "pass", "rules": []}

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

    result = verify_bundle(root, variant=variant)

    assert result.config_created is True
    assert result.status == "pass"
    assert result.exit_code == 0
    assert result.report_path.is_file()
    assert (result.output_dir / "document-0001" / "000001.speechscope.json").is_file()


def test_checksum_mismatch_fails_before_asr(monkeypatch, tmp_path: Path) -> None:
    root, variant, _wav = _document_bundle(tmp_path)
    _install_fake_renderplan(monkeypatch, "0" * 64)
    config_path = root / "utterverify" / variant / "utterverify.toml"
    _install_fake_speechscope(monkeypatch, config_path)

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
