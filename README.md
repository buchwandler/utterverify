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

## Bundle workflow

The first UtterVerify-owned artifact is always `utterverify.toml`. `bundle verify`
creates the selected variant's config before loading a RenderPlan or opening SpeechScope.
When the bundle contains exactly one complete variant in both `renderplan/` and
`voicerender/`, `--variant` is optional. Symlinked variants are not followed. If there are
multiple complete variants, UtterVerify lists them and requires explicit `--variant`
selection; stale `utterverify/` output never selects an input variant.

For a bundle with one rendered variant, configure and run the gate like this:

```bash
# Backend and target model are stored in utterverify.toml.
utterverify bundle configure novel.ssmd-bundle \
  --engine whispercpp \
  --model whispercpp:tiny.en-q5_1

# Installation and model-weight management belong to SpeechScope.
speechscope models install whispercpp:tiny.en-q5_1

# Doctor checks local readiness without inference or writes.
utterverify doctor novel.ssmd-bundle

# Variant is inferred because there is exactly one complete candidate.
utterverify bundle verify novel.ssmd-bundle --chapters 5-17
```

`--engine` is a user-friendly alias for `--backend`; both update the canonical
`[speechscope].backend` key. Managed model references are preferred. Existing local model
files are supported and saved as absolute paths when configured. UtterVerify never downloads
or installs model weights. `bundle configure` can also update device, normalization,
language-hint and review/fail-fast policy flags while preserving threshold settings.

If no config exists, `bundle configure` creates the defaults and applies the supplied
settings. `bundle init` remains available for creating a default or explicitly selected
config. If the bundle contains multiple complete variants, specify one for configure,
doctor, init, and verify:

```bash
utterverify bundle configure novel.ssmd-bundle \
  --variant narration--28f616b6fe3b \
  --engine whispercpp \
  --model whispercpp:tiny.en-q5_1

utterverify doctor novel.ssmd-bundle --variant narration--28f616b6fe3b
utterverify bundle verify novel.ssmd-bundle \
  --variant narration--28f616b6fe3b \
  --chapters 5-17
```

For direct environment checks, `utterverify doctor` can use an optional config file and
temporary overrides:

```bash
utterverify doctor --config ./utterverify.toml --backend redux
utterverify doctor --engine whispercpp --model whispercpp:tiny.en-q5_1 --json
```

Doctor performs no inference, config creation, report writes, model downloads, or installs.
For whisper.cpp it checks both `whisper-cli` and the selected model; an executable without
a model is not ready, and an installed model without the executable is not ready. A model
may come from `--model`, the config, or `SPEECHSCOPE_WHISPERCPP_MODEL` (in that order).
Environment-sourced models are reported as such and warned as unpinned. Configure a
reproducible bundle model with `bundle configure`. `SPEECHSCOPE_WHISPERCPP_BIN` is honored
by the same SpeechScope executable probe used by its backend. Redux readiness is a
side-effect-free dependency metadata preflight and does not prove full runtime/device
readiness. Doctor exits 0 when ready, 3 when the selected runtime is not ready, and 2 for
invalid config or bundle input.

Then continue with composition only if the gate succeeds:

```bash
audiocompose bundle compose novel.ssmd-bundle --variant narration--28f616b6fe3b
```

Book chapter selection uses source chapter numbers (`all`, `5`, `5-17`, or `5,7,10-12`),
not array indexes. Document bundles accept only `--chapters all`.

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
