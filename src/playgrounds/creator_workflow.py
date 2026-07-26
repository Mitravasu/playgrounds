"""Bounded host-side component generation, rendering, review, and publication."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from playgrounds.runs import RunRecord, RunStore
from playgrounds.sandbox import (
    CreatorJobRequest,
    SandboxArtifact,
    SandboxJobResult,
)

CREATOR_PROMPT_VERSION = "poc-creator-v1"
REVIEWER_PROMPT_VERSION = "poc-reviewer-v1"
RUBRIC_VERSION = "poc-rubric-v1"
MAX_ATTEMPTS = 2
DELIVERY_THRESHOLD = 80.0
PROTECTED_SCORE_FLOOR = 65
PROTECTED_DIMENSIONS = (
    "design_language_adherence",
    "contextual_appropriateness",
    "interaction_and_state_quality",
)
RUBRIC_WEIGHTS = {
    "design_language_adherence": 0.35,
    "contextual_appropriateness": 0.25,
    "interaction_and_state_quality": 0.15,
    "responsive_behavior": 0.10,
    "accessibility_beyond_baseline": 0.10,
    "implementation_quality": 0.05,
}
CREATOR_INPUTS = (
    SandboxArtifact(path="component.html", media_type="text/html"),
    SandboxArtifact(path="component.css", media_type="text/css"),
    SandboxArtifact(path="component.js", media_type="text/javascript"),
)
CREATOR_OUTPUTS = (
    SandboxArtifact(path="screenshot.png", media_type="image/png"),
    SandboxArtifact(path="render.json", media_type="application/json"),
)
InferenceText = Annotated[str, Field(min_length=1, max_length=500)]
FeedbackText = Annotated[str, Field(min_length=1, max_length=1_000)]


class GeneratedComponent(BaseModel):
    """The three model-owned portions of one browser-native component."""

    model_config = ConfigDict(extra="forbid", strict=True)

    markup: str = Field(
        min_length=1,
        max_length=500_000,
        description=(
            "One HTML fragment with exactly one outer data-pg-component element; excludes "
            "html, head, body, style, and script tags."
        ),
    )
    css: str = Field(
        min_length=1,
        max_length=500_000,
        description=(
            "Plain CSS whose selectors are scoped beneath [data-pg-component]; contains no "
            "style tags, imports, or external resources."
        ),
    )
    javascript: str = Field(
        max_length=500_000,
        description=(
            "Deferred classic browser JavaScript wrapped in an IIFE; contains no script tags, "
            "module imports, globals, packages, or network access. Use an empty string when the "
            "component requires no JavaScript behavior."
        ),
    )
    inferred_choices: list[InferenceText] = Field(
        max_length=50,
        description=(
            "Concise explanations of design choices not directly observed in the style guide; "
            "use an empty array when every choice is evidence-backed."
        ),
    )


class RubricScores(BaseModel):
    """Reviewer-owned subscores; application code owns aggregation and routing."""

    model_config = ConfigDict(extra="forbid", strict=True)

    design_language_adherence: int = Field(
        ge=0,
        le=100,
        description="Fit with the source site's visual and behavioral design language.",
    )
    contextual_appropriateness: int = Field(
        ge=0,
        le=100,
        description="Suitability of hierarchy, emphasis, and behavior for the requested use.",
    )
    interaction_and_state_quality: int = Field(
        ge=0,
        le=100,
        description="Quality and completeness of pointer, keyboard, focus, and component states.",
    )
    responsive_behavior: int = Field(
        ge=0,
        le=100,
        description="Usability and visual coherence across plausible viewport sizes.",
    )
    accessibility_beyond_baseline: int = Field(
        ge=0,
        le=100,
        description="Accessibility quality beyond the deterministic baseline hard gates.",
    )
    implementation_quality: int = Field(
        ge=0,
        le=100,
        description="Code clarity, isolation, maintainability, and offline self-containment.",
    )


ReviewDimension = Literal[
    "design_language_adherence",
    "contextual_appropriateness",
    "interaction_and_state_quality",
    "responsive_behavior",
    "accessibility_beyond_baseline",
    "implementation_quality",
]


class ConcreteProblem(BaseModel):
    """One reviewer finding tied to a fixed rubric dimension and revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dimension: ReviewDimension = Field(
        description="The one rubric dimension directly affected by this problem."
    )
    problem: FeedbackText = Field(
        description="A concrete deficiency cited from the supplied code, evidence, or screenshot."
    )
    actionable_revision: FeedbackText = Field(
        description="One concrete source change that directly addresses the cited problem."
    )


class ReviewerResponse(RubricScores):
    """The strict flat response shape sent to and received from the reviewer model."""

    concrete_problems: list[ConcreteProblem] = Field(
        max_length=20,
        description="Structured deficiencies and revisions; use an empty array when none exist.",
    )


class RubricReview(BaseModel):
    """Trusted-host review data used for deterministic scoring and revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scores: RubricScores
    cited_problems: list[FeedbackText] = Field(
        max_length=20,
        description=(
            "Concrete evidence-based deficiencies; use an empty array when none are found."
        ),
    )
    revision_instructions: list[FeedbackText] = Field(
        max_length=20,
        description=(
            "Concrete changes addressing cited problems; use an empty array when none are needed."
        ),
    )


class AttemptEvaluation(BaseModel):
    """Deterministic routing result plus inspectable reviewer feedback."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    rubric_version: str = RUBRIC_VERSION
    attempt: int
    hard_gates: dict[str, bool]
    hard_gates_passed: bool
    scores: RubricScores
    aggregate_score: float
    protected_dimensions_passed: bool
    passed: bool
    cited_problems: list[str]
    revision_instructions: list[str]


@dataclass(frozen=True)
class GenerationResult:
    component: GeneratedComponent
    raw_responses: tuple[str, ...]


@dataclass(frozen=True)
class ReviewResult:
    review: RubricReview
    raw_responses: tuple[str, ...]


@dataclass(frozen=True)
class CreatorResult:
    """The completed run record and directly openable published component."""

    run: RunRecord
    creation_id: str
    component_directory: Path
    evaluation: AttemptEvaluation


class ComponentGenerator(Protocol):
    model_name: str

    def generate(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
    ) -> GenerationResult: ...

    def revise(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        component: GeneratedComponent,
        evaluation: AttemptEvaluation,
        render_screenshot: bytes | None,
    ) -> GenerationResult: ...


class ComponentReviewer(Protocol):
    model_name: str

    def review(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        component: GeneratedComponent,
        render_diagnostics: Mapping[str, Any],
        render_screenshot: bytes,
    ) -> ReviewResult: ...


class CreatorSandboxRunner(Protocol):
    def run(
        self, request: CreatorJobRequest, input_files: Mapping[str, bytes]
    ) -> SandboxJobResult: ...


class CreatorWorkflowError(RuntimeError):
    """A creator failure tied to an inspectable run and creation."""

    def __init__(self, run_id: str, creation_id: str, message: str) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.creation_id = creation_id


class ModelResponseError(ValueError):
    """A final model-contract failure retaining every bounded raw response."""

    def __init__(self, message: str, *, role: str, responses: tuple[str, ...]) -> None:
        super().__init__(message)
        self.role = role
        self.responses = responses


def _discard_progress(_: str) -> None:
    """Keep the workflow quiet unless its caller explicitly requests progress."""


@dataclass(frozen=True)
class _Attempt:
    number: int
    component: GeneratedComponent
    files: dict[str, bytes]
    outputs: dict[str, bytes]
    diagnostics: dict[str, Any]
    evaluation: AttemptEvaluation


@dataclass(frozen=True)
class CreatorWorkflow:
    """Generate, sandbox, review, revise once, and publish the best valid attempt."""

    store: RunStore
    sandbox_runner: CreatorSandboxRunner
    generator: ComponentGenerator
    reviewer: ComponentReviewer
    components_directory: Path = Path("components")
    reporter: Callable[[str], None] = _discard_progress

    def create(self, run_id: str, prompt: str) -> CreatorResult:
        """Create one component from a completed analyzer run."""

        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("component prompt must not be empty")
        self.reporter("Loading source style guide and screenshot...")
        style_guide = _json_object(
            self.store.load_analysis_artifact(run_id, "style-guide.json"), "style-guide.json"
        )
        source_screenshot = self.store.load_analysis_artifact(run_id, "screenshot.png")
        creation = self.store.create_creation(run_id, normalized_prompt)
        attempts: list[_Attempt] = []
        generation: GenerationResult | None = None
        current_attempt = 1
        try:
            for attempt_number in range(1, MAX_ATTEMPTS + 1):
                current_attempt = attempt_number
                if generation is None:
                    self.reporter("Generating component...")
                    generation = self.generator.generate(
                        prompt=normalized_prompt,
                        style_guide=style_guide,
                        source_screenshot=source_screenshot,
                    )
                else:
                    self.reporter("Revising component from the first evaluation...")
                    previous = attempts[-1]
                    generation = self.generator.revise(
                        prompt=normalized_prompt,
                        style_guide=style_guide,
                        source_screenshot=source_screenshot,
                        component=previous.component,
                        evaluation=previous.evaluation,
                        render_screenshot=previous.outputs.get("screenshot.png"),
                    )
                files = component_files(generation.component)
                self.reporter(f"Rendering attempt {attempt_number} in the offline sandbox...")
                sandbox_result = self.sandbox_runner.run(
                    CreatorJobRequest(inputs=CREATOR_INPUTS, outputs=CREATOR_OUTPUTS),
                    files,
                )
                diagnostics = _sandbox_diagnostics(sandbox_result)
                gates_passed = all(hard_gates_from_diagnostics(diagnostics).values())
                if sandbox_result.succeeded and gates_passed:
                    self.reporter(f"Reviewing attempt {attempt_number} against the rubric...")
                    review_result = self.reviewer.review(
                        prompt=normalized_prompt,
                        style_guide=style_guide,
                        source_screenshot=source_screenshot,
                        component=generation.component,
                        render_diagnostics=diagnostics,
                        render_screenshot=sandbox_result.outputs["screenshot.png"],
                    )
                    evaluation = evaluate_attempt(attempt_number, diagnostics, review_result.review)
                    raw_review = review_result.raw_responses
                else:
                    evaluation = failed_hard_gate_evaluation(attempt_number, diagnostics)
                    raw_review = ()
                attempt = _Attempt(
                    number=attempt_number,
                    component=generation.component,
                    files=files,
                    outputs=sandbox_result.outputs,
                    diagnostics=diagnostics,
                    evaluation=evaluation,
                )
                attempts.append(attempt)
                self._persist_attempt(
                    run_id,
                    creation.creation_id,
                    attempt,
                    generation.raw_responses,
                    raw_review,
                    sandbox_result.logs,
                )
                if evaluation.passed:
                    break

            valid_attempts = [item for item in attempts if item.evaluation.hard_gates_passed]
            if not valid_attempts:
                raise RuntimeError("no creation attempt passed the render and baseline hard gates")
            selected = max(
                valid_attempts,
                key=lambda item: (item.evaluation.aggregate_score, -item.number),
            )
            final_artifacts = self._final_artifacts(
                run_id, creation.creation_id, normalized_prompt, selected
            )
            run = self.store.complete_creation(
                run_id,
                creation.creation_id,
                artifacts=final_artifacts,
                model_name=self.generator.model_name,
                prompt_version=CREATOR_PROMPT_VERSION,
            )
            destination = self._publish(creation.creation_id, final_artifacts)
            self.reporter(
                "Creation passed the rubric."
                if selected.evaluation.passed
                else "Creation finished with the best valid attempt and recorded limitations."
            )
            return CreatorResult(
                run=run,
                creation_id=creation.creation_id,
                component_directory=destination,
                evaluation=selected.evaluation,
            )
        except Exception as error:
            message = str(error) or error.__class__.__name__
            if isinstance(error, ModelResponseError):
                self._persist_failed_model_responses(
                    run_id,
                    creation.creation_id,
                    role=error.role,
                    responses=error.responses,
                    attempt=current_attempt,
                )
            self.store.mark_creation_failed(run_id, creation.creation_id, message)
            if isinstance(error, CreatorWorkflowError):
                raise
            raise CreatorWorkflowError(run_id, creation.creation_id, message) from error

    def _persist_attempt(
        self,
        run_id: str,
        creation_id: str,
        attempt: _Attempt,
        raw_generation: tuple[str, ...],
        raw_review: tuple[str, ...],
        sandbox_log: str,
    ) -> None:
        artifacts = {
            **attempt.files,
            **_raw_response_artifacts("generation", raw_generation),
            "render.json": _json_bytes(attempt.diagnostics),
            "evaluation.json": _json_bytes(attempt.evaluation.model_dump(mode="json")),
        }
        media_types = {
            "component.html": "text/html",
            "component.css": "text/css",
            "component.js": "text/javascript",
            **{
                name: "text/plain" for name in _raw_response_artifacts("generation", raw_generation)
            },
            "render.json": "application/json",
            "evaluation.json": "application/json",
        }
        if "screenshot.png" in attempt.outputs:
            artifacts["screenshot.png"] = attempt.outputs["screenshot.png"]
            media_types["screenshot.png"] = "image/png"
        artifacts.update(_raw_response_artifacts("review", raw_review))
        media_types.update(
            {name: "text/plain" for name in _raw_response_artifacts("review", raw_review)}
        )
        if sandbox_log:
            artifacts["sandbox.log"] = sandbox_log.encode()
            media_types["sandbox.log"] = "text/plain"
        self.store.persist_creation_attempt(
            run_id,
            creation_id,
            attempt=attempt.number,
            artifacts=artifacts,
            media_types=media_types,
        )

    def _persist_failed_model_responses(
        self,
        run_id: str,
        creation_id: str,
        *,
        role: str,
        responses: tuple[str, ...],
        attempt: int,
    ) -> None:
        artifacts = _raw_response_artifacts(role, responses)
        if not artifacts:
            return
        self.store.persist_creation_attempt(
            run_id,
            creation_id,
            attempt=attempt,
            artifacts=artifacts,
            media_types={name: "text/plain" for name in artifacts},
        )

    def _final_artifacts(
        self, run_id: str, creation_id: str, prompt: str, selected: _Attempt
    ) -> dict[str, bytes]:
        metadata = {
            "schema_version": 1,
            "run_id": run_id,
            "creation_id": creation_id,
            "prompt": prompt,
            "style_guide_path": "analysis/style-guide.json",
            "source_screenshot_path": "analysis/screenshot.png",
            "creator_model": self.generator.model_name,
            "reviewer_model": self.reviewer.model_name,
            "creator_prompt_version": CREATOR_PROMPT_VERSION,
            "reviewer_prompt_version": REVIEWER_PROMPT_VERSION,
            "rubric_version": RUBRIC_VERSION,
            "selected_attempt": selected.number,
            "rubric_passed": selected.evaluation.passed,
            "inferred_choices": selected.component.inferred_choices,
            "render_errors": selected.diagnostics.get("errors", []),
        }
        return {
            **selected.files,
            "metadata.json": _json_bytes(metadata),
            "screenshot.png": selected.outputs["screenshot.png"],
            "render.json": _json_bytes(selected.diagnostics),
            "evaluation.json": _json_bytes(selected.evaluation.model_dump(mode="json")),
        }

    def _publish(self, creation_id: str, artifacts: Mapping[str, bytes]) -> Path:
        self.components_directory.mkdir(parents=True, exist_ok=True)
        destination = self.components_directory / creation_id
        if destination.exists():
            raise ValueError(f"component output already exists: {destination}")
        with TemporaryDirectory(
            dir=self.components_directory, prefix=f".{creation_id}."
        ) as temporary:
            temporary_path = Path(temporary)
            for name, content in artifacts.items():
                (temporary_path / name).write_bytes(content)
            os.replace(temporary_path, destination)
        return destination


class OllamaComponentGenerator:
    """Generate strict component source documents with a multimodal Ollama client."""

    def __init__(
        self,
        client: Any,
        *,
        model_name: str,
        reporter: Callable[[str], None] = _discard_progress,
    ) -> None:
        self._client = client
        self.model_name = model_name
        self._reporter = reporter

    def generate(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
    ) -> GenerationResult:
        instruction = (
            "Create one plain HTML/CSS/JavaScript component for the request. Transfer the "
            "supplied site's design language; do not copy an existing component. Return exactly "
            "one JSON object matching the schema. `markup` must be an HTML fragment with exactly "
            "one outer element carrying `data-pg-component`. Do not include html, head, style, "
            "or script tags. Scope every CSS selector beneath `[data-pg-component]`. JavaScript "
            "runs as a deferred classic script after the markup exists; wrap it in an IIFE and "
            "do not create globals, or return an empty JavaScript string when no scripted "
            "behavior is required. Use semantic controls, accessible names, visible focus, "
            "keyboard behavior, and Escape dismissal where applicable. Use no external URLs, "
            "packages, fonts, images, network calls, or generated prose outside the component. "
            "Record visually inferred choices in `inferred_choices`.\n\n"
            f"Component request: {prompt}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}"
        )
        return self._chat(instruction, [source_screenshot])

    def revise(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        component: GeneratedComponent,
        evaluation: AttemptEvaluation,
        render_screenshot: bytes | None,
    ) -> GenerationResult:
        instruction = (
            "Revise the component once using the deterministic gates, rubric deltas, cited "
            "problems, and revision instructions. Preserve strengths and fix failures. Return "
            "exactly one JSON object matching the same schema and constraints.\n\n"
            f"Component request: {prompt}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}\n"
            f"Current component: {component.model_dump_json()}\n"
            f"Evaluation: {evaluation.model_dump_json()}"
        )
        images = [source_screenshot]
        if render_screenshot is not None:
            images.append(render_screenshot)
        return self._chat(instruction, images)

    def _chat(self, prompt: str, images: list[bytes]) -> GenerationResult:
        response = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt, "images": images}],
            format=GeneratedComponent.model_json_schema(),
            options={"temperature": 0},
        )
        content = response.message.content
        if not content:
            raise ValueError("creator model returned no content")
        try:
            component = _validate_generated_response(content)
            return GenerationResult(component=component, raw_responses=(content,))
        except ValueError as error:
            self._reporter("Creator response needs repair; sending validation errors to model...")
            repaired = self._repair(content, str(error))
            try:
                component = _validate_generated_response(repaired)
            except ValueError as repair_error:
                raise ModelResponseError(
                    str(repair_error),
                    role="generation",
                    responses=(content, repaired),
                ) from repair_error
            return GenerationResult(component=component, raw_responses=(content, repaired))

    def _repair(self, response: str, validation_error: str) -> str:
        prompt = (
            "Repair the creator response so it exactly matches the supplied JSON schema and "
            "component boundary rules. Return only the repaired JSON object. Preserve the "
            "component's supported design and behavior. `markup` must contain only the single "
            "data-pg-component HTML fragment. If markup contains `<style>` or `<script>`, move "
            "their contents into the separate `css` and `javascript` strings and remove those "
            "tags. Convert `inferred_choices` to an array of concise strings. Scope every CSS "
            "selector beneath `[data-pg-component]`. Keep `javascript` as an empty string when "
            "the component requires no scripted behavior. Do not add external resources.\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        repaired = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            format=GeneratedComponent.model_json_schema(),
            options={"temperature": 0},
        ).message.content
        if not repaired:
            raise ModelResponseError(
                "creator repair model returned no content",
                role="generation",
                responses=(response,),
            )
        return repaired


class OllamaComponentReviewer:
    """Score a rendered attempt in a fresh, rubric-only model context."""

    def __init__(
        self,
        client: Any,
        *,
        model_name: str,
        reporter: Callable[[str], None] = _discard_progress,
    ) -> None:
        self._client = client
        self.model_name = model_name
        self._reporter = reporter

    def review(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        component: GeneratedComponent,
        render_diagnostics: Mapping[str, Any],
        render_screenshot: bytes,
    ) -> ReviewResult:
        rubric = "\n".join(f"- {name}: {weight:.0%}" for name, weight in RUBRIC_WEIGHTS.items())
        instruction = (
            "Review the generated component independently. Compare its rendered screenshot and "
            "code with the source screenshot and evidence-backed style guide. Score every rubric "
            "dimension from 0 to 100. Cite concrete problems and give actionable revision "
            "instructions. Do not decide pass/fail or calculate the aggregate; application code "
            "owns routing. Return exactly one JSON object matching the supplied schema. The root "
            "keys must be design_language_adherence, contextual_appropriateness, "
            "interaction_and_state_quality, responsive_behavior, "
            "accessibility_beyond_baseline, implementation_quality, and concrete_problems. Each "
            "concrete problem must contain dimension, problem, and actionable_revision. Use an "
            "empty concrete_problems array when there are no deficiencies.\n\n"
            f"Request: {prompt}\nRubric:\n{rubric}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}\n"
            f"Component: {component.model_dump_json()}\n"
            f"Deterministic render diagnostics: {json.dumps(render_diagnostics, sort_keys=True)}"
        )
        response = self._client.chat(
            model=self.model_name,
            messages=[
                {
                    "role": "user",
                    "content": instruction,
                    "images": [source_screenshot, render_screenshot],
                }
            ],
            format=ReviewerResponse.model_json_schema(),
            options={"temperature": 0},
        )
        content = response.message.content
        if not content:
            raise ValueError("reviewer model returned no content")
        try:
            review = _review_from_response(
                _validate_model_json(content, ReviewerResponse, "reviewer")
            )
            return ReviewResult(review=review, raw_responses=(content,))
        except ValueError as error:
            self._reporter("Reviewer response needs repair; sending validation errors to model...")
            repaired = self._repair(content, str(error))
            try:
                review = _review_from_response(
                    _validate_model_json(repaired, ReviewerResponse, "reviewer")
                )
            except ValueError as repair_error:
                raise ModelResponseError(
                    str(repair_error),
                    role="review",
                    responses=(content, repaired),
                ) from repair_error
            return ReviewResult(review=review, raw_responses=(content, repaired))

    def _repair(self, response: str, validation_error: str) -> str:
        prompt = (
            "Repair the reviewer response so it exactly matches the supplied JSON schema. "
            "Return only the repaired JSON object. Preserve supported scores and feedback. "
            "The root must contain exactly the six rubric score fields plus concrete_problems. "
            "Every score must be an integer from 0 to 100. Every concrete problem must contain "
            "exactly dimension, problem, and actionable_revision. Do not calculate pass/fail, "
            "nest scores, or add fields outside the schema.\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        repaired = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            format=ReviewerResponse.model_json_schema(),
            options={"temperature": 0},
        ).message.content
        if not repaired:
            raise ModelResponseError(
                "reviewer repair model returned no content",
                role="review",
                responses=(response,),
            )
        return repaired


def component_files(component: GeneratedComponent) -> dict[str, bytes]:
    """Build three directly openable component files from model-owned fragments."""

    validate_generated_component(component)
    html = (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "  <title>Generated component</title>\n"
        '  <link rel="stylesheet" href="component.css">\n'
        '  <script src="component.js" defer></script>\n'
        "</head>\n"
        "<body>\n"
        f"{component.markup.strip()}\n"
        "</body>\n"
        "</html>\n"
    )
    return {
        "component.html": html.encode(),
        "component.css": (component.css.rstrip() + "\n").encode(),
        "component.js": (component.javascript.rstrip() + "\n").encode(),
    }


def validate_generated_component(component: GeneratedComponent) -> None:
    """Reject obvious package-boundary violations before executing generated code."""

    markup = component.markup.lower()
    if markup.count("data-pg-component") != 1:
        raise ValueError("component markup must contain exactly one data-pg-component root")
    if re.search(r"<\s*(?:html|head|body|style|script)\b", markup):
        raise ValueError("component markup must be a fragment without document or code tags")
    combined = f"{component.markup}\n{component.css}\n{component.javascript}"
    if re.search(r"(?:https?|wss?|ftp)://|@import\b", combined, flags=re.IGNORECASE):
        raise ValueError("generated components must not reference external resources")
    if "[data-pg-component" not in component.css:
        raise ValueError("component CSS must be scoped beneath data-pg-component")


def _validate_generated_response(content: str) -> GeneratedComponent:
    component = _validate_model_json(content, GeneratedComponent, "creator")
    validate_generated_component(component)
    return component


def _raw_response_artifacts(prefix: str, responses: tuple[str, ...]) -> dict[str, bytes]:
    names = (f"{prefix}.raw.txt", f"{prefix}.repair.raw.txt")
    return {name: response.encode() for name, response in zip(names, responses, strict=False)}


def _review_from_response(response: ReviewerResponse) -> RubricReview:
    """Convert the strict model-facing shape into trusted evaluation data."""

    scores = RubricScores.model_validate(response.model_dump(exclude={"concrete_problems"}))
    return RubricReview(
        scores=scores,
        cited_problems=[
            f"{problem.dimension}: {problem.problem}" for problem in response.concrete_problems
        ],
        revision_instructions=[
            problem.actionable_revision for problem in response.concrete_problems
        ],
    )


def evaluate_attempt(
    attempt: int, diagnostics: Mapping[str, Any], review: RubricReview
) -> AttemptEvaluation:
    """Compute deterministic gates, aggregate, protected floors, and pass/fail."""

    hard_gates = hard_gates_from_diagnostics(diagnostics)
    score_values = review.scores.model_dump()
    aggregate = round(
        sum(float(score_values[name]) * weight for name, weight in RUBRIC_WEIGHTS.items()), 2
    )
    protected_passed = all(
        score_values[name] >= PROTECTED_SCORE_FLOOR for name in PROTECTED_DIMENSIONS
    )
    hard_gates_passed = all(hard_gates.values())
    return AttemptEvaluation(
        attempt=attempt,
        hard_gates=hard_gates,
        hard_gates_passed=hard_gates_passed,
        scores=review.scores,
        aggregate_score=aggregate,
        protected_dimensions_passed=protected_passed,
        passed=hard_gates_passed and aggregate >= DELIVERY_THRESHOLD and protected_passed,
        cited_problems=review.cited_problems,
        revision_instructions=review.revision_instructions,
    )


def hard_gates_from_diagnostics(diagnostics: Mapping[str, Any]) -> dict[str, bool]:
    """Derive baseline gates only from sandbox-observed facts."""

    inspection = diagnostics.get("inspection", {})
    if not isinstance(inspection, Mapping):
        inspection = {}
    bounds = inspection.get("root_bounds", {})
    if not isinstance(bounds, Mapping):
        bounds = {}
    return {
        "execution": diagnostics.get("errors") == [],
        "security": diagnostics.get("blocked_requests") == [],
        "render": bool(inspection.get("root_found"))
        and float(bounds.get("width", 0)) > 0
        and float(bounds.get("height", 0)) > 0,
        "baseline_accessibility": inspection.get("unnamed_interactive_count") == 0,
    }


def failed_hard_gate_evaluation(attempt: int, diagnostics: Mapping[str, Any]) -> AttemptEvaluation:
    """Represent hard-gate failure without asking the quality reviewer to speculate."""

    zero_scores = RubricScores(
        design_language_adherence=0,
        contextual_appropriateness=0,
        interaction_and_state_quality=0,
        responsive_behavior=0,
        accessibility_beyond_baseline=0,
        implementation_quality=0,
    )
    failed_gates = [
        name for name, passed in hard_gates_from_diagnostics(diagnostics).items() if not passed
    ]
    message = str(
        diagnostics.get("sandbox_error")
        or f"hard gates failed: {', '.join(failed_gates) or 'unknown'}"
    )
    return AttemptEvaluation(
        attempt=attempt,
        hard_gates=hard_gates_from_diagnostics(diagnostics),
        hard_gates_passed=False,
        scores=zero_scores,
        aggregate_score=0,
        protected_dimensions_passed=False,
        passed=False,
        cited_problems=[message],
        revision_instructions=[
            "Fix every failed hard gate and preserve the offline package contract."
        ],
    )


def _sandbox_diagnostics(result: SandboxJobResult) -> dict[str, Any]:
    if not result.succeeded:
        return {
            "schema_version": 1,
            "errors": [result.error or "sandbox job failed"],
            "blocked_requests": [],
            "inspection": {},
            "sandbox_error": result.error,
        }
    return _json_object(result.outputs["render.json"], "render.json")


def _json_object(content: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"{name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise TypeError(f"{name} must contain a JSON object")
    return value


def _validate_model_json(content: str, model: type[BaseModel], role: str) -> Any:
    stripped = content.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise ValueError(f"{role} response has an incomplete Markdown fence")
        stripped = "\n".join(lines[1:-1]).strip()
    try:
        return model.model_validate_json(stripped)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False)[:5]
        )
        raise ValueError(f"{role} response does not match its schema: {details}") from error


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
