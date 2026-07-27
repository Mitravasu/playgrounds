from functools import lru_cache
from pathlib import Path

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables or `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    ollama_api_key: SecretStr = Field(min_length=1, validation_alias="OLLAMA_API_KEY")
    ollama_host: HttpUrl = Field(
        default=HttpUrl("https://ollama.com"),
        validation_alias="OLLAMA_HOST",
    )
    ollama_model: str = Field(
        default="gemma4:cloud",
        min_length=1,
        validation_alias="OLLAMA_MODEL",
    )
    creator_model: str = Field(
        default="gemma4:cloud",
        min_length=1,
        validation_alias="CREATOR_MODEL",
    )
    reviewer_model: str = Field(
        default="gemma4:cloud",
        min_length=1,
        validation_alias="REVIEWER_MODEL",
    )
    ollama_structured_outputs: bool = Field(
        default=False,
        validation_alias="OLLAMA_STRUCTURED_OUTPUTS",
        description=(
            "Send JSON schemas through Ollama's format field. Keep false for Ollama Cloud, "
            "which does not support structured outputs."
        ),
    )
    ollama_timeout_seconds: float = Field(
        default=600.0,
        gt=0,
        validation_alias="OLLAMA_TIMEOUT_SECONDS",
        description="Maximum time an Ollama HTTP operation may wait before failing.",
    )
    ollama_planning_timeout_seconds: float = Field(
        default=120.0,
        gt=0,
        validation_alias="OLLAMA_PLANNING_TIMEOUT_SECONDS",
        description="Short planning-only Ollama HTTP timeout before using the host fallback.",
    )
    sandbox_image: str = Field(
        default="playgrounds-browser:latest",
        min_length=1,
        validation_alias="PLAYGROUNDS_SANDBOX_IMAGE",
    )
    runs_directory: Path = Field(
        default=Path("runs"),
        validation_alias="PLAYGROUNDS_RUNS_DIRECTORY",
    )
    storybooks_directory: Path = Field(
        default=Path("storybooks"),
        validation_alias="PLAYGROUNDS_STORYBOOKS_DIRECTORY",
    )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide validated settings."""

    return Settings()
