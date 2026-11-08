# UtterVerify

`utterverify` is an optional content-validation gate between
[VoiceRender](https://github.com/buchwandler/voicerender) and
[AudioCompose](https://github.com/buchwandler/audiocompose). It compares each completed
VoiceRender speech WAV with the exact RenderPlan speech text by using SpeechScope.

UtterVerify targets RenderPlan 0.2.1 (`renderplan>=0.2.1,<0.3`) and SpeechScope 0.1.1 (`speechscope>=0.1.1,<0.2`).
It does **not** modify RenderPlans, VoiceRender WAVs, source SSMD, or AudioCompose state.

## Pipeline

```text
UtterPlan
  ↓
RenderPlan
  ↓
VoiceRender
  ↓
[UtterVerify] (optional)
  ↓
AudioCompose
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

#### Optional local whisper.cpp readiness and smoke test

If a whisper.cpp model is already installed, pass its existing file path to Doctor for a read-only readiness check; this does not download, install, move, or overwrite model weights:

```bash
MODEL="$HOME/.cache/speechscope/whispercpp/tiny.en-q5_1/ggml-tiny.en-q5_1.bin"
test -r "$MODEL"
utterverify doctor --engine whispercpp --model "$MODEL"
```

Doctor checks the executable and model availability/readability without inference. To run an optional ASR smoke test, configure a direct handoff to use that local file and verify a completed RenderPlan/VoiceRender pair:

```bash
utterverify configure ./verify/chapter --engine whispercpp --model "$MODEL"
utterverify verify ./renderplan/chapter.renderplan.toml \
  --render-dir ./voicerender/chapter \
  --output-dir ./verify/chapter
```

Only `verify` runs inference. Skip this optional smoke test when no suitable completed handoff is available; no model installation is required.

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

This intentionally makes policy a persisted, reviewable input rather than a hidden CLI default. Edit it for a less strict gate or a different SpeechScope backend/model.

This is compound-aware exact-content verification: zero compound-aware lexical error and zero character error do not require strict whitespace-tokenized word-boundary equality. For example, SpeechScope can accept a compound split such as “audiobook” versus “audio book”; this policy is not byte-exact or strict word equality.

## Handoff checks

Before ASR runs, UtterVerify requires a complete `render-manifest.toml`, validates it
against the RenderPlan, checks each speech occurrence and deterministic WAV filename,
rejects symlink/path escapes, and verifies the WAV SHA-256 recorded by VoiceRender before ASR, so stale or replaced audio is rejected before inference.
SpeechScope then writes one JSON report per segment.
UtterVerify passes the exact prepared RenderPlan `SpeechItem.text` as SpeechScope's reference text.
A SpeechScope policy `fail` makes verification exit 1. Backend or input errors exit 2. `review` is a non-pass result requiring attention (including missing timing under the default policy); it exits 0 by default, or 1 when `run.fail_on_review = true`.

Direct and bundle `verify` return 0 for pass or permitted review, 1 for fail or blocking review, and 2 for input/backend errors. `doctor` returns 0 when ready, 3 when runtime readiness checks fail, and 2 for invalid config or bundle input.

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
