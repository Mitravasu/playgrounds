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
                "OLLAMA_TIMEOUT_SECONDS=45",
                "CREATOR_MAX_COMPONENTS=3",
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
    assert settings.ollama_timeout_seconds == 45
    assert settings.creator_max_components == 3
    assert settings.ollama_planning_timeout_seconds == 120
    assert settings.ollama_structured_outputs is False
    assert settings.storybooks_directory == Path("storybooks")
    assert "test-api-key" not in repr(settings)


def test_api_key_is_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=tmp_path / "missing.env")  # type: ignore[call-arg]


def test_langfuse_credentials_must_be_configured_together(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        Settings(  # type: ignore[call-arg]
            _env_file=write_env(tmp_path),
            LANGFUSE_PUBLIC_KEY="pk-test",
        )


def test_creator_max_components_must_be_within_safety_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("CREATOR_MAX_COMPONENTS", "7")
    with pytest.raises(ValidationError):
        Settings(_env_file=write_env(tmp_path))  # type: ignore[call-arg]
