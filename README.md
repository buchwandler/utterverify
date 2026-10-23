# UtterVerify

`utterverify` is an optional content-validation gate between
[VoiceRender](https://github.com/buchwandler/voicerender) and
[AudioCompose](https://github.com/buchwandler/audiocompose). It compares each completed
VoiceRender speech WAV with the exact RenderPlan speech text by using SpeechScope.

It does **not** modify RenderPlans, VoiceRender WAVs, source SSMD, or AudioCompose state.

## Pipeline

```text
UtterPlan -> VoiceRender -> [UtterVerify] -> AudioCompose
                            optional
```

For bundle workflows, the default sibling layout is:

```text
book.ssmd-bundle/
├── renderplan/
│   └── narration--<fingerprint>/...
├── voicerender/
│   └── narration--<fingerprint>/...
├── utterverify/
│   └── narration--<fingerprint>/
│       ├── utterverify.toml
│       ├── utterverify-report.toml
│       ├── chapter-0001/
│       │   ├── 000001.speechscope.json
│       │   └── ...
│       └── ...
└── audiocompose/
```

The first UtterVerify-owned artifact is always `utterverify.toml`. `bundle verify`
automatically creates it before loading a RenderPlan or opening a SpeechScope backend.
You can also create/edit it explicitly before validation:

```bash
utterverify bundle init novel.ssmd-bundle --variant narration--28f616b6fe3b
$EDITOR novel.ssmd-bundle/utterverify/narration--28f616b6fe3b/utterverify.toml
utterverify bundle verify novel.ssmd-bundle --variant narration--28f616b6fe3b
```

Then continue with composition only if the gate succeeds:

```bash
audiocompose bundle compose novel.ssmd-bundle --variant narration--28f616b6fe3b
```

Book chapter selection mirrors the upstream source-number style:

```bash
utterverify bundle verify novel.ssmd-bundle \
  --variant narration--28f616b6fe3b \
  --chapters 5-17
```

Document bundles accept only `--chapters all`.

## Default policy

The generated `utterverify.toml` uses SpeechScope's exact-content policy shape:

```toml
format = "utterverify.config"
schema_version = 1

[speechscope]
backend = "redux"
device = "cpu"
normalization = "basic-v1"
use_language_hint = false

[thresholds]
schema_version = "1"
max_compound_wer = 0.0
max_cer = 0.0
missing_timing = "review"

[run]
fail_on_review = false
fail_fast = false
```

This intentionally makes policy a persisted, reviewable input rather than a hidden CLI
default. Edit it for a less strict gate or a different SpeechScope backend/model.

## Handoff checks

Before ASR runs, UtterVerify requires a complete `render-manifest.toml`, validates it
against the RenderPlan, checks each speech occurrence and deterministic WAV filename,
rejects symlink/path escapes, and verifies the WAV SHA-256 recorded by VoiceRender.
SpeechScope then writes one JSON report per segment.

A SpeechScope policy `fail` makes the command exit 1. Backend/input errors exit 2. Review
is exit 0 by default; set `run.fail_on_review = true` to make review block composition.

## Low-level one-plan workflow

```bash
utterverify verify chapter.renderplan.toml \
  --render-dir render/chapter-0001 \
  --output-dir verify/chapter-0001
```

This also creates `verify/chapter-0001/utterverify.toml` first.

## Install

```bash
pip install utterverify
```

For SpeechScope's Redux backend dependencies:

```bash
pip install 'utterverify[redux]'
```

Python 3.11+ is required because SpeechScope requires Python 3.11+.

## Development

The package uses a flat layout (`utterverify/`, no `src/`) and derives versions from Git
tags with `setuptools-scm`.

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
ruff check .
mypy utterverify
```
