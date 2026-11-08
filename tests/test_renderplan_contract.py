from __future__ import annotations

import hashlib
import json
import sys
import tomllib
from pathlib import Path
from types import ModuleType

import pytest
import renderplan
from renderplan import (
    Producer,
    RenderManifest,
    RenderPlan,
    RenderPlanSet,
    RenderPlanSetChapter,
    SourceSpan,
    SpeechArtifact,
    SpeechItem,
    load_plan,
    load_renderplan_set,
    save_manifest,
    save_plan,
    save_renderplan_set,
)

from utterverify.bundle import verify_bundle
from utterverify.config import VerifyConfig
from utterverify.core import verify_renderplan
from utterverify.errors import HandoffError

_SOURCE_PLAN_ID = "sha256:" + "1" * 64
_PROFILE_ID = "sha256:" + "2" * 64
_VARIANT = "narration--0123456789ab"


class _VerificationRequest:
    def __init__(self, audio: Path, **kwargs) -> None:
        self.audio = audio
        self.__dict__.update(kwargs)


class _ThresholdPolicy:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class _Analysis:
    def __init__(self, status: str = "pass") -> None:
        self.evaluation = {"status": status, "rules": []}

    def write_json(self, path: Path) -> None:
        path.write_text(json.dumps({"evaluation": self.evaluation}), encoding="utf-8")


class _Verifier:
    def __init__(self, status: str = "pass") -> None:
        self.status = status
        self.requests: list[_VerificationRequest] = []
        self.entered = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *_args) -> bool:
        self.entered = False
        return False

    def analyze(self, request: _VerificationRequest) -> _Analysis:
        self.requests.append(request)
        return _Analysis(self.status)


def _install_fake_speechscope(monkeypatch) -> None:
    module = ModuleType("speechscope")
    module.ThresholdPolicy = _ThresholdPolicy
    module.VerificationRequest = _VerificationRequest
    monkeypatch.setitem(sys.modules, "speechscope", module)


def _write_plan(path: Path, texts: tuple[str, ...], *, profile_id: str = _PROFILE_ID) -> RenderPlan:
    speech = tuple(
        SpeechItem(
            number=index,
            text=text,
            language="en-US",
            role="narrator",
            binding={"voice": "test-voice"},
            source=(
                SourceSpan("chapter.ssmd.md", (index - 1) * 100, (index - 1) * 100 + len(text)),
            ),
        )
        for index, text in enumerate(texts, start=1)
    )
    save_plan(
        RenderPlan(
            source_plan_id=_SOURCE_PLAN_ID,
            render_profile_id=profile_id,
            flow=speech,
            producer=Producer("utterplan", "0.6.18"),
            schema_version=2,
        ),
        path,
    )
    loaded = load_plan(path)
    assert loaded.schema_version == 2
    return loaded


def _write_render(
    render_dir: Path,
    plan: RenderPlan,
    *,
    complete: bool = True,
    render_plan_id: str | None = None,
) -> tuple[Path, ...]:
    render_dir.mkdir(parents=True, exist_ok=True)
    wavs: list[Path] = []
    artifacts = []
    for speech in plan.speech:
        wav = render_dir / speech.wav
        wav.write_bytes(f"fake-wav-{speech.number}-{speech.text}".encode())
        wavs.append(wav)
        artifacts.append(
            SpeechArtifact(
                segment_number=speech.number,
                segment_id=speech.segment_id or speech.computed_id(),
                wav=speech.wav,
                sha256="sha256:" + hashlib.sha256(wav.read_bytes()).hexdigest(),
                frames=160,
                sample_rate=16_000,
                channels=1,
            )
        )
    manifest = RenderManifest(
        render_plan_id=render_plan_id or plan.plan_id,
        speech=tuple(artifacts),
        complete=complete,
        producer=Producer("voicerender", "0.1.0"),
    )
    save_manifest(manifest, render_dir / "render-manifest.toml")
    return tuple(wavs)


def _direct_handoff(
    root: Path,
    *,
    complete: bool = True,
    manifest_plan_id: str | None = None,
    texts: tuple[str, ...] = ("Prepared narration text.", "Second prepared segment."),
) -> tuple[Path, Path, Path, tuple[Path, ...]]:
    plan_path = root / "chapter.renderplan.toml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan = _write_plan(plan_path, texts)
    render_dir = root / "renders" / "chapter"
    wavs = _write_render(
        render_dir,
        plan,
        complete=complete,
        render_plan_id=manifest_plan_id,
    )
    return plan_path, render_dir, plan_path.parent / "verify", wavs


def _book_bundle(root: Path, *, identity_mismatch: str | None = None) -> tuple[Path, str]:
    root = root.with_name(f"{root.name}.ssmd-bundle")
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(
        json.dumps({"format": "ssmdconvert.book", "schema_version": 1, "chapters": []}),
        encoding="utf-8",
    )
    renderplan_dir = root / "renderplan" / _VARIANT
    renderplan_dir.mkdir(parents=True)
    renderplan_path = renderplan_dir / "chapter-0001.renderplan.toml"
    plan = _write_plan(renderplan_path, ("Book chapter prepared text.",))

    set_profile_id = _PROFILE_ID
    set_source_plan_id = plan.source_plan_id
    set_render_plan_id = plan.plan_id
    if identity_mismatch == "render_plan_id":
        set_render_plan_id = "sha256:" + "3" * 64
    elif identity_mismatch == "source_plan_id":
        set_source_plan_id = "sha256:" + "4" * 64
    elif identity_mismatch == "profile_id":
        set_profile_id = "sha256:" + "5" * 64

    chapter = RenderPlanSetChapter(
        id="chapter-0001",
        source_number=1,
        source="chapter-0001.ssmd.md",
        utterplan="utterplan/chapter-0001.utterplan.toml",
        source_plan_id=set_source_plan_id,
        renderplan=renderplan_path.name,
        render_plan_id=set_render_plan_id,
    )
    plan_set = RenderPlanSet(
        variant=_VARIANT,
        profile_fingerprint=set_profile_id.removeprefix("sha256:")[:12],
        producer_version="0.2.1",
        manifest="utterplan-manifest.toml",
        utterplan_dir="utterplan",
        profile_id=set_profile_id,
        snapshot="renderprofile.toml",
        source_profile="renderprofile.toml",
        chapters=(chapter,),
    )
    set_path = renderplan_dir / "renderplan-set.toml"
    save_renderplan_set(set_path, plan_set)
    assert load_renderplan_set(set_path).chapters == (chapter,)

    render_dir = root / "voicerender" / _VARIANT / chapter.id
    _write_render(render_dir, plan)
    return root, _VARIANT


def test_real_renderplan_v2_direct_handoff_uses_exact_text_and_persists_each_report(
    monkeypatch, tmp_path: Path
) -> None:
    assert renderplan.supported_contracts()["renderplan"] == 2
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, _wavs = _direct_handoff(tmp_path / "direct")
    verifier = _Verifier()

    result = verify_renderplan(
        plan_path,
        render_dir=render_dir,
        output_dir=output_dir,
        config=VerifyConfig(backend="redux"),
        verifier=verifier,
    )

    assert result.status == "pass"
    assert [segment.number for segment in result.segments] == [1, 2]
    assert [request.reference_text for request in verifier.requests] == [
        "Prepared narration text.",
        "Second prepared segment.",
    ]
    assert all(
        segment.report is not None and segment.report.is_file() for segment in result.segments
    )
    report = tomllib.loads((output_dir / "utterverify-report.toml").read_text(encoding="utf-8"))
    assert report["render_plan_id"] == result.render_plan_id
    assert report["source_plan_id"] == result.source_plan_id
    assert report["render_profile_id"] == result.render_profile_id
    assert report["manifest_id"] == result.manifest_id
    report_segments = report["segment"]
    assert [item["reference_text"] for item in report_segments] == [
        "Prepared narration text.",
        "Second prepared segment.",
    ]
    assert all(item["wav_sha256"].startswith("sha256:") for item in report_segments)
    assert all(item["status"] == "pass" for item in report_segments)


def test_real_renderplan_incomplete_manifest_is_rejected_before_asr(
    monkeypatch, tmp_path: Path
) -> None:
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, _wavs = _direct_handoff(
        tmp_path / "incomplete", complete=False
    )
    verifier = _Verifier()

    with pytest.raises(HandoffError, match="complete"):
        verify_renderplan(
            plan_path,
            render_dir=render_dir,
            output_dir=output_dir,
            config=VerifyConfig(backend="redux"),
            verifier=verifier,
        )
    assert verifier.requests == []


def test_real_renderplan_manifest_identity_mismatch_is_rejected_before_asr(
    monkeypatch, tmp_path: Path
) -> None:
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, _wavs = _direct_handoff(
        tmp_path / "mismatch", manifest_plan_id="sha256:" + "6" * 64
    )
    verifier = _Verifier()

    with pytest.raises(HandoffError, match="does not belong"):
        verify_renderplan(
            plan_path,
            render_dir=render_dir,
            output_dir=output_dir,
            config=VerifyConfig(backend="redux"),
            verifier=verifier,
        )
    assert verifier.requests == []


def test_real_renderplan_all_wav_hashes_are_preflighted_before_asr(
    monkeypatch, tmp_path: Path
) -> None:
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, wavs = _direct_handoff(tmp_path / "hash-mismatch")
    wavs[-1].write_bytes(b"changed after VoiceRender manifest")
    verifier = _Verifier()

    with pytest.raises(HandoffError, match="checksum mismatch"):
        verify_renderplan(
            plan_path,
            render_dir=render_dir,
            output_dir=output_dir,
            config=VerifyConfig(backend="redux"),
            verifier=verifier,
        )
    assert verifier.requests == []


def test_fail_fast_marks_later_real_occurrences_not_evaluated(monkeypatch, tmp_path: Path) -> None:
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, _wavs = _direct_handoff(
        tmp_path / "fail-fast", texts=("First fail.", "Skipped second.", "Skipped third.")
    )
    verifier = _Verifier(status="fail")

    result = verify_renderplan(
        plan_path,
        render_dir=render_dir,
        output_dir=output_dir,
        config=VerifyConfig(backend="redux", fail_fast=True),
        verifier=verifier,
    )

    assert result.status == "fail"
    assert [segment.status for segment in result.segments] == [
        "fail",
        "not_evaluated",
        "not_evaluated",
    ]
    assert len(verifier.requests) == 1
    assert "fail-fast" in result.segments[1].message


@pytest.mark.parametrize(
    ("speechscope_status", "expected_status"),
    [
        ("pass", "pass"),
        ("review", "review"),
        ("fail", "fail"),
        ("not_evaluated", "not_evaluated"),
        ("unexpected-status", "review"),
    ],
)
def test_real_renderplan_speechscope_status_mapping(
    monkeypatch, tmp_path: Path, speechscope_status: str, expected_status: str
) -> None:
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, _wavs = _direct_handoff(
        tmp_path / speechscope_status, texts=("Status mapping.",)
    )
    verifier = _Verifier(status=speechscope_status)

    result = verify_renderplan(
        plan_path,
        render_dir=render_dir,
        output_dir=output_dir,
        config=VerifyConfig(backend="redux"),
        verifier=verifier,
    )

    assert result.segments[0].status == expected_status


def test_real_renderplan_asr_exception_is_an_error_outcome(monkeypatch, tmp_path: Path) -> None:
    _install_fake_speechscope(monkeypatch)
    plan_path, render_dir, output_dir, _wavs = _direct_handoff(
        tmp_path / "asr-exception", texts=("ASR error.",)
    )

    class ErrorVerifier(_Verifier):
        def analyze(self, request: _VerificationRequest) -> _Analysis:
            self.requests.append(request)
            raise RuntimeError("decoder failed")

    result = verify_renderplan(
        plan_path,
        render_dir=render_dir,
        output_dir=output_dir,
        config=VerifyConfig(backend="redux"),
        verifier=ErrorVerifier(),
    )

    assert result.status == "error"
    assert result.segments[0].status == "error"
    assert result.segments[0].message == "decoder failed"


@pytest.mark.parametrize(
    "identity_mismatch", [None, "render_plan_id", "source_plan_id", "profile_id"]
)
def test_real_renderplan_book_set_identity_is_checked_before_asr(
    monkeypatch, tmp_path: Path, identity_mismatch: str | None
) -> None:
    _install_fake_speechscope(monkeypatch)
    import utterverify.bundle as bundle_module

    root, variant = _book_bundle(
        tmp_path / f"book-{identity_mismatch or 'valid'}", identity_mismatch=identity_mismatch
    )
    verifier = _Verifier()
    monkeypatch.setattr(bundle_module, "_open_verifier", lambda _config: verifier)

    if identity_mismatch is not None:
        with pytest.raises(HandoffError, match="does not match"):
            verify_bundle(root, variant=variant)
        assert verifier.requests == []
        assert verifier.entered is False
        return

    result = verify_bundle(root, variant=variant)
    assert result.status == "pass"
    assert [request.reference_text for request in verifier.requests] == [
        "Book chapter prepared text."
    ]
