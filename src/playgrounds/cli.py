"""Local commands for the disposable-sandbox POC."""

import docker
import typer

from playgrounds.analyzer_workflow import AnalyzerWorkflow, OllamaStyleGuideSynthesizer
from playgrounds.config import get_settings
from playgrounds.llm import create_ollama_client
from playgrounds.runs import RunStore
from playgrounds.sandbox import SandboxRunner

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Analyze a site's rendered design language in isolated browser jobs."""


@app.command()
def analyze(url: str) -> None:
    """Analyze one trusted POC URL and print its persisted run directory."""

    settings = get_settings()
    workflow = AnalyzerWorkflow(
        store=RunStore(settings.runs_directory),
        sandbox_runner=SandboxRunner(docker.from_env(), image=settings.sandbox_image),
        synthesizer=OllamaStyleGuideSynthesizer(
            create_ollama_client(settings), model_name=settings.ollama_model
        ),
    )
    run = workflow.analyze(url)
    typer.echo(f"analysis complete: {settings.runs_directory / run.run_id}")


if __name__ == "__main__":
    app()
