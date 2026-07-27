"""The trusted five-node POC analyzer workflow."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from playgrounds.runs import RunRecord, RunStore
from playgrounds.sandbox import (
    PublicAnalyzerJobRequest,
    SandboxArtifact,
    SandboxJobResult,
)
from playgrounds.style_guide import CaptureMetadata, StyleGuide, StyleGuideContent, Viewport
from playgrounds.synthesis_evidence import summarize_observations

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
    ) -> str: ...

    def repair(self, *, response: str, validation_error: str) -> str: ...


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


def _discard_progress(_: str) -> None:
    """Keep the workflow quiet unless its caller explicitly requests progress."""


@dataclass(frozen=True)
class AnalyzerWorkflow:
    """Validate, extract, synthesize, validate, and persist one page guide."""

    store: RunStore
    sandbox_runner: AnalyzerSandboxRunner
    synthesizer: StyleGuideSynthesizer
    reporter: Callable[[str], None] = _discard_progress

    def analyze(self, url: str) -> RunRecord:
        """Execute the POC graph and leave all successful evidence inspectable."""

        self.reporter("Validating public URL...")
        # Node 1: typed request creation is the trusted URL validation boundary.
        request = PublicAnalyzerJobRequest(url=url, outputs=ANALYSIS_OUTPUTS)
        run = self.store.create_run(request.url)
        try:
            # Node 2: the sandbox owns Playwright and returns declared artifacts only.
            self.reporter("Starting analyzer sandbox...")
            result = self.sandbox_runner.run(request, {})
            if result.logs:
                self.store.persist_analysis_sandbox_log(run.run_id, result.logs)
            if not result.succeeded:
                raise AnalyzerWorkflowError(
                    run.run_id, result.error or "analyzer sandbox job failed"
                )

            # Persist deterministic evidence before model inference so synthesis can be retried.
            self.reporter("Persisting analyzer evidence...")
            self.store.persist_analysis_evidence(run.run_id, result.outputs)
            page = _parse_object(result.outputs["page.json"], "page.json")
            observations = _parse_object(result.outputs["observations.json"], "observations.json")

            # Node 3: the host-side model interprets evidence; it never enters the sandbox.
            self.reporter("Synthesizing style guide...")
            response = self.synthesizer.synthesize(
                source_url=request.url,
                page=page,
                observations=summarize_observations(observations),
                screenshot=result.outputs["screenshot.png"],
            )
            self.store.persist_style_guide_response(run.run_id, response, attempt=1)
            try:
                guide = validate_style_guide_response(response, source_url=request.url, page=page)
            except StyleGuideValidationError as error:
                self.reporter("Style guide needs repair; sending validation errors to model...")
                response = self.synthesizer.repair(response=response, validation_error=str(error))
                self.store.persist_style_guide_response(run.run_id, response, attempt=2)
                guide = validate_style_guide_response(response, source_url=request.url, page=page)

            # Node 4: reject a guide for another page even if its shape is otherwise valid.
            if str(guide.source_url) != request.url:
                raise ValueError("style guide source URL does not match the analyzed URL")

            # Node 5: persist only the schema-validated guide and model provenance.
            self.reporter("Validating and persisting style guide...")
            return self.store.persist_style_guide(
                run.run_id,
                guide.model_dump_json(indent=2).encode() + b"\n",
                model_name=getattr(self.synthesizer, "model_name", "configured-model"),
                prompt_version=STYLE_GUIDE_PROMPT_VERSION,
            )
        except Exception as error:
            self.reporter("Analysis failed; recording the failure...")
            message = str(error) or error.__class__.__name__
            self.store.mark_analysis_failed(run.run_id, message)
            if isinstance(error, AnalyzerWorkflowError):
                raise
            raise AnalyzerWorkflowError(run.run_id, message) from error


class OllamaStyleGuideSynthesizer:
    """Request a schema-constrained POC guide from the trusted Ollama client."""

    def __init__(
        self,
        client: Any,
        *,
        model_name: str,
        reporter: Callable[[str], None] = _discard_progress,
        use_structured_outputs: bool = False,
    ) -> None:
        self._client = client
        self.model_name = model_name
        self._reporter = reporter
        self._use_structured_outputs = use_structured_outputs

    def synthesize(
        self,
        *,
        source_url: str,
        page: Mapping[str, Any],
        observations: Mapping[str, Any],
        screenshot: bytes,
    ) -> str:
        """Ask the model for one raw style-guide response."""

        prompt = (
            "Synthesize one evidence-backed POC style guide for the analyzed page.\n"
            "Use only the supplied evidence. Mark a fact inferred=true when it is not directly "
            "observed. Every entry needs observation IDs in evidence_refs.\n"
            "Return exactly one JSON object matching the supplied schema. Do not use Markdown "
            "code fences. The root keys must be colors, typography, spacing, surfaces, "
            "component_patterns, interaction_states, and layout_rules.\n"
            "Use component_patterns only for reusable UI structures. Each entry has name, "
            "description, evidence_refs, and optional variants, styles, and inferred.\n"
            "Use interaction_states only for a state of one component pattern. Each entry has "
            "component_pattern, state, description, evidence_refs, and optional styles and "
            "inferred.\n"
            "Use layout_rules only for page-level arrangement rules. Each entry has name, "
            "description, value, evidence_refs, and optional inferred.\n"
            "Do not place a field from one section's entry shape into another section. The host "
            "adds the source URL and capture metadata. Preserve semantic links as links. When an "
            "evidence item has visual_role=button_like_link, describe it as a button-like link "
            "or action link, never as a native button.\n\n"
            f"Source URL: {source_url}\n"
            f"Page metadata: {json.dumps(page, sort_keys=True)}\n"
            f"Compact evidence summary: {json.dumps(observations, sort_keys=True)}"
        )
        self._reporter("Sending message to model")
        response = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt, "images": [screenshot]}],
            format=(
                StyleGuideContent.model_json_schema() if self._use_structured_outputs else None
            ),
            think=False,
            options={"temperature": 0},
        )
        self._reporter("Received response from reporter")
        content = response.message.content
        if not content:
            raise ValueError("style-guide model returned no content")
        self._reporter(f"Style-guide model response: {_preview(content)}")
        return content

    def repair(self, *, response: str, validation_error: str) -> str:
        """Request one schema-only repair without revisiting the page evidence."""

        prompt = (
            "Repair the following style-guide response so it exactly matches the supplied JSON "
            "schema. Return only the repaired JSON object, with no Markdown fence or explanation. "
            "Keep supported facts and evidence references; do not invent new page evidence.\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        self._reporter("Sending style-guide repair request to model...")
        repaired = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            format=(
                StyleGuideContent.model_json_schema() if self._use_structured_outputs else None
            ),
            think=False,
            options={"temperature": 0},
        ).message.content
        if not repaired:
            raise ValueError("style-guide repair model returned no content")
        self._reporter(f"Style-guide repair response: {_preview(repaired)}")
        return repaired


class StyleGuideValidationError(ValueError):
    """A concise validation failure safe to return to the repair prompt."""


def validate_style_guide_response(
    content: str, *, source_url: str, page: Mapping[str, Any]
) -> StyleGuide:
    """Add host facts and validate the model's response against the POC schema."""

    try:
        return _style_guide_from_model_response(content, source_url=source_url, page=page)
    except ValidationError as error:
        first_errors = error.errors(include_url=False)[:5]
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'response'}: {item['msg']}"
            for item in first_errors
        )
        raise StyleGuideValidationError(
            f"style-guide response does not match the required schema: {details}"
        ) from error
    except (TypeError, ValueError) as error:
        raise StyleGuideValidationError(str(error)) from error


def _parse_object(content: bytes, artifact_name: str) -> dict[str, Any]:
    """Parse an already sandbox-validated JSON artifact as an object."""

    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"{artifact_name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise TypeError(f"{artifact_name} must contain a JSON object")
    return value


def _preview(content: str, limit: int = 150) -> str:
    """Return one bounded, single-line model-output preview for the local CLI."""

    normalized = " ".join(content.split())
    return normalized[:limit] + ("..." if len(normalized) > limit else "")


def _json_content(content: str) -> str:
    """Accept a harmless Markdown fence while rejecting all other non-JSON output."""

    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) < 3 or not lines[-1].strip() == "```":
            raise ValueError("style-guide response has an incomplete Markdown code fence")
        stripped = "\n".join(lines[1:-1]).strip()
    return stripped


def _style_guide_from_model_response(
    content: str, *, source_url: str, page: Mapping[str, Any]
) -> StyleGuide:
    """Add trusted capture facts after validating the model-owned guide content."""

    try:
        candidate = json.loads(_json_content(content))
    except json.JSONDecodeError as error:
        raise ValueError("style-guide response is not valid JSON") from error
    if not isinstance(candidate, dict):
        raise TypeError("style-guide response must contain a JSON object")
    for host_owned_key in ("schema_version", "source_url", "capture"):
        candidate.pop(host_owned_key, None)
    synthesis = StyleGuideContent.model_validate(candidate)
    viewport = page.get("viewport")
    if not isinstance(viewport, dict):
        raise TypeError("page.json must contain viewport metadata")
    raw_title: object = page.get("title")
    return StyleGuide(
        source_url=source_url,
        capture=CaptureMetadata(
            title=raw_title if isinstance(raw_title, str) else "",
            viewport=Viewport(width=viewport["width"], height=viewport["height"]),
        ),
        **synthesis.model_dump(),
    )
