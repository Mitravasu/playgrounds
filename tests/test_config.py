from pathlib import Path

import pytest
from pydantic import ValidationError

from playgrounds.config import Settings


def write_env(path: Path, *, api_key: str = "test-api-key") -> Path:
    env_file = path / ".env"
    env_file.write_text(
        "\n".join(
            (
                f"OLLAMA_API_KEY={api_key}",
                "OLLAMA_HOST=https://ollama.com",
                "OLLAMA_MODEL=gemma4:cloud",
                "PLAYGROUNDS_TRUSTED_ANALYZER_HOSTS=www.mitravasu.com,example.com",
            )
        ),
        encoding="utf-8",
    )
    return env_file


def test_settings_load_from_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    settings = Settings(_env_file=write_env(tmp_path))  # type: ignore[call-arg]

    assert settings.ollama_api_key.get_secret_value() == "test-api-key"
    assert str(settings.ollama_host) == "https://ollama.com/"
    assert settings.ollama_model == "gemma4:cloud"
    assert settings.trusted_analyzer_host_set == frozenset({"www.mitravasu.com", "example.com"})
    assert "test-api-key" not in repr(settings)


def test_api_key_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=tmp_path / "missing.env")  # type: ignore[call-arg]
