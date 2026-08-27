import io
import json
import threading
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from playgrounds.creator_workflow import (
    ConcreteProblem,
    CreatorWorkflow,
    CreatorWorkflowError,
    GeneratedFile,
    GeneratedStorybook,
    GenerationResult,
    ModelResponseError,
    OllamaStorybookGenerator,
    OllamaStorybookReviewer,
    PlannedComponent,
    PlannedStory,
    ReviewerResponse,
    ReviewResult,
    RubricReview,
    RubricScores,
    StorybookComponentResponse,
    StorybookPlan,
    StorybookPlanResponse,
    StorybookTokensResponse,
    evaluate_attempt,
    failed_hard_gate_evaluation,
    hard_gates_from_diagnostics,
    storybook_files,
    validate_generated_storybook,
)
from playgrounds.runs import CreationStatus, RunStore
from playgrounds.sandbox import SandboxJobResult

FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"


def completed_run(store: RunStore) -> str:
    run = store.create_run("https://www.mitravasu.com/")
    store.persist_analysis_evidence(
        run.run_id,
        {
            "page.json": b'{"title":"Example"}',
            "observations.json": b'{"observations":[]}',
            "screenshot.png": b"source-png",
        },
    )
    store.persist_style_guide(
        run.run_id,
        b'{"schema_version":1,"colors":{"action":"#f60"}}',
        model_name="analyzer",
        prompt_version="v1",
    )
    return run.run_id


def generated(label: str = "Continue") -> GeneratedStorybook:
    return GeneratedStorybook(
        schema_version=1,
        plan=StorybookPlan(
            title="Account controls",
            summary="A small account-oriented component system.",
            components=[
                PlannedComponent(
                    name="Button",
                    purpose="Trigger an account action.",
                    dependencies=[],
                    props=["label", "disabled"],
                    variants=["primary"],
                    states=["default", "disabled"],
                )
            ],
            stories=[
                PlannedStory(
                    component="Button",
                    name="Default",
                    description="Enabled primary action.",
                    viewport="desktop",
                ),
                PlannedStory(
                    component="Button",
                    name="Disabled",
                    description="Unavailable action.",
                    viewport="mobile",
                ),
            ],
        ),
        files=[
            GeneratedFile(
                path="src/tokens/tokens.css",
                content=":root { --pg-action: #f60; --pg-on-action: #fff; }\n",
            ),
            GeneratedFile(
                path="src/components/Button/Button.tsx",
                content=(
                    'import "../../tokens/tokens.css";\n'
                    'import "./Button.css";\n'
                    "export interface ButtonProps { label: string; disabled?: boolean }\n"
                    "export function Button({ label, disabled = false }: ButtonProps) {\n"
                    '  return <button className="pg-button" disabled={disabled}>{label}</button>;\n'
                    "}\n"
                ),
            ),
            GeneratedFile(
                path="src/components/Button/Button.css",
                content=(
                    ".pg-button { background: var(--pg-action); color: var(--pg-on-action); }\n"
                    ".pg-button:focus-visible { outline: 2px solid currentColor; }\n"
                ),
            ),
            GeneratedFile(
                path="src/components/Button/Button.stories.tsx",
                content=(
                    'import type { Meta, StoryObj } from "@storybook/react-vite";\n'
                    'import { Button } from "./Button";\n'
                    "const meta = { component: Button, args: { label: "
                    f'"{label}"'
                    " } } satisfies Meta<typeof Button>;\n"
                    "export default meta;\n"
                    "type Story = StoryObj<typeof meta>;\n"
                    "export const Default: Story = {};\n"
                    "export const Disabled: Story = { args: { disabled: true } };\n"
                ),
            ),
        ],
        inferred_choices=["Used one primary action variant for the bounded request."],
    )


def static_storybook_zip() -> bytes:
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", "<!doctype html><title>Storybook</title>")
        archive.writestr("index.json", '{"entries":{}}')
    return content.getvalue()


def diagnostics(*, render: bool = True) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "build": {
            "typecheck_succeeded": True,
            "storybook_succeeded": True,
            "expected_story_count": 2,
            "story_count": 2,
        },
        "errors": [],
        "blocked_requests": [],
        "stories": [
            {
                "id": "button--default",
                "title": "Button",
                "name": "Default",
                "errors": [],
                "blocked_requests": [],
                "inspection": {
                    "root_found": render,
                    "root_bounds": {"width": 200 if render else 0, "height": 40 if render else 0},
                    "interactive_count": 1,
                    "unnamed_interactive_count": 0,
                },
            },
            {
                "id": "button--disabled",
                "title": "Button",
                "name": "Disabled",
                "errors": [],
                "blocked_requests": [],
                "inspection": {
                    "root_found": render,
                    "root_bounds": {"width": 200 if render else 0, "height": 40 if render else 0},
                    "interactive_count": 1,
                    "unnamed_interactive_count": 0,
                },
            },
        ],
    }


class FakeGenerator:
    model_name = "creator-test"

    def __init__(self) -> None:
        self.generate_kwargs: dict[str, Any] = {}
        self.revise_kwargs: dict[str, Any] = {}

    def generate(self, **kwargs: Any) -> GenerationResult:
        self.generate_kwargs = kwargs
        return GenerationResult(
            storybook=generated("First"),
            plan_raw_responses=('{"plan":"first"}',),
            token_raw_responses=('{"tokens":"first"}',),
            component_raw_responses={"Button": ('{"component":"first"}',)},
        )

    def revise(self, **kwargs: Any) -> GenerationResult:
        self.revise_kwargs = kwargs
        return GenerationResult(
            storybook=generated("Revised"),
            plan_raw_responses=('{"plan":"revised"}',),
            token_raw_responses=('{"tokens":"revised"}',),
            component_raw_responses={"Button": ('{"component":"revised"}',)},
        )


class InvalidGenerator(FakeGenerator):
    def generate(self, **kwargs: Any) -> GenerationResult:
        raise ModelResponseError(
            "creator response still does not match its schema",
            role="component-Button",
            responses=('{"files":"first"}', '{"files":"repair"}'),
        )


class RevisionFailingGenerator(FakeGenerator):
    def revise(self, **kwargs: Any) -> GenerationResult:
        self.revise_kwargs = kwargs
        raise TimeoutError("revision timed out")


class FakeSandbox:
    def __init__(self, *, render: bool = True) -> None:
        self.render = render
        self.calls: list[tuple[object, Mapping[str, bytes]]] = []

    def run(self, request: object, input_files: Mapping[str, bytes]) -> SandboxJobResult:
        self.calls.append((request, input_files))
        return SandboxJobResult(
            succeeded=True,
            exit_code=0,
            outputs={
                "render.json": json.dumps(diagnostics(render=self.render)).encode(),
                "screenshot.png": f"render-{len(self.calls)}".encode(),
                "storybook.zip": static_storybook_zip(),
            },
            logs="Storybook rendered",
        )


def scores(value: int) -> RubricScores:
    return RubricScores(
        design_language_adherence=value,
        contextual_appropriateness=value,
        interaction_and_state_quality=value,
        responsive_behavior=value,
        accessibility_beyond_baseline=value,
        system_coherence=value,
        story_coverage_and_documentation=value,
        implementation_quality=value,
    )


class FakeReviewer:
    model_name = "reviewer-test"

    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.calls: list[dict[str, Any]] = []

    def review(self, **kwargs: Any) -> ReviewResult:
        self.calls.append(kwargs)
        value = self.values.pop(0)
        return ReviewResult(
            review=RubricReview(
                scores=scores(value),
                cited_problems=[] if value >= 80 else ["The system hierarchy is weak."],
                revision_instructions=[] if value >= 80 else ["Strengthen shared hierarchy."],
            ),
            raw_responses=(json.dumps({"score": value}),),
        )


def test_creator_revises_once_and_publishes_passing_storybook(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    run_id = completed_run(store)
    generator = FakeGenerator()
    sandbox = FakeSandbox()
    reviewer = FakeReviewer([70, 85])
    progress: list[str] = []
    workflow = CreatorWorkflow(
        store=store,
        sandbox_runner=sandbox,
        generator=generator,
        reviewer=reviewer,
        storybooks_directory=tmp_path / "storybooks",
        reporter=progress.append,
    )

    result = workflow.create(run_id, "Create account controls.")

    assert result.evaluation.passed is True
    assert result.evaluation.aggregate_score == 85
    assert len(sandbox.calls) == 2
    assert len(reviewer.calls) == 2
    assert generator.generate_kwargs["source_screenshot"] == b"source-png"
    assert generator.revise_kwargs["render_screenshot"] == b"render-1"
    assert generator.revise_kwargs["storybook"].plan.title == "Account controls"
    assert (result.storybook_directory / "project.json").is_file()
    assert (
        "Revised"
        in (
            result.storybook_directory
            / "project"
            / "src"
            / "components"
            / "Button"
            / "Button.stories.tsx"
        ).read_text()
    )
    assert (result.storybook_directory / "storybook-static" / "index.html").is_file()
    metadata = json.loads((result.storybook_directory / "metadata.json").read_text())
    assert metadata["selected_attempt"] == 2
    assert metadata["story_count"] == 2
    first_attempt = tmp_path / "runs" / run_id / "creations" / result.creation_id / "attempt-1"
    assert (first_attempt / "plan.raw.txt").is_file()
    assert (first_attempt / "tokens.raw.txt").is_file()
    assert (first_attempt / "component-Button.raw.txt").is_file()
    assert store.load_run(run_id).creations[0].status is CreationStatus.COMPLETE
    assert progress[-1] == "Storybook passed the rubric."


def test_creator_publishes_valid_first_attempt_when_revision_generation_fails(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    run_id = completed_run(store)
    generator = RevisionFailingGenerator()
    sandbox = FakeSandbox()
    reviewer = FakeReviewer([70])
    progress: list[str] = []
    workflow = CreatorWorkflow(
        store=store,
        sandbox_runner=sandbox,
        generator=generator,
        reviewer=reviewer,
        storybooks_directory=tmp_path / "storybooks",
        reporter=progress.append,
    )

    result = workflow.create(run_id, "Create account controls.")

    assert result.evaluation.passed is False
    assert result.evaluation.aggregate_score == 70
    assert len(sandbox.calls) == 1
    assert len(reviewer.calls) == 1
    assert (result.storybook_directory / "storybook-static" / "index.html").is_file()
    creation = store.load_run(run_id).creations[0]
    assert creation.status is CreationStatus.COMPLETE
    second_attempt = (
        tmp_path / "runs" / run_id / "creations" / result.creation_id / "attempt-2"
    )
    assert (second_attempt / "revision.validation.txt").read_text() == "revision timed out"
    assert any(
        item.startswith("Revision generation failed; publishing the valid first attempt")
        for item in progress
    )


def test_creator_marks_creation_failed_when_no_attempt_passes_hard_gates(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    run_id = completed_run(store)
    reviewer = FakeReviewer([90, 90])
    workflow = CreatorWorkflow(
        store=store,
        sandbox_runner=FakeSandbox(render=False),
        generator=FakeGenerator(),
        reviewer=reviewer,
        storybooks_directory=tmp_path / "storybooks",
    )

    with pytest.raises(CreatorWorkflowError, match="Storybook hard gates") as error:
        workflow.create(run_id, "Create account controls.")

    creation = store.load_run(run_id).creations[0]
    assert creation.creation_id == error.value.creation_id
    assert creation.status is CreationStatus.FAILED
    assert reviewer.calls == []
    assert not (tmp_path / "storybooks").exists()


def test_creator_persists_raw_responses_when_schema_repair_fails(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    run_id = completed_run(store)
    workflow = CreatorWorkflow(
        store=store,
        sandbox_runner=FakeSandbox(),
        generator=InvalidGenerator(),
        reviewer=FakeReviewer([90]),
        storybooks_directory=tmp_path / "storybooks",
    )

    with pytest.raises(CreatorWorkflowError, match="still does not match") as error:
        workflow.create(run_id, "Create account controls.")

    attempt = tmp_path / "runs" / run_id / "creations" / error.value.creation_id / "attempt-1"
    assert (attempt / "component-Button.raw.txt").read_text() == '{"files":"first"}'
    assert (attempt / "component-Button.repair.raw.txt").read_text() == '{"files":"repair"}'
    assert "still does not match" in (attempt / "component-Button.validation.txt").read_text()


def test_weighted_rubric_enforces_system_coherence_floor() -> None:
    review = RubricReview(
        scores=scores(100).model_copy(update={"system_coherence": 64}),
        cited_problems=[],
        revision_instructions=[],
    )

    evaluation = evaluate_attempt(1, diagnostics(), review)

    assert evaluation.aggregate_score > 80
    assert evaluation.protected_dimensions_passed is False
    assert evaluation.passed is False


def test_storybook_hard_gates_cover_build_story_count_and_each_story() -> None:
    values = diagnostics()
    assert all(hard_gates_from_diagnostics(values).values())

    values["build"]["story_count"] = 1
    assert hard_gates_from_diagnostics(values)["story_coverage"] is False

    values = diagnostics()
    values["stories"][1]["blocked_requests"] = ["https://example.com/image.png"]
    assert hard_gates_from_diagnostics(values)["security"] is False


def test_storybook_files_are_one_bounded_json_input() -> None:
    files = storybook_files(generated())

    assert set(files) == {"project.json"}
    project = json.loads(files["project.json"])
    assert project["schema_version"] == 1
    assert len(project["plan"]["stories"]) == 2
    assert len(project["files"]) == 4


def test_container_storybook_fixture_matches_the_host_contract() -> None:
    fixture = GeneratedStorybook.model_validate_json(
        (FIXTURE_DIRECTORY / "storybook-project.json").read_bytes()
    )

    validate_generated_storybook(fixture)
    assert fixture.plan.title == "Account controls"


def test_generated_storybook_rejects_unsupported_import_and_missing_story() -> None:
    unsupported = generated().model_copy(deep=True)
    unsupported.files[1].content = (
        'import thing from "unknown-package";\n' + unsupported.files[1].content
    )
    with pytest.raises(ValueError, match="unsupported package"):
        validate_generated_storybook(unsupported)

    missing_story = generated().model_copy(deep=True)
    missing_story.files[-1].content = missing_story.files[-1].content.replace(
        "export const Disabled", "const Disabled"
    )
    with pytest.raises(ValueError, match="planned story is not exported"):
        validate_generated_storybook(missing_story)

    extra_story = generated().model_copy(deep=True)
    extra_story.files[-1].content += "\nexport const Unplanned: Story = {};\n"
    with pytest.raises(ValueError, match="unplanned story is exported"):
        validate_generated_storybook(extra_story)

    dynamic_import = generated().model_copy(deep=True)
    dynamic_import.files[1].content += '\nconst load = () => import("./Button");\n'
    with pytest.raises(ValueError, match="forbidden runtime API"):
        validate_generated_storybook(dynamic_import)

    legacy_storybook_test = generated().model_copy(deep=True)
    legacy_storybook_test.files[-1].content = (
        'import { fn } from "@storybook/test";\n' + legacy_storybook_test.files[-1].content
    )
    with pytest.raises(ValueError, match='use "storybook/test" instead'):
        validate_generated_storybook(legacy_storybook_test)

    cross_component_import = two_component_project()
    cross_component_import.files[1].content = (
        'import { Card } from "../Card/Card";\n' + cross_component_import.files[1].content
    )
    with pytest.raises(ValueError, match="escapes or is missing"):
        validate_generated_storybook(cross_component_import)


def test_generated_storybook_schema_is_strict_and_described() -> None:
    schema = GeneratedStorybook.model_json_schema()
    plan_schema = StorybookPlanResponse.model_json_schema()
    token_schema = StorybookTokensResponse.model_json_schema()
    component_schema = StorybookComponentResponse.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema_version",
        "plan",
        "files",
        "inferred_choices",
    }
    assert all("description" in field for field in schema["properties"].values())
    assert schema["$defs"]["GeneratedFile"]["additionalProperties"] is False
    assert schema["$defs"]["StorybookPlan"]["additionalProperties"] is False
    assert set(plan_schema["required"]) == {"schema_version", "plan"}
    assert set(token_schema["required"]) == {
        "schema_version",
        "content",
        "inferred_choices",
    }
    assert set(component_schema["required"]) == {
        "schema_version",
        "files",
        "inferred_choices",
    }
    assert plan_schema["additionalProperties"] is False
    assert token_schema["additionalProperties"] is False
    assert component_schema["additionalProperties"] is False
    assert "anyOf" not in json.dumps(schema)
    assert "oneOf" not in json.dumps(schema)


def test_reviewer_schema_has_eight_scores_and_concrete_problems() -> None:
    schema = ReviewerResponse.model_json_schema()
    expected = set(scores(80).model_dump()) | {"concrete_problems"}

    assert set(schema["required"]) == expected
    assert set(schema["properties"]) == expected
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["ConcreteProblem"]["additionalProperties"] is False


def test_creator_and_reviewer_reject_missing_fields_and_score_coercion() -> None:
    data = generated().model_dump()
    del data["plan"]
    with pytest.raises(ValidationError, match="plan"):
        GeneratedStorybook.model_validate(data)

    coercible_scores: dict[str, object] = scores(80).model_dump()
    coercible_scores["system_coherence"] = "80"
    coercible_scores["concrete_problems"] = []
    with pytest.raises(ValidationError, match="system_coherence"):
        ReviewerResponse.model_validate(coercible_scores)


class FakeOllamaClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=self.responses.pop(0)))


class TimedFakeOllamaClient(FakeOllamaClient):
    def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            time.sleep(0.03)
        return SimpleNamespace(
            message=SimpleNamespace(content=self.responses.pop(0)),
            total_duration=1_500_000_000,
            prompt_eval_count=120,
            prompt_eval_duration=500_000_000,
            eval_count=30,
            eval_duration=1_000_000_000,
        )


class PlanningFailureClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        if prompt.startswith("Plan the smallest sufficient React"):
            raise TimeoutError("planning timed out")
        if prompt.startswith("Generate the single shared CSS token"):
            content = json.dumps(
                {
                    "schema_version": 1,
                    "content": ":root { --pg-action: #f60; }",
                    "inferred_choices": [],
                }
            )
        else:
            content = json.dumps(
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "path": (
                                "src/components/RequestedInterface/RequestedInterface.tsx"
                            ),
                            "content": (
                                'import "../../tokens/tokens.css";\n'
                                'import "./RequestedInterface.css";\n'
                                "export function RequestedInterface() {\n"
                                '  return <main className="requested-interface">Interface</main>;\n'
                                "}\n"
                            ),
                        },
                        {
                            "path": (
                                "src/components/RequestedInterface/RequestedInterface.css"
                            ),
                            "content": ".requested-interface { color: var(--pg-action); }\n",
                        },
                        {
                            "path": (
                                "src/components/RequestedInterface/"
                                "RequestedInterface.stories.tsx"
                            ),
                            "content": (
                                'import type { Meta, StoryObj } from "@storybook/react-vite";\n'
                                'import { RequestedInterface } from "./RequestedInterface";\n'
                                "const meta = { component: RequestedInterface } "
                                "satisfies Meta<typeof RequestedInterface>;\n"
                                "export default meta;\n"
                                "type Story = StoryObj<typeof meta>;\n"
                                "export const Default: Story = {};\n"
                            ),
                        },
                    ],
                    "inferred_choices": [],
                }
            )
        return SimpleNamespace(message=SimpleNamespace(content=content))


def tokens_response(project: GeneratedStorybook | None = None) -> str:
    resolved = project or generated()
    return json.dumps(
        {
            "schema_version": 1,
            "content": resolved.files[0].content,
            "inferred_choices": [],
        }
    )


def component_response(
    project: GeneratedStorybook | None = None,
    component_name: str = "Button",
) -> str:
    resolved = project or generated()
    directory = f"src/components/{component_name}/"
    return json.dumps(
        {
            "schema_version": 1,
            "files": [
                item.model_dump(mode="json")
                for item in resolved.files
                if item.path.startswith(directory)
            ],
            "inferred_choices": resolved.inferred_choices,
        }
    )


def two_component_project() -> GeneratedStorybook:
    project = generated().model_copy(deep=True)
    project.plan.components.append(
        PlannedComponent(
            name="Card",
            purpose="Group related account information.",
            dependencies=[],
            props=["title: string"],
            variants=["default"],
            states=["default"],
        )
    )
    project.plan.stories.append(
        PlannedStory(
            component="Card",
            name="Default",
            description="Basic account information.",
            viewport="desktop",
        )
    )
    project.files.extend(
        [
            GeneratedFile(
                path="src/components/Card/Card.tsx",
                content=(
                    'import "../../tokens/tokens.css";\n'
                    'import "./Card.css";\n'
                    "export interface CardProps { title: string }\n"
                    "export function Card({ title }: CardProps) {\n"
                    '  return <section className="pg-card"><h2>{title}</h2></section>;\n'
                    "}\n"
                ),
            ),
            GeneratedFile(
                path="src/components/Card/Card.css",
                content=".pg-card { padding: 1rem; color: var(--pg-action); }\n",
            ),
            GeneratedFile(
                path="src/components/Card/Card.stories.tsx",
                content=(
                    'import type { Meta, StoryObj } from "@storybook/react-vite";\n'
                    'import { Card } from "./Card";\n'
                    "const meta = { component: Card, args: { title: "
                    '"Account"'
                    " } } satisfies Meta<typeof Card>;\n"
                    "export default meta;\n"
                    "type Story = StoryObj<typeof meta>;\n"
                    "export const Default: Story = {};\n"
                ),
            ),
        ]
    )
    return project


class ParallelComponentClient:
    def __init__(self, project: GeneratedStorybook) -> None:
        self.project = project
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._barrier = threading.Barrier(2)
        self.active_components = 0
        self.max_active_components = 0

    def chat(self, **kwargs: Any) -> SimpleNamespace:
        with self._lock:
            self.calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        if prompt.startswith("Plan the smallest sufficient React"):
            content = json.dumps(
                {
                    "schema_version": 1,
                    "plan": self.project.plan.model_dump(mode="json"),
                }
            )
        elif prompt.startswith("Generate the single shared CSS token"):
            content = tokens_response(self.project)
        else:
            component_name = next(
                name
                for name in ("Button", "Card")
                if f"Implement only the isolated {name}" in prompt
            )
            with self._lock:
                self.active_components += 1
                self.max_active_components = max(
                    self.max_active_components,
                    self.active_components,
                )
            self._barrier.wait(timeout=1)
            time.sleep(0.01)
            content = component_response(self.project, component_name)
            with self._lock:
                self.active_components -= 1
        return SimpleNamespace(message=SimpleNamespace(content=content))


class FailingComponentClient:
    def __init__(self, project: GeneratedStorybook, failed_component: str) -> None:
        self.project = project
        self.failed_component = failed_component
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def chat(self, **kwargs: Any) -> SimpleNamespace:
        with self._lock:
            self.calls.append(kwargs)
        prompt = kwargs["messages"][-1]["content"]
        if prompt.startswith("Plan the smallest sufficient React"):
            content = json.dumps(
                {
                    "schema_version": 1,
                    "plan": self.project.plan.model_dump(mode="json"),
                }
            )
        elif prompt.startswith("Generate the single shared CSS token"):
            content = tokens_response(self.project)
        elif f"Implement only the isolated {self.failed_component}" in prompt:
            raise TimeoutError("component timed out")
        else:
            component_name = next(
                name
                for name in ("Button", "Card")
                if f"Implement only the isolated {name}" in prompt
            )
            content = component_response(self.project, component_name)
        return SimpleNamespace(message=SimpleNamespace(content=content))


def test_ollama_creator_uses_minimal_fallback_when_planning_fails() -> None:
    client = PlanningFailureClient()
    progress: list[str] = []
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        reporter=progress.append,
    )

    result = generator.generate(
        prompt="Create a compact chatbot.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert result.storybook.plan.title == "Requested interface"
    assert [component.name for component in result.storybook.plan.components] == [
        "RequestedInterface"
    ]
    assert result.plan_raw_responses == ()
    assert len(client.calls) == 3
    assert client.calls[0]["options"]["num_predict"] == 2_048
    assert any(
        item.startswith(
            "Planning failed; continuing with the minimal host fallback plan"
        )
        for item in progress
    )


def test_ollama_creator_repairs_plan_and_one_isolated_component() -> None:
    invalid_plan = json.dumps(
        {
            "schema_version": "1.0.0",
            "plan": {
                "components": [
                    {
                        "name": "Button",
                        "files": ["Button.tsx", "Button.css", "Button.stories.tsx"],
                    }
                ]
            },
        }
    )
    repaired_plan = json.dumps(
        {
            "schema_version": 1,
            "plan": generated().plan.model_dump(mode="json"),
        }
    )
    invalid_component = json.dumps(
        {
            "schema_version": "1.0.0",
            "files": {"Button.tsx": "export function Button() { return null; }"},
            "inferred_choices": {"layout": "Used the observed spacing rhythm."},
        }
    )
    repaired_component = component_response()
    client = FakeOllamaClient(
        [
            invalid_plan,
            repaired_plan,
            tokens_response(),
            invalid_component,
            repaired_component,
        ]
    )
    progress: list[str] = []
    generator = OllamaStorybookGenerator(client, model_name="test-model", reporter=progress.append)

    result = generator.generate(
        prompt="Create account controls.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert result.storybook == generated()
    assert result.plan_raw_responses == (invalid_plan, repaired_plan)
    assert result.token_raw_responses == (tokens_response(),)
    assert result.component_raw_responses == {"Button": (invalid_component, repaired_component)}
    assert len(client.calls) == 5
    assert all(call["think"] is False for call in client.calls)
    assert all(call["format"] is None for call in client.calls)
    planning_prompt = client.calls[0]["messages"][-1]["content"]
    assert client.calls[0]["messages"][0]["role"] == "system"
    assert "smallest coherent implementation" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["options"]["num_predict"] == 2_048
    assert '"schema_version": 1' in planning_prompt
    assert "Do not generate source files in this phase" in planning_prompt
    assert "Generate at most 4 isolated" in planning_prompt
    assert "Never exceed eight stories total" in planning_prompt
    assert "images" not in client.calls[1]["messages"][-1]
    plan_repair_prompt = client.calls[1]["messages"][-1]["content"]
    assert "plan.stories: Field required" in plan_repair_prompt
    token_prompt = client.calls[2]["messages"][-1]["content"]
    assert "single shared CSS token file" in token_prompt
    assert "images" not in client.calls[2]["messages"][-1]
    component_prompt = client.calls[3]["messages"][-1]["content"]
    assert "Implement only the isolated Button" in component_prompt
    assert "Never import another src/components directory" in component_prompt
    assert "avoid optional helpers" in component_prompt
    component_repair_prompt = client.calls[4]["messages"][-1]["content"]
    assert "Repair only the isolated Button" in component_repair_prompt
    assert "files: Input should be a valid array" in component_repair_prompt
    assert progress[0] == "Planning Storybook components and stories..."
    assert progress[1].startswith("Planning request sent to test-model")
    assert progress[2].startswith("Planning completed in ")
    assert "Creator plan did not match StorybookPlan; repairing once..." in progress
    assert "Generating shared Storybook design tokens..." in progress
    assert "Generating 1 isolated component(s) in parallel (up to 1 at once)..." in progress
    assert any(item.startswith("Component Button request sent") for item in progress)
    assert any("Creator component Button did not match its contract" in item for item in progress)
    assert any(item.startswith("Component Button repair request sent") for item in progress)


def test_ollama_creator_generates_isolated_components_in_parallel() -> None:
    project = two_component_project()
    client = ParallelComponentClient(project)
    progress: list[str] = []
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        reporter=progress.append,
    )

    result = generator.generate(
        prompt="Create account controls and an information card.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert result.storybook == project
    assert client.max_active_components == 2
    assert set(result.component_raw_responses) == {"Button", "Card"}
    assert len(client.calls) == 4
    assert all(call["think"] is False for call in client.calls)
    assert "Generating 2 isolated component(s) in parallel (up to 2 at once)..." in progress


def test_ollama_creator_enforces_configured_component_limit() -> None:
    oversized_plan = json.dumps(
        {
            "schema_version": 1,
            "plan": two_component_project().plan.model_dump(mode="json"),
        }
    )
    repaired_plan = json.dumps(
        {
            "schema_version": 1,
            "plan": generated().plan.model_dump(mode="json"),
        }
    )
    client = FakeOllamaClient(
        [
            oversized_plan,
            repaired_plan,
            tokens_response(),
            component_response(),
        ]
    )
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        max_components=1,
    )

    result = generator.generate(
        prompt="Create account controls.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert [component.name for component in result.storybook.plan.components] == ["Button"]
    planning_prompt = client.calls[0]["messages"][-1]["content"]
    assert "Generate at most 1 isolated" in planning_prompt
    repair_prompt = client.calls[1]["messages"][-1]["content"]
    assert "configured maximum is 1" in repair_prompt
    assert "plan contains at most 1 components" in repair_prompt


def test_ollama_creator_omits_one_component_after_two_generation_failures() -> None:
    project = two_component_project()
    client = FailingComponentClient(project, "Card")
    progress: list[str] = []
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        reporter=progress.append,
    )

    result = generator.generate(
        prompt="Create account controls and an information card.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert [component.name for component in result.storybook.plan.components] == ["Button"]
    assert {story.component for story in result.storybook.plan.stories} == {"Button"}
    assert all("/Card/" not in item.path for item in result.storybook.files)
    assert set(result.component_raw_responses) == {"Button"}
    assert (
        sum(
            "Implement only the isolated Card" in call["messages"][-1]["content"]
            for call in client.calls
        )
        == 2
    )
    assert "Retrying only failed component generation: Card." in progress
    assert "Continuing without component(s) that failed twice: Card." in progress


def test_ollama_creator_revision_reuses_previous_component_after_two_failures() -> None:
    project = two_component_project()
    client = FailingComponentClient(project, "Card")
    progress: list[str] = []
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        reporter=progress.append,
    )
    evaluation = evaluate_attempt(
        1,
        diagnostics(),
        RubricReview(
            scores=scores(70),
            cited_problems=["The hierarchy is weak."],
            revision_instructions=["Strengthen hierarchy."],
        ),
    )

    result = generator.revise(
        prompt="Create account controls and an information card.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"source",
        storybook=project,
        evaluation=evaluation,
        render_screenshot=b"render",
    )

    assert result.storybook.plan == project.plan
    assert {
        item.path for item in result.storybook.files if item.path.startswith("src/components/Card/")
    } == {
        "src/components/Card/Card.tsx",
        "src/components/Card/Card.css",
        "src/components/Card/Card.stories.tsx",
    }
    assert "Component Card still failed; reusing its previous valid files." in progress


def test_ollama_creator_reports_heartbeat_and_native_timing() -> None:
    plan_response = json.dumps(
        {
            "schema_version": 1,
            "plan": generated().plan.model_dump(mode="json"),
        }
    )
    component_project = generated()
    component_project.files[-1].content = (
        'import { fn } from "@storybook/test";\n' + component_project.files[-1].content
    )
    client = TimedFakeOllamaClient(
        [plan_response, tokens_response(component_project), component_response(component_project)]
    )
    progress: list[str] = []
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        reporter=progress.append,
        heartbeat_seconds=0.01,
        use_structured_outputs=True,
    )

    result = generator.generate(
        prompt="Create account controls.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert any("Planning is still waiting for the model..." in item for item in progress)
    planning_request = next(item for item in progress if item.startswith("Planning request sent"))
    assert "prompt " in planning_request
    assert "1 image(s) / 3 B" in planning_request
    planning_timing = next(item for item in progress if item.startswith("Planning completed"))
    assert "wall-clock" in planning_timing
    assert "server 1.5s" in planning_timing
    assert "prompt 120 tokens in 0.5s" in planning_timing
    assert "output 30 tokens in 1.0s (30.0 tokens/s)" in planning_timing
    assert '"storybook/test"' in result.storybook.files[-1].content
    assert '"@storybook/test"' not in result.storybook.files[-1].content
    assert len(client.calls) == 3
    assert all(call["think"] is False for call in client.calls)
    assert all(isinstance(call["format"], dict) for call in client.calls)


def test_ollama_creator_revision_replans_without_resending_images() -> None:
    plan_response = json.dumps(
        {
            "schema_version": 1,
            "plan": generated("Revised").plan.model_dump(mode="json"),
        }
    )
    revised_project = generated("Revised")
    client = FakeOllamaClient(
        [plan_response, tokens_response(revised_project), component_response(revised_project)]
    )
    progress: list[str] = []
    generator = OllamaStorybookGenerator(client, model_name="test-model", reporter=progress.append)
    evaluation = evaluate_attempt(
        1,
        diagnostics(),
        RubricReview(
            scores=scores(70),
            cited_problems=["The hierarchy is weak."],
            revision_instructions=["Strengthen hierarchy."],
        ),
    )

    result = generator.revise(
        prompt="Create account controls.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"source",
        storybook=generated("First"),
        evaluation=evaluation,
        render_screenshot=b"render",
    )

    assert result.storybook == generated("Revised")
    assert len(client.calls) == 3
    assert all(call["think"] is False for call in client.calls)
    assert "images" not in client.calls[0]["messages"][-1]
    assert "Current plan:" in client.calls[0]["messages"][-1]["content"]
    assert "Current tokens:" in client.calls[1]["messages"][-1]["content"]
    assert "Current component files:" in client.calls[2]["messages"][-1]["content"]
    assert "images" not in client.calls[1]["messages"][-1]
    assert progress[0] == "Planning the Storybook revision from evaluation feedback..."
    assert progress[1].startswith("Planning request sent to test-model")
    assert progress[2].startswith("Planning completed in ")
    assert "Generating shared Storybook design tokens..." in progress
    assert any(item.startswith("Token generation request sent") for item in progress)
    assert any(item.startswith("Component Button request sent") for item in progress)


def test_ollama_creator_repairs_only_component_cited_by_sandbox_failure() -> None:
    project = two_component_project()
    failed_diagnostics = diagnostics()
    failed_diagnostics["build"]["typecheck_succeeded"] = False
    failed_diagnostics["build"]["storybook_succeeded"] = False
    failed_diagnostics["errors"] = [
        "src/components/Card/Card.stories.tsx: Property 'args' is missing."
    ]
    evaluation = failed_hard_gate_evaluation(1, failed_diagnostics)
    client = FakeOllamaClient([component_response(project, "Card")])
    progress: list[str] = []
    generator = OllamaStorybookGenerator(
        client,
        model_name="test-model",
        reporter=progress.append,
    )

    result = generator.revise(
        prompt="Create account controls and an information card.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"source",
        storybook=project,
        evaluation=evaluation,
        render_screenshot=b"render",
    )

    assert result.storybook == project
    assert result.plan_raw_responses == ()
    assert result.token_raw_responses == ()
    assert len(client.calls) == 1
    assert "Implement only the isolated Card" in client.calls[0]["messages"][-1]["content"]
    assert progress[0] == (
        "Repairing deterministic sandbox failures without replanning (Card)..."
    )
    assert "Reusing the validated shared Storybook tokens..." in progress
    assert not any(item.startswith("Planning request sent") for item in progress)


def test_ollama_reviewer_repairs_schema_mismatch_once() -> None:
    invalid = '{"design_language_adherence":90}'
    repaired = ReviewerResponse(**scores(88).model_dump(), concrete_problems=[])
    client = FakeOllamaClient([invalid, repaired.model_dump_json()])
    progress: list[str] = []
    reviewer = OllamaStorybookReviewer(client, model_name="test-model", reporter=progress.append)

    result = reviewer.review(
        prompt="Create account controls.",
        style_guide={"colors": {}},
        source_screenshot=b"source",
        storybook=generated(),
        render_diagnostics=diagnostics(),
        render_screenshot=b"render",
    )

    assert result.review.scores == scores(88)
    assert result.raw_responses == (invalid, repaired.model_dump_json())
    assert len(client.calls) == 2
    assert all(call["think"] is False for call in client.calls)
    assert all(call["format"] is None for call in client.calls)
    assert progress == ["Reviewer response needs repair; sending validation errors to model..."]


def test_ollama_reviewer_accepts_flat_storybook_response() -> None:
    response = ReviewerResponse(
        **scores(90).model_dump(),
        concrete_problems=[
            ConcreteProblem(
                dimension="system_coherence",
                problem="The disabled state bypasses the shared opacity token.",
                actionable_revision="Add and reuse a disabled-opacity token.",
            )
        ],
    ).model_dump_json()
    client = FakeOllamaClient([response])
    reviewer = OllamaStorybookReviewer(client, model_name="test-model")

    result = reviewer.review(
        prompt="Create account controls.",
        style_guide={"colors": {}},
        source_screenshot=b"source",
        storybook=generated(),
        render_diagnostics=diagnostics(),
        render_screenshot=b"render",
    )

    assert result.review.scores.system_coherence == 90
    assert result.review.cited_problems == [
        "system_coherence: The disabled state bypasses the shared opacity token."
    ]
    assert len(client.calls) == 1
    assert client.calls[0]["think"] is False
    assert client.calls[0]["format"] is None
