"""Bounded host-side Storybook generation, review, and publication."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from playgrounds.runs import RunRecord, RunStore
from playgrounds.sandbox import CreatorJobRequest, SandboxArtifact, SandboxJobResult

CREATOR_PROMPT_VERSION = "storybook-creator-v4"
REVIEWER_PROMPT_VERSION = "storybook-reviewer-v1"
RUBRIC_VERSION = "storybook-rubric-v1"
CREATOR_SYSTEM_PROMPT = (
    "You are a bounded Storybook generator. Return only the strict JSON requested. "
    "Choose the smallest coherent implementation that satisfies the request. Prefer complete, "
    "high-quality essentials over breadth. Do not add optional components, stories, variants, "
    "states, documentation, examples, abstractions, or explanations. Never think aloud."
)
MAX_ATTEMPTS = 2
CREATOR_HEARTBEAT_SECONDS = 15.0
MAX_PARALLEL_COMPONENT_CALLS = 4
MAX_COMPONENT_GENERATION_ATTEMPTS = 2
MAX_PLANNING_OUTPUT_TOKENS = 2_048
DELIVERY_THRESHOLD = 80.0
PROTECTED_SCORE_FLOOR = 65
PROTECTED_DIMENSIONS = (
    "design_language_adherence",
    "interaction_and_state_quality",
    "system_coherence",
)
RUBRIC_WEIGHTS = {
    "design_language_adherence": 0.25,
    "contextual_appropriateness": 0.15,
    "interaction_and_state_quality": 0.15,
    "responsive_behavior": 0.10,
    "accessibility_beyond_baseline": 0.10,
    "system_coherence": 0.10,
    "story_coverage_and_documentation": 0.10,
    "implementation_quality": 0.05,
}
CREATOR_INPUTS = (
    SandboxArtifact(path="project.json", media_type="application/json", max_bytes=2_000_000),
)
CREATOR_OUTPUTS = (
    SandboxArtifact(path="screenshot.png", media_type="image/png"),
    SandboxArtifact(path="render.json", media_type="application/json"),
    SandboxArtifact(path="storybook.zip", media_type="application/zip", max_bytes=50_000_000),
)
MAX_GENERATED_FILES = 100
MAX_GENERATED_FILE_BYTES = 200_000
MAX_GENERATED_SOURCE_BYTES = 2_000_000
MAX_PUBLISHED_FILES = 2_000
MAX_PUBLISHED_BYTES = 100_000_000
ALLOWED_BARE_IMPORTS = {
    "react",
    "react-dom",
    "react/jsx-runtime",
    "lucide-react",
    "@storybook/react-vite",
    "storybook/test",
}
GENERATED_PATH_PATTERN = re.compile(
    r"^src/(?:tokens/[A-Za-z][A-Za-z0-9_-]*\.(?:css|json)|"
    r"components/[A-Z][A-Za-z0-9]*/[A-Z][A-Za-z0-9]*\.(?:tsx|css)|"
    r"components/[A-Z][A-Za-z0-9]*/[A-Z][A-Za-z0-9]*\.stories\.tsx)$"
)
IMPORT_PATTERN = re.compile(
    r"(?:from\s+|import\s*)[\"']([^\"']+)[\"']|import\s*\(\s*[\"']([^\"']+)[\"']"
)
GENERATED_STORYBOOK_EXAMPLE = {
    "schema_version": 1,
    "plan": {
        "title": "Example controls",
        "summary": "A small component system for the requested interface.",
        "components": [
            {
                "name": "Button",
                "purpose": "Trigger an action.",
                "dependencies": [],
                "props": ["label: string", "disabled?: boolean", "onClick?: () => void"],
                "variants": ["primary"],
                "states": ["default", "disabled"],
            }
        ],
        "stories": [
            {
                "component": "Button",
                "name": "Default",
                "description": "Enabled primary action.",
                "viewport": "desktop",
            },
            {
                "component": "Button",
                "name": "Disabled",
                "description": "Unavailable action.",
                "viewport": "mobile",
            },
        ],
    },
    "files": [
        {
            "path": "src/tokens/tokens.css",
            "content": ":root { --example-action: #000; }",
        },
        {
            "path": "src/components/Button/Button.tsx",
            "content": (
                'import "../../tokens/tokens.css";\n'
                'import "./Button.css";\n'
                "export interface ButtonProps { onClick?: () => void }\n"
                "export function Button({ onClick }: ButtonProps) {\n"
                '  return <button type="button" onClick={onClick}>Example</button>;\n'
                "}"
            ),
        },
        {
            "path": "src/components/Button/Button.css",
            "content": ".example-button { color: var(--example-action); }",
        },
        {
            "path": "src/components/Button/Button.stories.tsx",
            "content": (
                'import type { Meta, StoryObj } from "@storybook/react-vite";\n'
                'import { fn } from "storybook/test";\n'
                'import { Button } from "./Button";\n'
                "const meta = { component: Button, args: { onClick: fn() } } "
                "satisfies Meta<typeof Button>;\n"
                "export default meta;\n"
                "type Story = StoryObj<typeof meta>;\n"
                "export const Default: Story = {};\n"
                "export const Disabled: Story = {};"
            ),
        },
    ],
    "inferred_choices": [],
}
STORYBOOK_PLAN_EXAMPLE = {
    "schema_version": GENERATED_STORYBOOK_EXAMPLE["schema_version"],
    "plan": GENERATED_STORYBOOK_EXAMPLE["plan"],
}
STORYBOOK_TOKENS_EXAMPLE = {
    "schema_version": GENERATED_STORYBOOK_EXAMPLE["schema_version"],
    "content": GENERATED_STORYBOOK_EXAMPLE["files"][0]["content"],
    "inferred_choices": GENERATED_STORYBOOK_EXAMPLE["inferred_choices"],
}
STORYBOOK_COMPONENT_EXAMPLE = {
    "schema_version": GENERATED_STORYBOOK_EXAMPLE["schema_version"],
    "files": GENERATED_STORYBOOK_EXAMPLE["files"][1:],
    "inferred_choices": GENERATED_STORYBOOK_EXAMPLE["inferred_choices"],
}
InferenceText = Annotated[str, Field(min_length=1, max_length=500)]
FeedbackText = Annotated[str, Field(min_length=1, max_length=1_000)]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
ComponentName = Annotated[str, Field(pattern=r"^[A-Z][A-Za-z0-9]{0,63}$")]
StoryName = Annotated[str, Field(pattern=r"^[A-Z][A-Za-z0-9]{0,63}$")]


class PlannedComponent(BaseModel):
    """One component family the generated Storybook must implement."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: ComponentName = Field(description="PascalCase component and directory name.")
    purpose: ShortText = Field(description="The component's role in the requested interface.")
    dependencies: list[ComponentName] = Field(
        max_length=0,
        description="Always empty because every generated component is isolated.",
    )
    props: list[ShortText] = Field(
        max_length=20,
        description="Public typed data props and user-event callbacks exposed by the component.",
    )
    variants: list[ShortText] = Field(
        max_length=20,
        description="Meaningful semantic or visual variants to implement.",
    )
    states: list[ShortText] = Field(
        max_length=20,
        description="Meaningful interaction, content, or status states to demonstrate.",
    )


class PlannedStory(BaseModel):
    """One concrete Storybook story and its inspection viewport."""

    model_config = ConfigDict(extra="forbid", strict=True)

    component: ComponentName = Field(description="Planned component rendered by this story.")
    name: StoryName = Field(description="Named CSF export in the component's story file.")
    description: ShortText = Field(description="The state or behavior this story demonstrates.")
    viewport: Literal["desktop", "mobile"] = Field(
        description="Viewport used for deterministic sandbox rendering."
    )


class StorybookPlan(BaseModel):
    """The package-level plan that keeps multi-file generation coherent."""

    model_config = ConfigDict(extra="forbid", strict=True)

    title: ShortText = Field(description="Human-readable generated design-system title.")
    summary: str = Field(
        min_length=1,
        max_length=1_000,
        description="How the planned system answers the user request and style guide.",
    )
    components: list[PlannedComponent] = Field(
        min_length=1,
        max_length=6,
        description="Component families in dependency order.",
    )
    stories: list[PlannedStory] = Field(
        min_length=1,
        max_length=30,
        description="All required renderable stories across the component families.",
    )

    @model_validator(mode="after")
    def validate_references(self) -> StorybookPlan:
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("planned component names must be unique")
        available: set[str] = set()
        for component in self.components:
            if len(component.dependencies) != len(set(component.dependencies)):
                raise ValueError(f"{component.name} dependencies must be unique")
            if component.dependencies:
                raise ValueError(
                    f"{component.name} must be isolated and cannot depend on other components"
                )
            missing = set(component.dependencies) - available
            if missing:
                raise ValueError(
                    f"{component.name} dependencies must precede it: {', '.join(sorted(missing))}"
                )
            available.add(component.name)
        story_keys = [(story.component, story.name) for story in self.stories]
        if len(story_keys) != len(set(story_keys)):
            raise ValueError("planned component story names must be unique")
        unknown = {story.component for story in self.stories} - set(names)
        if unknown:
            raise ValueError(f"stories reference unknown components: {', '.join(sorted(unknown))}")
        components_with_stories = {story.component for story in self.stories}
        missing_stories = set(names) - components_with_stories
        if missing_stories:
            raise ValueError(
                f"every component requires a story: {', '.join(sorted(missing_stories))}"
            )
        return self


class StorybookPlanResponse(BaseModel):
    """The strict output of the creator planning phase."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(description="Storybook planning schema version.")
    plan: StorybookPlan = Field(description="Validated component system and story coverage plan.")


class GeneratedFile(BaseModel):
    """One model-owned source file inside the trusted Storybook template."""

    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(
        min_length=1,
        max_length=200,
        description="Allowlisted POSIX path beneath src/tokens or src/components.",
    )
    content: str = Field(
        min_length=1,
        max_length=MAX_GENERATED_FILE_BYTES,
        description="Complete UTF-8 source content for this file.",
    )


class StorybookTokensResponse(BaseModel):
    """The strict output of the shared-token generation phase."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(description="Storybook token schema version.")
    content: str = Field(
        min_length=1,
        max_length=MAX_GENERATED_FILE_BYTES,
        description="Complete CSS custom-property definitions for src/tokens/tokens.css.",
    )
    inferred_choices: list[InferenceText] = Field(
        max_length=50,
        description="Token decisions not directly supported by analyzer evidence.",
    )


class StorybookComponentResponse(BaseModel):
    """The strict output of one isolated component-generation phase."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(description="Storybook component schema version.")
    files: list[GeneratedFile] = Field(
        min_length=3,
        max_length=3,
        description="Exactly the component TSX, CSS, and CSF story files.",
    )
    inferred_choices: list[InferenceText] = Field(
        max_length=50,
        description=(
            "Design choices not directly supported by analyzer evidence; use an empty array "
            "when every choice is evidence-backed."
        ),
    )


class GeneratedStorybook(BaseModel):
    """A planned, bounded set of model-owned React, CSS, and CSF source files."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = Field(description="Generated Storybook schema version.")
    plan: StorybookPlan = Field(description="Component system and story coverage plan.")
    files: list[GeneratedFile] = Field(
        min_length=1,
        max_length=MAX_GENERATED_FILES,
        description="Complete generated source files; configuration is host-owned.",
    )
    inferred_choices: list[InferenceText] = Field(
        max_length=50,
        description=(
            "Design choices not directly supported by analyzer evidence; use an empty array "
            "when every choice is evidence-backed."
        ),
    )


class RubricScores(BaseModel):
    """Reviewer-owned Storybook subscores; application code owns routing."""

    model_config = ConfigDict(extra="forbid", strict=True)

    design_language_adherence: int = Field(
        ge=0,
        le=100,
        description="System-wide fit with the analyzed site's visual and behavioral language.",
    )
    contextual_appropriateness: int = Field(
        ge=0,
        le=100,
        description="Suitability of hierarchy, APIs, and behavior for the requested product use.",
    )
    interaction_and_state_quality: int = Field(
        ge=0,
        le=100,
        description="Completeness and quality of pointer, keyboard, focus, and component states.",
    )
    responsive_behavior: int = Field(
        ge=0,
        le=100,
        description="Usability and coherence across the declared desktop and mobile stories.",
    )
    accessibility_beyond_baseline: int = Field(
        ge=0,
        le=100,
        description="Accessibility quality beyond deterministic rendered-DOM hard gates.",
    )
    system_coherence: int = Field(
        ge=0,
        le=100,
        description="Consistency of tokens, APIs, composition, variants, and states.",
    )
    story_coverage_and_documentation: int = Field(
        ge=0,
        le=100,
        description="Usefulness and completeness of stories, Controls, names, and descriptions.",
    )
    implementation_quality: int = Field(
        ge=0,
        le=100,
        description="Type safety, maintainability, isolation, and offline self-containment.",
    )


ReviewDimension = Literal[
    "design_language_adherence",
    "contextual_appropriateness",
    "interaction_and_state_quality",
    "responsive_behavior",
    "accessibility_beyond_baseline",
    "system_coherence",
    "story_coverage_and_documentation",
    "implementation_quality",
]


class ConcreteProblem(BaseModel):
    """One reviewer finding tied to a fixed package rubric dimension."""

    model_config = ConfigDict(extra="forbid", strict=True)

    dimension: ReviewDimension = Field(description="The directly affected rubric dimension.")
    problem: FeedbackText = Field(
        description="Concrete deficiency cited from source, evidence, diagnostics, or screenshot."
    )
    actionable_revision: FeedbackText = Field(
        description="Concrete project-file change that directly addresses the problem."
    )


class ReviewerResponse(RubricScores):
    """The strict flat response shape received from the reviewer model."""

    concrete_problems: list[ConcreteProblem] = Field(
        max_length=30,
        description="Structured deficiencies and revisions; use an empty array when none exist.",
    )


class RubricReview(BaseModel):
    """Trusted-host review data used for deterministic scoring and revision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    scores: RubricScores
    cited_problems: list[FeedbackText] = Field(max_length=30)
    revision_instructions: list[FeedbackText] = Field(max_length=30)


class AttemptEvaluation(BaseModel):
    """Deterministic hard gates, package scores, and revision feedback."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
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
    storybook: GeneratedStorybook
    plan_raw_responses: tuple[str, ...]
    token_raw_responses: tuple[str, ...]
    component_raw_responses: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ReviewResult:
    review: RubricReview
    raw_responses: tuple[str, ...]


@dataclass(frozen=True)
class CreatorResult:
    """The completed run record and published generated Storybook."""

    run: RunRecord
    creation_id: str
    storybook_directory: Path
    evaluation: AttemptEvaluation


class StorybookGenerator(Protocol):
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
        storybook: GeneratedStorybook,
        evaluation: AttemptEvaluation,
        render_screenshot: bytes | None,
    ) -> GenerationResult: ...


class StorybookReviewer(Protocol):
    model_name: str

    def review(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        storybook: GeneratedStorybook,
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


def _format_bytes(value: int) -> str:
    if value < 1_024:
        return f"{value} B"
    if value < 1_048_576:
        return f"{value / 1_024:.1f} KB"
    return f"{value / 1_048_576:.1f} MB"


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remaining = divmod(round(seconds), 60)
    return f"{minutes}m {remaining:02d}s"


def _model_timing_summary(phase: str, wall_seconds: float, response: Any) -> str:
    details: list[str] = []
    total_duration = getattr(response, "total_duration", None)
    if isinstance(total_duration, int):
        details.append(f"server {_format_duration(total_duration / 1_000_000_000)}")
    prompt_count = getattr(response, "prompt_eval_count", None)
    prompt_duration = getattr(response, "prompt_eval_duration", None)
    if isinstance(prompt_count, int):
        prompt_detail = f"prompt {prompt_count:,} tokens"
        if isinstance(prompt_duration, int):
            prompt_detail += f" in {_format_duration(prompt_duration / 1_000_000_000)}"
        details.append(prompt_detail)
    output_count = getattr(response, "eval_count", None)
    output_duration = getattr(response, "eval_duration", None)
    if isinstance(output_count, int):
        output_detail = f"output {output_count:,} tokens"
        if isinstance(output_duration, int):
            output_seconds = output_duration / 1_000_000_000
            output_detail += f" in {_format_duration(output_seconds)}"
            if output_seconds > 0:
                output_detail += f" ({output_count / output_seconds:.1f} tokens/s)"
        details.append(output_detail)
    timing = "; ".join(details) if details else "Ollama server timing unavailable"
    return f"{phase} completed in {_format_duration(wall_seconds)} wall-clock ({timing})."


@dataclass(frozen=True)
class _Attempt:
    number: int
    storybook: GeneratedStorybook
    files: dict[str, bytes]
    outputs: dict[str, bytes]
    diagnostics: dict[str, Any]
    evaluation: AttemptEvaluation


@dataclass(frozen=True)
class CreatorWorkflow:
    """Generate, build, review, revise once, and publish the best Storybook."""

    store: RunStore
    sandbox_runner: CreatorSandboxRunner
    generator: StorybookGenerator
    reviewer: StorybookReviewer
    storybooks_directory: Path = Path("storybooks")
    reporter: Callable[[str], None] = _discard_progress

    def create(self, run_id: str, prompt: str) -> CreatorResult:
        """Create one generated Storybook from a completed analyzer run."""

        normalized_prompt = prompt.strip()
        if not normalized_prompt:
            raise ValueError("Storybook prompt must not be empty")
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
                    generation = self.generator.generate(
                        prompt=normalized_prompt,
                        style_guide=style_guide,
                        source_screenshot=source_screenshot,
                    )
                else:
                    previous = attempts[-1]
                    try:
                        generation = self.generator.revise(
                            prompt=normalized_prompt,
                            style_guide=style_guide,
                            source_screenshot=source_screenshot,
                            storybook=previous.storybook,
                            evaluation=previous.evaluation,
                            render_screenshot=previous.outputs.get("screenshot.png"),
                        )
                    except Exception as error:
                        if not previous.evaluation.hard_gates_passed:
                            raise
                        message = str(error) or error.__class__.__name__
                        role = error.role if isinstance(error, ModelResponseError) else "revision"
                        responses = error.responses if isinstance(error, ModelResponseError) else ()
                        self._persist_failed_model_responses(
                            run_id,
                            creation.creation_id,
                            role=role,
                            responses=responses,
                            attempt=attempt_number,
                            validation_error=message,
                        )
                        self.reporter(
                            "Revision generation failed; publishing the valid first attempt "
                            f"instead: {message}"
                        )
                        break
                files = storybook_files(generation.storybook)
                self.reporter(f"Building attempt {attempt_number} in the offline sandbox...")
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
                        storybook=generation.storybook,
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
                    storybook=generation.storybook,
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
                    generation.plan_raw_responses,
                    generation.token_raw_responses,
                    generation.component_raw_responses,
                    raw_review,
                    sandbox_result.logs,
                )
                if evaluation.passed:
                    break

            valid_attempts = [item for item in attempts if item.evaluation.hard_gates_passed]
            if not valid_attempts:
                raise RuntimeError("no creation attempt passed the Storybook hard gates")
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
            destination = self._publish(creation.creation_id, selected.storybook, final_artifacts)
            self.reporter(
                "Storybook passed the rubric."
                if selected.evaluation.passed
                else "Storybook finished with the best valid attempt and recorded limitations."
            )
            return CreatorResult(
                run=run,
                creation_id=creation.creation_id,
                storybook_directory=destination,
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
                    validation_error=message,
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
        raw_plan: tuple[str, ...],
        raw_tokens: tuple[str, ...],
        raw_components: Mapping[str, tuple[str, ...]],
        raw_review: tuple[str, ...],
        sandbox_log: str,
    ) -> None:
        raw_plan_artifacts = _raw_response_artifacts("plan", raw_plan)
        raw_token_artifacts = _raw_response_artifacts("tokens", raw_tokens)
        raw_component_artifacts = {
            artifact_name: artifact
            for component_name, responses in raw_components.items()
            for artifact_name, artifact in _raw_response_artifacts(
                f"component-{component_name}", responses
            ).items()
        }
        raw_review_artifacts = _raw_response_artifacts("review", raw_review)
        artifacts = {
            **attempt.files,
            **raw_plan_artifacts,
            **raw_token_artifacts,
            **raw_component_artifacts,
            "render.json": _json_bytes(attempt.diagnostics),
            "evaluation.json": _json_bytes(attempt.evaluation.model_dump(mode="json")),
        }
        media_types = {
            "project.json": "application/json",
            **{name: "text/plain" for name in raw_plan_artifacts},
            **{name: "text/plain" for name in raw_token_artifacts},
            **{name: "text/plain" for name in raw_component_artifacts},
            "render.json": "application/json",
            "evaluation.json": "application/json",
        }
        for name, media_type in (
            ("screenshot.png", "image/png"),
            ("storybook.zip", "application/zip"),
        ):
            if name in attempt.outputs:
                artifacts[name] = attempt.outputs[name]
                media_types[name] = media_type
        artifacts.update(raw_review_artifacts)
        media_types.update({name: "text/plain" for name in raw_review_artifacts})
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
        validation_error: str,
    ) -> None:
        artifacts = _raw_response_artifacts(role, responses)
        artifacts[f"{role}.validation.txt"] = validation_error.encode()
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
            "schema_version": 2,
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
            "title": selected.storybook.plan.title,
            "component_count": len(selected.storybook.plan.components),
            "story_count": len(selected.storybook.plan.stories),
            "inferred_choices": selected.storybook.inferred_choices,
            "render_errors": selected.diagnostics.get("errors", []),
        }
        return {
            **selected.files,
            "metadata.json": _json_bytes(metadata),
            "screenshot.png": selected.outputs["screenshot.png"],
            "storybook.zip": selected.outputs["storybook.zip"],
            "render.json": _json_bytes(selected.diagnostics),
            "evaluation.json": _json_bytes(selected.evaluation.model_dump(mode="json")),
        }

    def _publish(
        self,
        creation_id: str,
        storybook: GeneratedStorybook,
        artifacts: Mapping[str, bytes],
    ) -> Path:
        self.storybooks_directory.mkdir(parents=True, exist_ok=True)
        destination = self.storybooks_directory / creation_id
        if destination.exists():
            raise ValueError(f"Storybook output already exists: {destination}")
        with TemporaryDirectory(
            dir=self.storybooks_directory, prefix=f".{creation_id}."
        ) as temporary:
            temporary_path = Path(temporary)
            for name, content in artifacts.items():
                (temporary_path / name).write_bytes(content)
            source_directory = temporary_path / "project"
            for generated_file in storybook.files:
                path = source_directory / generated_file.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(generated_file.content, encoding="utf-8")
            _extract_storybook(artifacts["storybook.zip"], temporary_path / "storybook-static")
            os.replace(temporary_path, destination)
        return destination


class OllamaStorybookGenerator:
    """Plan first, then generate bounded React source against the validated plan."""

    def __init__(
        self,
        client: Any,
        *,
        model_name: str,
        planning_client: Any | None = None,
        reporter: Callable[[str], None] = _discard_progress,
        heartbeat_seconds: float = CREATOR_HEARTBEAT_SECONDS,
        use_structured_outputs: bool = False,
        max_components: int = 4,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("creator heartbeat interval must be positive")
        if not 1 <= max_components <= 6:
            raise ValueError("creator component limit must be between 1 and 6")
        self._client = client
        self._planning_client = planning_client or client
        self.model_name = model_name
        self._reporter = reporter
        self._heartbeat_seconds = heartbeat_seconds
        self._use_structured_outputs = use_structured_outputs
        self._max_components = max_components

    def _chat_with_progress(
        self,
        phase: str,
        *,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any],
        max_output_tokens: int | None = None,
        client: Any | None = None,
    ) -> Any:
        request_messages = [
            {"role": "system", "content": CREATOR_SYSTEM_PROMPT},
            *messages,
        ]
        prompt_bytes = sum(
            len(str(message.get("content", "")).encode()) for message in request_messages
        )
        images = [
            image
            for message in request_messages
            for image in message.get("images", [])
            if isinstance(image, bytes)
        ]
        image_bytes = sum(len(image) for image in images)
        image_summary = f", {len(images)} image(s) / {_format_bytes(image_bytes)}" if images else ""
        self._reporter(
            f"{phase} request sent to {self.model_name} "
            f"(prompt {_format_bytes(prompt_bytes)}{image_summary})."
        )
        started = time.monotonic()
        finished = threading.Event()

        def report_heartbeat() -> None:
            while not finished.wait(self._heartbeat_seconds):
                elapsed = time.monotonic() - started
                self._reporter(
                    f"{phase} is still waiting for the model... "
                    f"{_format_duration(elapsed)} elapsed."
                )

        heartbeat = threading.Thread(
            target=report_heartbeat,
            name=f"creator-{phase.lower().replace(' ', '-')}-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            options: dict[str, int | float] = {"temperature": 0}
            if max_output_tokens is not None:
                options["num_predict"] = max_output_tokens
            response = (client or self._client).chat(
                model=self.model_name,
                messages=request_messages,
                format=response_format if self._use_structured_outputs else None,
                think=False,
                options=options,
            )
        except Exception:
            elapsed = time.monotonic() - started
            self._reporter(f"{phase} failed after {_format_duration(elapsed)} wall-clock.")
            raise
        finally:
            finished.set()
            heartbeat.join()
        elapsed = time.monotonic() - started
        self._reporter(_model_timing_summary(phase, elapsed, response))
        return response

    def generate(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
    ) -> GenerationResult:
        self._reporter("Planning Storybook components and stories...")
        try:
            plan, plan_responses = self._plan(
                prompt=prompt,
                style_guide=style_guide,
                images=[source_screenshot],
            )
        except Exception as error:  # noqa: BLE001 - planning has a safe host fallback
            detail = str(error) or error.__class__.__name__
            self._reporter(
                f"Planning failed; continuing with the minimal host fallback plan: {detail}"
            )
            plan = _fallback_storybook_plan(prompt)
            plan_responses = error.responses if isinstance(error, ModelResponseError) else ()
        storybook, token_responses, component_responses = self._generate_project(
            prompt=prompt,
            style_guide=style_guide,
            plan=plan,
        )
        return GenerationResult(
            storybook=storybook,
            plan_raw_responses=plan_responses,
            token_raw_responses=token_responses,
            component_raw_responses=component_responses,
        )

    def revise(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        storybook: GeneratedStorybook,
        evaluation: AttemptEvaluation,
        render_screenshot: bytes | None,
    ) -> GenerationResult:
        if not evaluation.hard_gates_passed:
            affected_components = _components_cited_by_evaluation(
                evaluation,
                storybook.plan,
            )
            scope = (
                ", ".join(sorted(affected_components))
                if affected_components
                else "all components"
            )
            self._reporter(
                "Repairing deterministic sandbox failures without replanning "
                f"({scope})..."
            )
            revised, token_responses, component_responses = self._generate_project(
                prompt=prompt,
                style_guide=style_guide,
                plan=storybook.plan,
                current_storybook=storybook,
                evaluation=evaluation,
                regenerate_tokens=False,
                component_names=affected_components or None,
            )
            return GenerationResult(
                storybook=revised,
                plan_raw_responses=(),
                token_raw_responses=token_responses,
                component_raw_responses=component_responses,
            )
        self._reporter("Planning the Storybook revision from evaluation feedback...")
        try:
            plan, plan_responses = self._plan(
                prompt=prompt,
                style_guide=style_guide,
                images=[],
                revision_context=(
                    f"Current plan: {storybook.plan.model_dump_json()}\n"
                    f"Evaluation: {evaluation.model_dump_json()}"
                ),
            )
        except Exception as error:  # noqa: BLE001 - preserve the validated current plan
            detail = str(error) or error.__class__.__name__
            self._reporter(
                f"Revision planning failed; preserving the current validated plan: {detail}"
            )
            plan = storybook.plan
            plan_responses = error.responses if isinstance(error, ModelResponseError) else ()
        revised, token_responses, component_responses = self._generate_project(
            prompt=prompt,
            style_guide=style_guide,
            plan=plan,
            current_storybook=storybook,
            evaluation=evaluation,
        )
        return GenerationResult(
            storybook=revised,
            plan_raw_responses=plan_responses,
            token_raw_responses=token_responses,
            component_raw_responses=component_responses,
        )

    def _plan(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        images: list[bytes],
        revision_context: str | None = None,
    ) -> tuple[StorybookPlan, tuple[str, ...]]:
        canonical_example = json.dumps(STORYBOOK_PLAN_EXAMPLE, indent=2)
        instruction = (
            "Plan the smallest sufficient React TypeScript design system for Storybook. Transfer "
            "the supplied "
            "site's design language rather than copying an observed component. Do not generate "
            "source files in this phase.\n\n"
            "OUTPUT CONTRACT — follow this literally:\n"
            "- Return one JSON object. A surrounding ```json fence is tolerated but unnecessary.\n"
            '- `schema_version` is the JSON NUMBER 1. Never use `"1"` or `"1.0.0"`.\n'
            "- `plan` contains exactly `title`, `summary`, `components`, and `stories`.\n"
            "- Every plan component contains exactly `name`, `purpose`, `dependencies`, `props`, "
            "`variants`, and `states`. Each of the last four fields is an array; use [] when "
            "empty. `dependencies` must always be []. Never put `files` inside a component.\n"
            "- Every plan story contains exactly `component`, `name`, `description`, and "
            "`viewport`. Viewport is exactly `desktop` or `mobile`.\n"
            f"- Generate at most {self._max_components} isolated PascalCase component(s). Choose "
            "the fewest that fully satisfy the request; add another only for a distinct reusable "
            "behavior.\n"
            "- Prefer one story per component. Add a second only for a materially distinct state "
            "required by the request. Never exceed eight stories total.\n"
            "- Exclude decorative wrappers, headers, status indicators, generic buttons, and "
            "helper components when they can remain private markup inside a core component.\n"
            "- Declare every CSF story. Components share tokens but never import one another.\n"
            "- Props may include typed data and callbacks for user events.\n"
            "CANONICAL SHAPE EXAMPLE — copy its structure, not its example names or styling:\n"
            f"{canonical_example}\n\n"
            f"Storybook request: {prompt}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}"
        )
        if revision_context is not None:
            instruction += (
                "\n\nRevise the plan to fix the evaluation problems while preserving its "
                f"strengths.\n{revision_context}"
            )
        message: dict[str, Any] = {"role": "user", "content": instruction}
        if images:
            message["images"] = images
        response = self._chat_with_progress(
            "Planning",
            messages=[message],
            response_format=StorybookPlanResponse.model_json_schema(),
            max_output_tokens=MAX_PLANNING_OUTPUT_TOKENS,
            client=self._planning_client,
        )
        content = response.message.content
        if not content:
            raise ModelResponseError(
                "creator planning model returned no content",
                role="plan",
                responses=(),
            )
        try:
            planned = self._validate_plan(content)
            return planned.plan, (content,)
        except ValueError as error:
            self._reporter("Creator plan did not match StorybookPlan; repairing once...")
            self._reporter(str(error))
            repaired = self._repair_plan(content, str(error))
            try:
                planned = self._validate_plan(repaired)
            except ValueError as repair_error:
                raise ModelResponseError(
                    str(repair_error),
                    role="plan",
                    responses=(content, repaired),
                ) from repair_error
            return planned.plan, (content, repaired)

    def _repair_plan(self, response: str, validation_error: str) -> str:
        canonical_example = json.dumps(STORYBOOK_PLAN_EXAMPLE, indent=2)
        prompt = (
            "Rebuild the planning response to match the canonical shape. Return only the "
            "complete repaired JSON object. Do not generate source files.\n\n"
            "REQUIRED CORRECTIONS:\n"
            "- schema_version is the number 1, not a string or semantic version.\n"
            "- plan has title, summary, components, and stories.\n"
            "- every component has name, purpose, dependencies, props, variants, and states; "
            "dependencies is always [] and components never have a files field.\n"
            f"- plan contains at most {self._max_components} components.\n"
            "- every story has component, name, description, and desktop or mobile viewport.\n\n"
            "CANONICAL SHAPE EXAMPLE — copy its structure, not its names or styling:\n"
            f"{canonical_example}\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        repaired = self._chat_with_progress(
            "Planning repair",
            messages=[{"role": "user", "content": prompt}],
            response_format=StorybookPlanResponse.model_json_schema(),
            max_output_tokens=MAX_PLANNING_OUTPUT_TOKENS,
            client=self._planning_client,
        ).message.content
        if not repaired:
            raise ModelResponseError(
                "creator plan repair model returned no content",
                role="plan",
                responses=(response,),
            )
        return repaired

    def _validate_plan(self, response: str) -> StorybookPlanResponse:
        planned = _validate_model_json(response, StorybookPlanResponse, "creator plan")
        component_count = len(planned.plan.components)
        if component_count > self._max_components:
            raise ValueError(
                "creator plan contains "
                f"{component_count} components; configured maximum is {self._max_components}"
            )
        return planned

    def _generate_project(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        plan: StorybookPlan,
        current_storybook: GeneratedStorybook | None = None,
        evaluation: AttemptEvaluation | None = None,
        regenerate_tokens: bool = True,
        component_names: set[str] | None = None,
    ) -> tuple[
        GeneratedStorybook,
        tuple[str, ...],
        Mapping[str, tuple[str, ...]],
    ]:
        current_token_file = None
        if current_storybook is not None:
            current_token_file = next(
                (
                    item
                    for item in current_storybook.files
                    if item.path == "src/tokens/tokens.css"
                ),
                None,
            )
        if regenerate_tokens:
            self._reporter("Generating shared Storybook design tokens...")
            token_file, token_choices, token_responses = self._generate_tokens(
                prompt=prompt,
                style_guide=style_guide,
                plan=plan,
                current_tokens=(
                    current_token_file.content if current_token_file is not None else None
                ),
                evaluation=evaluation,
            )
        else:
            if current_token_file is None:
                raise ValueError("cannot repair components without existing shared tokens")
            self._reporter("Reusing the validated shared Storybook tokens...")
            token_file = current_token_file
            token_choices = []
            token_responses = ()
        selected_components = [
            component
            for component in plan.components
            if component_names is None or component.name in component_names
        ]
        worker_count = min(MAX_PARALLEL_COMPONENT_CALLS, len(selected_components))
        self._reporter(
            f"Generating {len(selected_components)} isolated component(s) in parallel "
            f"(up to {worker_count} at once)..."
        )
        current_files = (
            {item.path: item.content for item in current_storybook.files}
            if current_storybook is not None
            else {}
        )
        component_results: dict[
            str,
            tuple[list[GeneratedFile], list[str], tuple[str, ...]],
        ] = {}
        for component in plan.components:
            if component in selected_components:
                continue
            directory = f"src/components/{component.name}/"
            existing_files = [
                GeneratedFile(path=path, content=content)
                for path, content in current_files.items()
                if path.startswith(directory)
            ]
            if len(existing_files) != 3:
                raise ValueError(
                    f"cannot preserve {component.name}; its previous files are incomplete"
                )
            component_results[component.name] = (existing_files, [], ())
        component_failures: dict[str, Exception] = {}
        pending_components = selected_components
        for generation_attempt in range(1, MAX_COMPONENT_GENERATION_ATTEMPTS + 1):
            if not pending_components:
                break
            if generation_attempt > 1:
                names = ", ".join(component.name for component in pending_components)
                self._reporter(f"Retrying only failed component generation: {names}.")
            retry_worker_count = min(MAX_PARALLEL_COMPONENT_CALLS, len(pending_components))
            with ThreadPoolExecutor(
                max_workers=retry_worker_count,
                thread_name_prefix="storybook-component",
            ) as executor:
                futures = {
                    component.name: executor.submit(
                        self._generate_component,
                        prompt=prompt,
                        style_guide=style_guide,
                        plan=plan,
                        component=component,
                        token_file=token_file,
                        current_files=current_files,
                        evaluation=evaluation,
                    )
                    for component in pending_components
                }
                failed_this_attempt: list[PlannedComponent] = []
                for component in pending_components:
                    try:
                        component_results[component.name] = futures[component.name].result()
                        component_failures.pop(component.name, None)
                    except Exception as error:  # noqa: BLE001 - isolate one component boundary
                        component_failures[component.name] = error
                        failed_this_attempt.append(component)
                        detail = str(error) or error.__class__.__name__
                        self._reporter(
                            f"Component {component.name} generation attempt "
                            f"{generation_attempt} failed: {detail}"
                        )
            pending_components = failed_this_attempt

        effective_plan = plan
        unresolved_components: list[PlannedComponent] = []
        for component in pending_components:
            directory = f"src/components/{component.name}/"
            previous_files = [
                GeneratedFile(path=path, content=content)
                for path, content in current_files.items()
                if path.startswith(directory)
            ]
            if len(previous_files) == 3:
                error = component_failures[component.name]
                raw_responses = error.responses if isinstance(error, ModelResponseError) else ()
                component_results[component.name] = (previous_files, [], raw_responses)
                self._reporter(
                    f"Component {component.name} still failed; reusing its previous valid files."
                )
            else:
                unresolved_components.append(component)

        if unresolved_components:
            successful_names = set(component_results)
            if not successful_names:
                failures = "; ".join(
                    f"{component.name}: "
                    f"{str(component_failures[component.name]) or component_failures[component.name].__class__.__name__}"
                    for component in unresolved_components
                )
                raise RuntimeError(
                    "all component generations failed after "
                    f"{MAX_COMPONENT_GENERATION_ATTEMPTS} attempts: {failures}"
                )
            omitted_names = {component.name for component in unresolved_components}
            effective_plan = StorybookPlan(
                title=plan.title,
                summary=plan.summary,
                components=[
                    component for component in plan.components if component.name not in omitted_names
                ],
                stories=[
                    story for story in plan.stories if story.component not in omitted_names
                ],
            )
            self._reporter(
                "Continuing without component(s) that failed twice: "
                f"{', '.join(sorted(omitted_names))}."
            )
        files = [token_file]
        inferred_choices = list(token_choices)
        component_responses: dict[str, tuple[str, ...]] = {}
        for component in effective_plan.components:
            component_files, component_choices, raw_responses = component_results[component.name]
            files.extend(component_files)
            inferred_choices.extend(component_choices)
            component_responses[component.name] = raw_responses
        omitted_names = {
            component.name for component in plan.components
        } - {component.name for component in effective_plan.components}
        if omitted_names:
            inferred_choices.append(
                "Omitted components after two failed generation attempts: "
                f"{', '.join(sorted(omitted_names))}."
            )
        storybook = GeneratedStorybook(
            schema_version=1,
            plan=effective_plan,
            files=files,
            inferred_choices=list(dict.fromkeys(inferred_choices))[:50],
        )
        validate_generated_storybook(storybook)
        return storybook, token_responses, component_responses

    def _generate_tokens(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        plan: StorybookPlan,
        current_tokens: str | None,
        evaluation: AttemptEvaluation | None,
    ) -> tuple[GeneratedFile, list[str], tuple[str, ...]]:
        canonical_example = json.dumps(STORYBOOK_TOKENS_EXAMPLE, indent=2)
        instruction = (
            "Generate the single shared CSS token file for a Storybook component system. "
            "Return values only; the trusted host owns the file path and all Storybook "
            "configuration.\n\n"
            "OUTPUT CONTRACT:\n"
            "- Return exactly schema_version, content, and inferred_choices.\n"
            "- schema_version is the JSON number 1.\n"
            "- content is plain CSS beginning with :root and defines reusable custom properties "
            "for color, typography, spacing, radii, borders, shadows, motion, and focus.\n"
            "- Define only tokens needed by the supplied plan; avoid aliases and speculative "
            "scales.\n"
            "- inferred_choices is an array of strings.\n"
            "- Use no @import, url(), external resources, or selectors other than :root.\n\n"
            "CANONICAL SHAPE EXAMPLE — copy its structure, not its values:\n"
            f"{canonical_example}\n\n"
            f"Storybook request: {prompt}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}\n"
            f"Plan title and summary: {plan.title} — {plan.summary}"
        )
        if current_tokens is not None and evaluation is not None:
            instruction += (
                "\n\nRevise the current tokens using the evaluation while preserving supported "
                f"strengths.\nCurrent tokens: {current_tokens}\n"
                f"Evaluation: {evaluation.model_dump_json()}"
            )
        response = self._chat_with_progress(
            "Token generation",
            messages=[{"role": "user", "content": instruction}],
            response_format=StorybookTokensResponse.model_json_schema(),
        )
        content = response.message.content
        if not content:
            raise ModelResponseError(
                "creator token model returned no content",
                role="tokens",
                responses=(),
            )
        try:
            token_file, choices = _tokens_from_response(content)
            return token_file, choices, (content,)
        except ValueError as error:
            self._reporter("Creator tokens did not match the token contract; repairing once...")
            self._reporter(str(error))
            repaired = self._repair_tokens(content, str(error))
            try:
                token_file, choices = _tokens_from_response(repaired)
            except ValueError as repair_error:
                raise ModelResponseError(
                    str(repair_error),
                    role="tokens",
                    responses=(content, repaired),
                ) from repair_error
            return token_file, choices, (content, repaired)

    def _repair_tokens(self, response: str, validation_error: str) -> str:
        canonical_example = json.dumps(STORYBOOK_TOKENS_EXAMPLE, indent=2)
        prompt = (
            "Repair the shared token response. Return exactly schema_version, content, and "
            "inferred_choices. content must be plain :root CSS custom properties with no "
            "@import, url(), or external resource.\n\n"
            f"CANONICAL SHAPE EXAMPLE:\n{canonical_example}\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        repaired = self._chat_with_progress(
            "Token repair",
            messages=[{"role": "user", "content": prompt}],
            response_format=StorybookTokensResponse.model_json_schema(),
        ).message.content
        if not repaired:
            raise ModelResponseError(
                "creator token repair model returned no content",
                role="tokens",
                responses=(response,),
            )
        return repaired

    def _generate_component(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        plan: StorybookPlan,
        component: PlannedComponent,
        token_file: GeneratedFile,
        current_files: Mapping[str, str],
        evaluation: AttemptEvaluation | None,
    ) -> tuple[list[GeneratedFile], list[str], tuple[str, ...]]:
        stories = [story for story in plan.stories if story.component == component.name]
        canonical_example = json.dumps(STORYBOOK_COMPONENT_EXAMPLE, indent=2)
        instruction = (
            f"Implement only the isolated {component.name} Storybook component. Do not implement "
            "or import any other planned component. The trusted host supplies Storybook, React, "
            "Vite, TypeScript, dependencies, configuration, and shared tokens.\n\n"
            "OUTPUT CONTRACT:\n"
            "- Return exactly schema_version, files, and inferred_choices.\n"
            "- schema_version is the JSON number 1.\n"
            f"- files contains exactly these three paths: "
            f"src/components/{component.name}/{component.name}.tsx, "
            f"src/components/{component.name}/{component.name}.css, and "
            f"src/components/{component.name}/{component.name}.stories.tsx.\n"
            "- inferred_choices is an array of strings.\n"
            "- Component.tsx imports ../../tokens/tokens.css and its own CSS.\n"
            "- Never import another src/components directory.\n"
            "- Use typed props, semantic HTML, visible focus, keyboard behavior, and meaningful "
            "states.\n"
            "- Implement only props, variants, and states named in the plan. Keep markup and CSS "
            "small; avoid optional helpers, abstractions, comments, and speculative features.\n"
            "- Export exactly and only the CSF stories named in the supplied component story "
            "plan. Do not add convenience, state, or documentation stories.\n"
            '- Import Meta and StoryObj from "@storybook/react-vite". Import `fn` from exactly '
            '"storybook/test", never "@storybook/test".\n'
            "- Use only react, react-dom, lucide-react, @storybook/react-vite, and storybook/test. "
            "Use no Tailwind, external resources, network APIs, environment access, dynamic "
            "imports, Node built-ins, or configuration files.\n\n"
            "CANONICAL SHAPE EXAMPLE — copy its structure, replacing Button with the requested "
            f"component:\n{canonical_example}\n\n"
            f"Storybook request: {prompt}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}\n"
            f"Shared tokens: {token_file.content}\n"
            f"Component plan: {component.model_dump_json()}\n"
            f"Component stories: {json.dumps([story.model_dump(mode='json') for story in stories])}"
        )
        directory = f"src/components/{component.name}/"
        previous = {
            path: content for path, content in current_files.items() if path.startswith(directory)
        }
        if previous and evaluation is not None:
            instruction += (
                "\n\nRevise this component using the package evaluation while preserving its "
                f"valid strengths.\nCurrent component files: {json.dumps(previous)}\n"
                f"Evaluation: {evaluation.model_dump_json()}"
            )
        phase = f"Component {component.name}"
        response = self._chat_with_progress(
            phase,
            messages=[{"role": "user", "content": instruction}],
            response_format=StorybookComponentResponse.model_json_schema(),
        )
        content = response.message.content
        role = f"component-{component.name}"
        if not content:
            raise ModelResponseError(
                f"creator model returned no content for {component.name}",
                role=role,
                responses=(),
            )
        try:
            files, choices = _component_from_response(content, plan, component, token_file)
            return files, choices, (content,)
        except ValueError as error:
            self._reporter(
                f"Creator component {component.name} did not match its contract; repairing once..."
            )
            self._reporter(str(error))
            repaired = self._repair_component(
                content,
                str(error),
                component=component,
                stories=stories,
                token_file=token_file,
            )
            try:
                files, choices = _component_from_response(repaired, plan, component, token_file)
            except ValueError as repair_error:
                raise ModelResponseError(
                    str(repair_error),
                    role=role,
                    responses=(content, repaired),
                ) from repair_error
            return files, choices, (content, repaired)

    def _repair_component(
        self,
        response: str,
        validation_error: str,
        *,
        component: PlannedComponent,
        stories: list[PlannedStory],
        token_file: GeneratedFile,
    ) -> str:
        canonical_example = json.dumps(STORYBOOK_COMPONENT_EXAMPLE, indent=2)
        prompt = (
            f"Repair only the isolated {component.name} component response. Return exactly "
            "schema_version, files, and inferred_choices. Keep exactly its three complete paths, "
            "never import another component, import shared tokens from ../../tokens/tokens.css, "
            "and export exactly and only the supplied stories. Delete every extra story export. "
            "Use storybook/test, never @storybook/test.\n\n"
            f"CANONICAL SHAPE EXAMPLE:\n{canonical_example}\n\n"
            f"Component plan: {component.model_dump_json()}\n"
            f"Component stories: {json.dumps([story.model_dump(mode='json') for story in stories])}\n"
            f"Shared tokens: {token_file.content}\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        phase = f"Component {component.name} repair"
        repaired = self._chat_with_progress(
            phase,
            messages=[{"role": "user", "content": prompt}],
            response_format=StorybookComponentResponse.model_json_schema(),
        ).message.content
        if not repaired:
            raise ModelResponseError(
                f"creator repair model returned no content for {component.name}",
                role=f"component-{component.name}",
                responses=(response,),
            )
        return repaired


class OllamaStorybookReviewer:
    """Score a generated Storybook in a fresh package-rubric context."""

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

    def review(
        self,
        *,
        prompt: str,
        style_guide: Mapping[str, Any],
        source_screenshot: bytes,
        storybook: GeneratedStorybook,
        render_diagnostics: Mapping[str, Any],
        render_screenshot: bytes,
    ) -> ReviewResult:
        rubric = "\n".join(f"- {name}: {weight:.0%}" for name, weight in RUBRIC_WEIGHTS.items())
        score_fields = ", ".join(RUBRIC_WEIGHTS)
        instruction = (
            "Review the generated Storybook independently as both a rendered interface and a "
            "small design system. Compare its representative screenshot, source, plan, and "
            "per-story diagnostics with the source screenshot and evidence-backed style guide. "
            "Score every rubric dimension from 0 to 100. Evaluate visual transfer, component "
            "APIs, token reuse, cross-component coherence, story/state coverage, responsive "
            "behavior, accessibility, and implementation. Cite concrete file- or story-level "
            "problems and actionable revisions. Do not calculate the aggregate or decide "
            "pass/fail. Return exactly one flat JSON object with these score fields plus "
            f"concrete_problems: {score_fields}. Each problem contains dimension, problem, and "
            "actionable_revision. Use an empty array when there are no deficiencies.\n\n"
            f"Request: {prompt}\nRubric:\n{rubric}\n"
            f"Style guide: {json.dumps(style_guide, sort_keys=True)}\n"
            f"Generated Storybook: {storybook.model_dump_json()}\n"
            f"Deterministic diagnostics: {json.dumps(render_diagnostics, sort_keys=True)}"
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
            format=(ReviewerResponse.model_json_schema() if self._use_structured_outputs else None),
            think=False,
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
            "Return only the repaired JSON object. Preserve supported scores and feedback. The "
            "root must contain exactly all eight integer rubric fields plus concrete_problems. "
            "Every problem contains exactly dimension, problem, and actionable_revision. Do not "
            "calculate pass/fail, nest scores, or add fields.\n\n"
            f"Validation errors:\n{validation_error}\n\n"
            f"Previous response:\n{response}"
        )
        repaired = self._client.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}],
            format=(ReviewerResponse.model_json_schema() if self._use_structured_outputs else None),
            think=False,
            options={"temperature": 0},
        ).message.content
        if not repaired:
            raise ModelResponseError(
                "reviewer repair model returned no content",
                role="review",
                responses=(response,),
            )
        return repaired


def storybook_files(storybook: GeneratedStorybook) -> dict[str, bytes]:
    """Serialize the validated generated project for the fixed sandbox boundary."""

    validate_generated_storybook(storybook)
    return {"project.json": _json_bytes(storybook.model_dump(mode="json"))}


def validate_generated_storybook(storybook: GeneratedStorybook) -> None:
    """Reject project, path, source, story, and dependency boundary violations."""

    paths = [generated_file.path for generated_file in storybook.files]
    if len(paths) != len(set(paths)):
        raise ValueError("generated Storybook file paths must be unique")
    if "src/tokens/tokens.css" not in paths:
        raise ValueError("generated Storybook must contain src/tokens/tokens.css")
    total_bytes = sum(len(generated_file.content.encode()) for generated_file in storybook.files)
    if total_bytes > MAX_GENERATED_SOURCE_BYTES:
        raise ValueError("generated Storybook exceeds the total source-size limit")
    files_by_path = {generated_file.path: generated_file for generated_file in storybook.files}
    for generated_file in storybook.files:
        path = PurePosixPath(generated_file.path)
        if (
            path.as_posix() != generated_file.path
            or path.is_absolute()
            or ".." in path.parts
            or not GENERATED_PATH_PATTERN.fullmatch(generated_file.path)
        ):
            raise ValueError(f"generated Storybook path is not allowed: {generated_file.path}")
        if len(generated_file.content.encode()) > MAX_GENERATED_FILE_BYTES:
            raise ValueError(f"generated Storybook file is too large: {generated_file.path}")
        _validate_source(generated_file, files_by_path)
    expected_component_paths: set[str] = set()
    for component in storybook.plan.components:
        directory = f"src/components/{component.name}"
        expected = {
            f"{directory}/{component.name}.tsx",
            f"{directory}/{component.name}.css",
            f"{directory}/{component.name}.stories.tsx",
        }
        expected_component_paths.update(expected)
        if not expected <= set(paths):
            missing = ", ".join(sorted(expected - set(paths)))
            raise ValueError(f"{component.name} is missing required files: {missing}")
    actual_component_paths = {path for path in paths if path.startswith("src/components/")}
    if actual_component_paths != expected_component_paths:
        raise ValueError("each planned component must contain exactly its three source files")
    planned_components = {component.name for component in storybook.plan.components}
    file_components = {
        PurePosixPath(path).parts[2] for path in paths if path.startswith("src/components/")
    }
    if file_components != planned_components:
        raise ValueError("generated component directories must exactly match the plan")
    for component in storybook.plan.components:
        story_path = f"src/components/{component.name}/{component.name}.stories.tsx"
        content = files_by_path[story_path].content
        expected_stories = {
            story.name for story in storybook.plan.stories if story.component == component.name
        }
        exported_stories = set(
            re.findall(r"\bexport\s+const\s+([A-Z][A-Za-z0-9]{0,63})\b", content)
        )
        missing_stories = expected_stories - exported_stories
        if missing_stories:
            missing = ", ".join(sorted(missing_stories))
            raise ValueError(f"planned story is not exported: {component.name}/{missing}")
        extra_stories = exported_stories - expected_stories
        if extra_stories:
            extra = ", ".join(sorted(extra_stories))
            raise ValueError(f"unplanned story is exported: {component.name}/{extra}")


def _validate_source(
    generated_file: GeneratedFile, files_by_path: Mapping[str, GeneratedFile]
) -> None:
    content = generated_file.content
    if re.search(r"(?:https?|wss?|ftp)://|@import\b", content, flags=re.IGNORECASE):
        raise ValueError(f"generated source references an external resource: {generated_file.path}")
    if generated_file.path.endswith(".tsx") and re.search(
        r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(|"
        r"\b(?:process\.env|import\.meta\.env)\b|"
        r"\bimport\s*\(",
        content,
    ):
        raise ValueError(f"generated source uses a forbidden runtime API: {generated_file.path}")
    if generated_file.path.endswith(".css") and re.search(r"\burl\s*\(", content, re.IGNORECASE):
        raise ValueError(f"generated CSS uses a forbidden asset URL: {generated_file.path}")
    for match in IMPORT_PATTERN.finditer(content):
        imported = match.group(1) or match.group(2)
        if imported in ALLOWED_BARE_IMPORTS:
            continue
        if not imported.startswith("."):
            correction = '; use "storybook/test" instead' if imported == "@storybook/test" else ""
            raise ValueError(
                f"generated source imports an unsupported package in {generated_file.path}: "
                f"{imported}{correction}"
            )
        if not _relative_import_is_inside_source(generated_file.path, imported, files_by_path):
            raise ValueError(
                f"generated source import escapes or is missing in {generated_file.path}: "
                f"{imported}"
            )


def _relative_import_is_inside_source(
    source_path: str, imported: str, files_by_path: Mapping[str, GeneratedFile]
) -> bool:
    parts = list(PurePosixPath(source_path).parent.parts)
    for part in PurePosixPath(imported).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
        else:
            parts.append(part)
    candidate = PurePosixPath(*parts).as_posix()
    if not candidate.startswith("src/"):
        return False
    source_parts = PurePosixPath(source_path).parts
    candidate_parts = PurePosixPath(candidate).parts
    if (
        len(source_parts) > 2
        and len(candidate_parts) > 2
        and source_parts[1] == "components"
        and candidate_parts[1] == "components"
        and source_parts[2] != candidate_parts[2]
    ):
        return False
    candidates = {
        candidate,
        f"{candidate}.tsx",
        f"{candidate}.css",
        f"{candidate}.json",
        f"{candidate}/index.tsx",
    }
    return any(path in files_by_path for path in candidates)


def _normalize_generated_files(files: list[GeneratedFile]) -> list[GeneratedFile]:
    return [
        generated_file.model_copy(
            update={
                "content": generated_file.content.replace(
                    '"@storybook/test"', '"storybook/test"'
                ).replace("'@storybook/test'", "'storybook/test'")
            }
        )
        for generated_file in files
    ]


def _tokens_from_response(content: str) -> tuple[GeneratedFile, list[str]]:
    response = _validate_model_json(content, StorybookTokensResponse, "creator tokens")
    token_file = GeneratedFile(path="src/tokens/tokens.css", content=response.content)
    without_comments = re.sub(r"/\*.*?\*/", "", token_file.content, flags=re.DOTALL)
    selectors = [value.strip() for value in re.findall(r"([^{}]+)\{", without_comments)]
    if not selectors or any(selector != ":root" for selector in selectors):
        raise ValueError("shared token CSS must contain only a :root rule")
    _validate_source(token_file, {token_file.path: token_file})
    return token_file, response.inferred_choices


def _component_from_response(
    content: str,
    plan: StorybookPlan,
    component: PlannedComponent,
    token_file: GeneratedFile,
) -> tuple[list[GeneratedFile], list[str]]:
    response = _validate_model_json(
        content,
        StorybookComponentResponse,
        f"creator component {component.name}",
    )
    files = _normalize_generated_files(response.files)
    component_plan = StorybookPlan(
        title=plan.title,
        summary=plan.summary,
        components=[component],
        stories=[story for story in plan.stories if story.component == component.name],
    )
    isolated = GeneratedStorybook(
        schema_version=response.schema_version,
        plan=component_plan,
        files=[token_file, *files],
        inferred_choices=response.inferred_choices,
    )
    validate_generated_storybook(isolated)
    return files, response.inferred_choices


def _raw_response_artifacts(prefix: str, responses: tuple[str, ...]) -> dict[str, bytes]:
    names = (f"{prefix}.raw.txt", f"{prefix}.repair.raw.txt")
    return {name: response.encode() for name, response in zip(names, responses, strict=False)}


def _fallback_storybook_plan(prompt: str) -> StorybookPlan:
    """Return a minimal host-owned plan when model planning is unavailable."""

    request = " ".join(prompt.split())[:500]
    return StorybookPlan(
        title="Requested interface",
        summary=f"Minimal fallback implementation for the request: {request}",
        components=[
            PlannedComponent(
                name="RequestedInterface",
                purpose="Implement the requested interface as one cohesive component.",
                dependencies=[],
                props=[],
                variants=["default"],
                states=["default"],
            )
        ],
        stories=[
            PlannedStory(
                component="RequestedInterface",
                name="Default",
                description="The smallest complete rendering of the requested interface.",
                viewport="desktop",
            )
        ],
    )


def _components_cited_by_evaluation(
    evaluation: AttemptEvaluation,
    plan: StorybookPlan,
) -> set[str]:
    """Resolve generated component paths cited by deterministic sandbox feedback."""

    feedback = "\n".join(
        [*evaluation.cited_problems, *evaluation.revision_instructions]
    )
    cited = set(re.findall(r"\bsrc/components/([A-Z][A-Za-z0-9]{0,63})/", feedback))
    planned = {component.name for component in plan.components}
    return cited & planned


def _review_from_response(response: ReviewerResponse) -> RubricReview:
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
    """Compute deterministic project gates, weighted score, and pass/fail."""

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
    """Derive project and per-story gates only from sandbox-observed facts."""

    build = diagnostics.get("build", {})
    if not isinstance(build, Mapping):
        build = {}
    stories_value = diagnostics.get("stories", [])
    stories = stories_value if isinstance(stories_value, list) else []
    story_mappings = [story for story in stories if isinstance(story, Mapping)]
    render_passed = bool(story_mappings)
    accessibility_passed = bool(story_mappings)
    story_execution_passed = bool(story_mappings)
    story_security_passed = True
    for story in story_mappings:
        inspection = story.get("inspection", {})
        if not isinstance(inspection, Mapping):
            inspection = {}
        bounds = inspection.get("root_bounds", {})
        if not isinstance(bounds, Mapping):
            bounds = {}
        render_passed = render_passed and bool(inspection.get("root_found"))
        render_passed = (
            render_passed
            and float(bounds.get("width", 0)) > 0
            and float(bounds.get("height", 0)) > 0
        )
        accessibility_passed = (
            accessibility_passed and inspection.get("unnamed_interactive_count") == 0
        )
        story_execution_passed = story_execution_passed and story.get("errors") == []
        story_security_passed = story_security_passed and story.get("blocked_requests") == []
    story_count = build.get("story_count")
    expected_story_count = build.get("expected_story_count")
    return {
        "typecheck": build.get("typecheck_succeeded") is True,
        "storybook_build": build.get("storybook_succeeded") is True,
        "story_coverage": isinstance(story_count, int)
        and isinstance(expected_story_count, int)
        and story_count > 0
        and story_count == expected_story_count,
        "execution": diagnostics.get("errors") == [] and story_execution_passed,
        "security": diagnostics.get("blocked_requests") == [] and story_security_passed,
        "render": render_passed,
        "baseline_accessibility": accessibility_passed,
    }


def failed_hard_gate_evaluation(attempt: int, diagnostics: Mapping[str, Any]) -> AttemptEvaluation:
    """Represent hard-gate failure without asking the reviewer to speculate."""

    zero_scores = RubricScores(**{name: 0 for name in RUBRIC_WEIGHTS})
    hard_gates = hard_gates_from_diagnostics(diagnostics)
    failed_gates = [name for name, passed in hard_gates.items() if not passed]
    errors = diagnostics.get("errors")
    error_detail = "; ".join(str(error) for error in errors[:3]) if isinstance(errors, list) else ""
    message = str(
        diagnostics.get("sandbox_error")
        or error_detail
        or f"hard gates failed: {', '.join(failed_gates) or 'unknown'}"
    )
    return AttemptEvaluation(
        attempt=attempt,
        hard_gates=hard_gates,
        hard_gates_passed=False,
        scores=zero_scores,
        aggregate_score=0,
        protected_dimensions_passed=False,
        passed=False,
        cited_problems=[message],
        revision_instructions=[
            "Fix every failed Storybook hard gate and preserve the offline project contract."
        ],
    )


def _sandbox_diagnostics(result: SandboxJobResult) -> dict[str, Any]:
    if not result.succeeded:
        return {
            "schema_version": 2,
            "build": {},
            "errors": [result.error or "sandbox job failed"],
            "blocked_requests": [],
            "stories": [],
            "sandbox_error": result.error,
            "sandbox_log": result.logs,
        }
    return _json_object(result.outputs["render.json"], "render.json")


def _extract_storybook(content: bytes, destination: Path) -> None:
    """Extract only a bounded regular-file static Storybook archive."""

    destination.mkdir(parents=True)
    seen: set[str] = set()
    total_bytes = 0
    with zipfile.ZipFile(_bytes_reader(content)) as archive:
        members = archive.infolist()
        if len(members) > MAX_PUBLISHED_FILES:
            raise ValueError("static Storybook archive contains too many files")
        for member in members:
            path = PurePosixPath(member.filename)
            if (
                member.is_dir()
                or path.is_absolute()
                or ".." in path.parts
                or member.filename in {"", "."}
                or member.filename in seen
            ):
                raise ValueError("static Storybook archive contains an unsafe path")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError("static Storybook archive must not contain links")
            seen.add(member.filename)
            total_bytes += member.file_size
            if total_bytes > MAX_PUBLISHED_BYTES:
                raise ValueError("static Storybook archive expands beyond its size limit")
        for member in members:
            target = destination / PurePosixPath(member.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                while chunk := source.read(64 * 1024):
                    output.write(chunk)


def _bytes_reader(content: bytes) -> Any:
    import io

    return io.BytesIO(content)


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
        details = "\n".join(
            f"- {'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_url=False)[:30]
        )
        raise ValueError(f"{role} response does not match its schema:\n{details}") from error


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, indent=2, sort_keys=True).encode() + b"\n"
