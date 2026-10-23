from pathlib import Path

from utterverify.config import VerifyConfig, ensure_config, load_config


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
