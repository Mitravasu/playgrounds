from collections.abc import Mapping
from functools import cache
from typing import Any

from langfuse import Langfuse
from ollama import Client

from playgrounds.config import Settings, get_settings


class TracedOllamaClient:
    """Record Ollama chat calls as Langfuse generation observations."""

    def __init__(self, client: Any, langfuse: Any) -> None:
        self._client = client
        self._langfuse = langfuse

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate one chat request while recording its inputs, result, and usage."""

        model = kwargs.get("model")
        with self._langfuse.start_as_current_observation(
            as_type="generation",
            name="ollama.chat",
            model=str(model) if model is not None else None,
            input=_trace_value(kwargs.get("messages")),
            model_parameters=_model_parameters(kwargs),
            metadata={
                "provider": "ollama",
                "structured_output": kwargs.get("format") is not None,
            },
        ) as generation:
            try:
                response = self._client.chat(*args, **kwargs)
            except Exception as error:
                generation.update(level="ERROR", status_message=str(error))
                raise
            generation.update(
                output=_trace_value(getattr(response, "message", response)),
                usage_details=_usage_details(response),
            )
            return response

    def __getattr__(self, name: str) -> Any:
        """Delegate non-chat client operations unchanged."""

        return getattr(self._client, name)


def create_ollama_client(
    settings: Settings | None = None,
    *,
    timeout_seconds: float | None = None,
) -> Client | TracedOllamaClient:
    """Create an authenticated Ollama Cloud client."""

    resolved_settings = settings or get_settings()
    client = Client(
        host=str(resolved_settings.ollama_host),
        timeout=(
            resolved_settings.ollama_timeout_seconds if timeout_seconds is None else timeout_seconds
        ),
        headers={
            "Authorization": (f"Bearer {resolved_settings.ollama_api_key.get_secret_value()}")
        },
    )
    if (
        resolved_settings.langfuse_public_key is None
        or resolved_settings.langfuse_secret_key is None
    ):
        return client
    langfuse = _create_langfuse_client(
        resolved_settings.langfuse_public_key.get_secret_value(),
        resolved_settings.langfuse_secret_key.get_secret_value(),
        str(resolved_settings.langfuse_base_url),
    )
    return TracedOllamaClient(client, langfuse)


@cache
def _create_langfuse_client(public_key: str, secret_key: str, base_url: str) -> Langfuse:
    """Reuse one Langfuse exporter across the clients in a workflow."""

    return Langfuse(public_key=public_key, secret_key=secret_key, base_url=base_url)


def _trace_value(value: Any) -> Any:
    """Convert provider values into bounded, serializable trace data."""

    if isinstance(value, bytes):
        return {"type": "binary", "bytes": len(value)}
    if isinstance(value, Mapping):
        return {str(key): _trace_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_trace_value(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _trace_value(model_dump())
    return value


def _model_parameters(
    kwargs: Mapping[str, Any],
) -> dict[str, str | int | float | list[str]]:
    parameters: dict[str, str | int | float | list[str]] = {}
    for key, value in (kwargs.get("options") or {}).items():
        if isinstance(value, str | int | float) and not isinstance(value, bool):
            parameters[str(key)] = value
    think = kwargs.get("think")
    if isinstance(think, bool):
        parameters["think"] = str(think).lower()
    return parameters


def _usage_details(response: Any) -> dict[str, int] | None:
    input_tokens = getattr(response, "prompt_eval_count", None)
    output_tokens = getattr(response, "eval_count", None)
    usage = {}
    if isinstance(input_tokens, int):
        usage["input"] = input_tokens
    if isinstance(output_tokens, int):
        usage["output"] = output_tokens
    if isinstance(input_tokens, int) and isinstance(output_tokens, int):
        usage["total"] = input_tokens + output_tokens
    return usage or None
