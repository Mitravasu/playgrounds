"""The trusted five-node POC analyzer workflow."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from playgrounds.runs import RunRecord, RunStore
from playgrounds.sandbox import (
    PublicAnalyzerJobRequest,
    SandboxArtifact,
    SandboxJobResult,
)
from playgrounds.style_guide import StyleGuide

STYLE_GUIDE_PROMPT_VERSION = "poc-style-guide-v1"
ANALYSIS_OUTPUTS = (
    SandboxArtifact(path="screenshot.png", media_type="image/png"),
    SandboxArtifact(path="page.json", media_type="application/json"),
    SandboxArtifact(path="observations.json", media_type="application/json"),
)


class StyleGuideSynthesizer(Protocol):
    """Host-side model boundary for one structured style-guide response."""

    def synthesize(
        self,
        *,
        source_url: str,
        page: Mapping[str, Any],
        observations: Mapping[str, Any],
        screenshot: bytes,
    ) -> StyleGuide: ...


class AnalyzerSandboxRunner(Protocol):
    """The narrow trusted interface used to start an analyzer sandbox job."""

    def run(
        self, request: PublicAnalyzerJobRequest, input_files: Mapping[str, bytes]
    ) -> SandboxJobResult: ...


class AnalyzerWorkflowError(RuntimeError):
    """A bounded workflow failure with its inspectable run ID."""

    def __init__(self, run_id: str, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id


@dataclass(frozen=True)
class AnalyzerWorkflow:
    """Validate, extract, synthesize, validate, and persist one page guide."""

    store: RunStore
    sandbox_runner: AnalyzerSandboxRunner
    synthesizer: StyleGuideSynthesizer

    def analyze(self, url: str) -> RunRecord:
        """Execute the POC graph and leave all successful evidence inspectable."""

        # Node 1: typed request creation is the trusted URL validation boundary.
        request = PublicAnalyzerJobRequest(url=url, outputs=ANALYSIS_OUTPUTS)
        run = self.store.create_run(request.url)
        try:
            # Node 2: the sandbox owns Playwright and returns declared artifacts only.
            result = self.sandbox_runner.run(request, {})
            if not result.succeeded:
                raise AnalyzerWorkflowError(
                    run.run_id, result.error or "analyzer sandbox job failed"
                )

            # Persist deterministic evidence before model inference so synthesis can be retried.
            self.store.persist_analysis_evidence(run.run_id, result.outputs)
            page = _parse_object(result.outputs["page.json"], "page.json")
            observations = _parse_object(result.outputs["observations.json"], "observations.json")

            # Node 3: the host-side model interprets evidence; it never enters the sandbox.
            guide = self.synthesizer.synthesize(
                source_url=request.url,
                page=page,
                observations=observations,
                screenshot=result.outputs["screenshot.png"],
            )

            # Node 4: reject a guide for another page even if its shape is otherwise valid.
            if str(guide.source_url) != request.url:
                raise ValueError("style guide source URL does not match the analyzed URL")

            # Node 5: persist only the schema-validated guide and model provenance.
            return self.store.persist_style_guide(
                run.run_id,
                guide.model_dump_json(indent=2).encode() + b"\n",
                model_name=getattr(self.synthesizer, "model_name", "configured-model"),
                prompt_version=STYLE_GUIDE_PROMPT_VERSION,
            )
        except Exception as error:
            message = str(error) or error.__class__.__name__
            self.store.mark_analysis_failed(run.run_id, message)
            if isinstance(error, AnalyzerWorkflowError):
                raise
            raise AnalyzerWorkflowError(run.run_id, message) from error


class OllamaStyleGuideSynthesizer:
    """Request a schema-constrained POC guide from the trusted Ollama client."""

    def __init__(self, client: Any, *, model_name: str) -> None:
        self._client = client
        self.model_name = model_name

    def synthesize(
        self,
        *,
        source_url: str,
        page: Mapping[str, Any],
        observations: Mapping[str, Any],
        screenshot: bytes,
    ) -> StyleGuide:
        """Convert deterministic evidence into a strictly validated style guide."""

        prompt = (
            "Synthesize one evidence-backed POC style guide for the analyzed page. "
            "Use only the supplied evidence. Mark a fact inferred=true when it is not "
            "directly observed. Every fact and component family needs observation IDs in "
            "evidence_refs. Return JSON that exactly matches the supplied schema.\n\n"
            f"Source URL: {source_url}\n"
            f"Page metadata: {json.dumps(page, sort_keys=True)}\n"
            f"Observations: {json.dumps(observations, sort_keys=True)}"
        )
        response = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt, "images": [screenshot]}],
            format=StyleGuide.model_json_schema(),
            options={"temperature": 0},
        )
        content = response.message.content
        if not content:
            raise ValueError("style-guide model returned no content")
        return StyleGuide.model_validate_json(content)


def _parse_object(content: bytes, artifact_name: str) -> dict[str, Any]:
    """Parse an already sandbox-validated JSON artifact as an object."""

    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"{artifact_name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise TypeError(f"{artifact_name} must contain a JSON object")
    return value
