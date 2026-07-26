"""Local commands for the disposable-sandbox POC."""

import docker
import typer

from playgrounds.analyzer_workflow import AnalyzerWorkflow, OllamaStyleGuideSynthesizer
from playgrounds.config import get_settings
from playgrounds.creator_workflow import (
    CreatorWorkflow,
    OllamaComponentGenerator,
    OllamaComponentReviewer,
)
from playgrounds.llm import create_ollama_client
from playgrounds.runs import RunStore
from playgrounds.sandbox import SandboxRunner

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Analyze a site's rendered design language in isolated browser jobs."""


@app.command()
def analyze(url: str) -> None:
    """Analyze one manual public HTTPS URL and print its persisted run directory."""

    settings = get_settings()
    workflow = AnalyzerWorkflow(
        store=RunStore(settings.runs_directory),
        sandbox_runner=SandboxRunner(docker.from_env(), image=settings.sandbox_image),
        synthesizer=OllamaStyleGuideSynthesizer(
            create_ollama_client(settings), model_name=settings.ollama_model, reporter=typer.echo
        ),
        reporter=typer.echo,
    )
    run = workflow.analyze(url)
    typer.echo(f"analysis complete: {settings.runs_directory / run.run_id}")


@app.command()
def create(run_folder_name: str, prompt: str) -> None:
    """Generate one component from an analyzed run folder and a prompt."""

    settings = get_settings()
    run_id = run_folder_name.rstrip("/").rsplit("/", 1)[-1]
    client = create_ollama_client(settings)
    workflow = CreatorWorkflow(
        store=RunStore(settings.runs_directory),
        sandbox_runner=SandboxRunner(docker.from_env(), image=settings.sandbox_image),
        generator=OllamaComponentGenerator(
            client, model_name=settings.creator_model, reporter=typer.echo
        ),
        reviewer=OllamaComponentReviewer(
            client, model_name=settings.reviewer_model, reporter=typer.echo
        ),
        components_directory=settings.components_directory,
        reporter=typer.echo,
    )
    result = workflow.create(run_id, prompt)
    status = "passed" if result.evaluation.passed else "best valid attempt"
    typer.echo(
        f"creation complete ({status}, score {result.evaluation.aggregate_score:.2f}): "
        f"{result.component_directory}"
    )


if __name__ == "__main__":
    app()
