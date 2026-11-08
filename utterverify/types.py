"""Result models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SegmentStatus = Literal["pass", "review", "fail", "not_evaluated", "error"]
UnitStatus = Literal["pass", "review", "fail", "error"]


@dataclass(frozen=True, slots=True)
class SegmentOutcome:
    number: int
    segment_id: str
    reference_text: str
    wav: Path
    wav_sha256: str
    report: Path | None
    status: SegmentStatus
    language: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class UnitVerifyResult:
    id: str
    renderplan: Path
    render_plan_id: str
    source_plan_id: str
    render_profile_id: str
    render_dir: Path
    manifest_id: str | None
    output_dir: Path
    segments: tuple[SegmentOutcome, ...]
    status: UnitStatus

    @property
    def passed(self) -> int:
        return sum(item.status == "pass" for item in self.segments)

    @property
    def reviewed(self) -> int:
        return sum(item.status in {"review", "not_evaluated"} for item in self.segments)

    @property
    def failed(self) -> int:
        return sum(item.status == "fail" for item in self.segments)

    @property
    def errors(self) -> int:
        return sum(item.status == "error" for item in self.segments)


@dataclass(frozen=True, slots=True)
class BundleVerifyResult:
    bundle: Path
    kind: Literal["book", "document"]
    variant: str
    output_dir: Path
    config_path: Path
    report_path: Path
    config_created: bool
    units: tuple[UnitVerifyResult, ...]
    status: UnitStatus
    fail_on_review: bool = False

    speechscope_backend: str = "redux"
    speechscope_device: str = "cpu"
    speechscope_model: str | None = None
    normalization: str = "basic-v1"
    use_language_hint: bool = False
    config_sha256: str | None = None

    @property
    def segments(self) -> int:
        return sum(len(unit.segments) for unit in self.units)

    @property
    def passed(self) -> int:
        return sum(unit.passed for unit in self.units)

    @property
    def reviewed(self) -> int:
        return sum(unit.reviewed for unit in self.units)

    @property
    def failed(self) -> int:
        return sum(unit.failed for unit in self.units)

    @property
    def errors(self) -> int:
        return sum(unit.errors for unit in self.units)

    @property
    def exit_code(self) -> int:
        if self.status == "error":
            return 2
        if self.status == "fail" or (self.status == "review" and self.fail_on_review):
            return 1
        return 0
