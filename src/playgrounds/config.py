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
    sandbox_image: str = Field(
        default="playgrounds-browser:latest",
        min_length=1,
        validation_alias="PLAYGROUNDS_SANDBOX_IMAGE",
    )
    runs_directory: Path = Field(
        default=Path("runs"),
        validation_alias="PLAYGROUNDS_RUNS_DIRECTORY",
    )
    trusted_analyzer_hosts: str = Field(
        default="www.mitravasu.com",
        validation_alias="PLAYGROUNDS_TRUSTED_ANALYZER_HOSTS",
    )

    @property
    def trusted_analyzer_host_set(self) -> frozenset[str]:
        """Parse a small explicit comma-separated POC hostname allowlist."""

        hosts = frozenset(
            host.strip().lower().rstrip(".")
            for host in self.trusted_analyzer_hosts.split(",")
            if host.strip()
        )
        if not hosts:
            raise ValueError("trusted analyzer hosts must contain at least one hostname")
        return hosts


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide validated settings."""

    return Settings()
