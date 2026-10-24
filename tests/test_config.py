from pathlib import Path

from utterverify.config import VerifyConfig, ensure_config, load_config, update_config


def test_default_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "utterverify.toml"
    config, created = ensure_config(path)
    assert created is True
    assert path.is_file()
    loaded = load_config(path)
    assert loaded == config
    assert loaded.max_compound_wer == 0.0
    assert loaded.max_cer == 0.0


def test_existing_config_is_not_replaced(tmp_path: Path) -> None:
    path = tmp_path / "utterverify.toml"
    ensure_config(path, VerifyConfig(backend="whispercpp"))
    config, created = ensure_config(path, VerifyConfig(backend="redux"))
    assert created is False
    assert config.backend == "whispercpp"


def test_update_config_creates_missing_config_and_applies_only_supplied_fields(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "utterverify.toml"
    updated = update_config(
        path,
        backend="whispercpp",
        model="whispercpp:tiny.en-q5_1",
        use_language_hint=True,
        fail_fast=True,
    )
    assert updated.backend == "whispercpp"
    assert updated.device == "cpu"
    assert updated.model == "whispercpp:tiny.en-q5_1"
    assert updated.use_language_hint is True
    assert updated.fail_fast is True
    assert updated.max_compound_wer == 0.0


def test_update_config_preserves_thresholds_comments_and_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "utterverify.toml"
    ensure_config(path)
    text = path.read_text(encoding="utf-8")
    text = text.replace('backend = "redux"', 'backend = "redux" # keep comment')
    text += '\n[future]\nsetting = "preserve-me"\n'
    path.write_text(text, encoding="utf-8")

    updated = update_config(path, device="cuda", model="local.bin")
    persisted = path.read_text(encoding="utf-8")
    assert updated.device == "cuda"
    assert updated.model == "local.bin"
    assert updated.max_compound_wer == 0.0
    assert "# keep comment" in persisted
    assert '[future]\nsetting = "preserve-me"' in persisted

    cleared = update_config(path, model=None)
    assert cleared.model is None
    assert "model =" not in path.read_text(encoding="utf-8")
