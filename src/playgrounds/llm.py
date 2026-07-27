from ollama import Client

from playgrounds.config import Settings, get_settings


def create_ollama_client(
    settings: Settings | None = None,
    *,
    timeout_seconds: float | None = None,
) -> Client:
    """Create an authenticated Ollama Cloud client."""

    resolved_settings = settings or get_settings()
    return Client(
        host=str(resolved_settings.ollama_host),
        timeout=(
            resolved_settings.ollama_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        ),
        headers={
            "Authorization": (f"Bearer {resolved_settings.ollama_api_key.get_secret_value()}")
        },
    )
