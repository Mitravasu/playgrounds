from types import SimpleNamespace
from typing import Any, Self

import pytest

from playgrounds.config import Settings
from playgrounds.llm import TracedOllamaClient, create_ollama_client


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


def test_create_ollama_client_enables_tracing_when_credentials_are_set(
    monkeypatch: Any,
) -> None:
    ollama_client = object()
    langfuse_client = object()
    monkeypatch.setattr("playgrounds.llm.Client", lambda **_: ollama_client)
    monkeypatch.setattr(
        "playgrounds.llm._create_langfuse_client",
        lambda *_: langfuse_client,
    )
    settings = Settings(  # type: ignore[call-arg]
        OLLAMA_API_KEY="test-api-key",
        LANGFUSE_PUBLIC_KEY="pk-test",
        LANGFUSE_SECRET_KEY="sk-test",
        LANGFUSE_BASE_URL="https://us.cloud.langfuse.com",
    )

    client = create_ollama_client(settings)

    assert isinstance(client, TracedOllamaClient)
    assert client._client is ollama_client
    assert client._langfuse is langfuse_client


class FakeGeneration:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def update(self, **kwargs: Any) -> None:
        self.updates.append(kwargs)


class FakeLangfuse:
    def __init__(self) -> None:
        self.start_kwargs: dict[str, Any] = {}
        self.generation = FakeGeneration()

    def start_as_current_observation(self, **kwargs: Any) -> FakeGeneration:
        self.start_kwargs = kwargs
        return self.generation


def test_traced_client_records_chat_input_output_and_usage() -> None:
    response = SimpleNamespace(
        message=SimpleNamespace(model_dump=lambda: {"role": "assistant", "content": "Hello"}),
        prompt_eval_count=12,
        eval_count=4,
    )
    calls: list[dict[str, Any]] = []
    client = SimpleNamespace(chat=lambda **kwargs: calls.append(kwargs) or response)
    langfuse = FakeLangfuse()
    traced = TracedOllamaClient(client, langfuse)
    messages = [
        {"role": "user", "content": "Hi", "images": [b"image-bytes"]},
    ]

    result = traced.chat(
        model="gemma4:cloud",
        messages=messages,
        format={"type": "object"},
        think=False,
        options={"temperature": 0, "unsupported": object()},
    )

    assert result is response
    assert calls[0]["messages"] is messages
    assert langfuse.start_kwargs == {
        "as_type": "generation",
        "name": "ollama.chat",
        "model": "gemma4:cloud",
        "input": [
            {
                "role": "user",
                "content": "Hi",
                "images": [{"type": "binary", "bytes": 11}],
            }
        ],
        "model_parameters": {"temperature": 0, "think": "false"},
        "metadata": {"provider": "ollama", "structured_output": True},
    }
    assert langfuse.generation.updates == [
        {
            "output": {"role": "assistant", "content": "Hello"},
            "usage_details": {"input": 12, "output": 4, "total": 16},
        }
    ]


def test_traced_client_records_errors_and_reraises() -> None:
    def fail(**_: Any) -> None:
        raise RuntimeError("provider unavailable")

    langfuse = FakeLangfuse()
    traced = TracedOllamaClient(
        SimpleNamespace(chat=fail),
        langfuse,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        traced.chat(model="gemma4:cloud", messages=[])

    assert langfuse.generation.updates == [
        {"level": "ERROR", "status_message": "provider unavailable"}
    ]
