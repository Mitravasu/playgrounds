from typing import Any

from playgrounds.config import Settings
from playgrounds.llm import create_ollama_client


def test_create_ollama_client_uses_cloud_credentials(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("playgrounds.llm.Client", fake_client)
    settings = Settings(  # type: ignore[call-arg]
        OLLAMA_API_KEY="test-api-key",
        OLLAMA_HOST="https://ollama.com",
        OLLAMA_MODEL="gemma4:cloud",
    )

    create_ollama_client(settings)

    assert captured == {
        "host": "https://ollama.com/",
        "timeout": 600.0,
        "headers": {"Authorization": "Bearer test-api-key"},
    }


def test_create_ollama_client_accepts_a_shorter_planning_timeout(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("playgrounds.llm.Client", fake_client)
    settings = Settings(  # type: ignore[call-arg]
        OLLAMA_API_KEY="test-api-key",
        OLLAMA_HOST="https://ollama.com",
    )

    create_ollama_client(settings, timeout_seconds=settings.ollama_planning_timeout_seconds)

    assert captured["timeout"] == 120.0
