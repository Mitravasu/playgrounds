import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from playgrounds.creator_workflow import (
    CreatorWorkflow,
    CreatorWorkflowError,
    GeneratedComponent,
    GenerationResult,
    ModelResponseError,
    OllamaComponentGenerator,
    OllamaComponentReviewer,
    ReviewerResponse,
    ReviewResult,
    RubricReview,
    RubricScores,
    component_files,
    evaluate_attempt,
)
from playgrounds.runs import CreationStatus, RunStore
from playgrounds.sandbox import SandboxJobResult


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


def generated(label: str) -> GeneratedComponent:
    return GeneratedComponent(
        markup=f'<button data-pg-component type="button">{label}</button>',
        css="[data-pg-component] { color: #f60; }\n"
        "[data-pg-component]:focus-visible { outline: 2px solid currentColor; }",
        javascript='(() => { document.querySelector("[data-pg-component]")?.focus(); })();',
        inferred_choices=["Used the action color for the requested control."],
    )


class FakeGenerator:
    model_name = "creator-test"

    def __init__(self) -> None:
        self.generate_kwargs: dict[str, Any] = {}
        self.revise_kwargs: dict[str, Any] = {}

    def generate(self, **kwargs: Any) -> GenerationResult:
        self.generate_kwargs = kwargs
        return GenerationResult(
            component=generated("First"),
            raw_responses=('{"first":"invalid"}', '{"first":"repaired"}'),
        )

    def revise(self, **kwargs: Any) -> GenerationResult:
        self.revise_kwargs = kwargs
        return GenerationResult(component=generated("Revised"), raw_responses=('{"revised":true}',))


class InvalidGenerator(FakeGenerator):
    def generate(self, **kwargs: Any) -> GenerationResult:
        raise ModelResponseError(
            "creator response still does not match its schema",
            role="generation",
            responses=('{"markup":"first"}', '{"markup":"repair"}'),
        )


class FakeSandbox:
    def __init__(self, *, root_found: bool = True) -> None:
        self.root_found = root_found
        self.calls: list[tuple[object, Mapping[str, bytes]]] = []

    def run(self, request: object, input_files: Mapping[str, bytes]) -> SandboxJobResult:
        self.calls.append((request, input_files))
        diagnostics = {
            "schema_version": 1,
            "errors": [],
            "blocked_requests": [],
            "inspection": {
                "root_found": self.root_found,
                "root_bounds": {"width": 200, "height": 40},
                "interactive_count": 1,
                "unnamed_interactive_count": 0,
            },
        }
        return SandboxJobResult(
            succeeded=True,
            exit_code=0,
            outputs={
                "render.json": json.dumps(diagnostics).encode(),
                "screenshot.png": f"render-{len(self.calls)}".encode(),
            },
            logs="creator rendered",
        )


def scores(value: int) -> RubricScores:
    return RubricScores(
        design_language_adherence=value,
        contextual_appropriateness=value,
        interaction_and_state_quality=value,
        responsive_behavior=value,
        accessibility_beyond_baseline=value,
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
                cited_problems=[] if value >= 80 else ["The hierarchy is too weak."],
                revision_instructions=[] if value >= 80 else ["Increase visual hierarchy."],
            ),
            raw_responses=(json.dumps({"score": value}),),
        )


def test_creator_revises_once_and_publishes_passing_component(tmp_path: Path) -> None:
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
        components_directory=tmp_path / "components",
        reporter=progress.append,
    )

    result = workflow.create(run_id, "Create an account action.")

    assert result.evaluation.passed is True
    assert result.evaluation.aggregate_score == 85
    assert len(sandbox.calls) == 2
    assert len(reviewer.calls) == 2
    assert generator.generate_kwargs["source_screenshot"] == b"source-png"
    assert generator.revise_kwargs["render_screenshot"] == b"render-1"
    assert (result.component_directory / "component.html").is_file()
    assert "Revised" in (result.component_directory / "component.html").read_text()
    assert (
        json.loads((result.component_directory / "metadata.json").read_text())["selected_attempt"]
        == 2
    )
    record = store.load_run(run_id)
    assert record.creations[0].status is CreationStatus.COMPLETE
    assert (
        tmp_path
        / "runs"
        / run_id
        / "creations"
        / result.creation_id
        / "attempt-1"
        / "evaluation.json"
    ).is_file()
    assert (
        tmp_path
        / "runs"
        / run_id
        / "creations"
        / result.creation_id
        / "attempt-1"
        / "generation.repair.raw.txt"
    ).is_file()
    assert progress[-1] == "Creation passed the rubric."


def test_creator_marks_creation_failed_when_no_attempt_passes_hard_gates(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    run_id = completed_run(store)
    reviewer = FakeReviewer([90, 90])
    workflow = CreatorWorkflow(
        store=store,
        sandbox_runner=FakeSandbox(root_found=False),
        generator=FakeGenerator(),
        reviewer=reviewer,
        components_directory=tmp_path / "components",
    )

    with pytest.raises(CreatorWorkflowError, match="no creation attempt passed") as error:
        workflow.create(run_id, "Create an account action.")

    creation = store.load_run(run_id).creations[0]
    assert creation.creation_id == error.value.creation_id
    assert creation.status is CreationStatus.FAILED
    assert reviewer.calls == []
    assert not (tmp_path / "components").exists()


def test_creator_persists_both_raw_responses_when_schema_repair_fails(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    run_id = completed_run(store)
    workflow = CreatorWorkflow(
        store=store,
        sandbox_runner=FakeSandbox(),
        generator=InvalidGenerator(),
        reviewer=FakeReviewer([90]),
        components_directory=tmp_path / "components",
    )

    with pytest.raises(CreatorWorkflowError, match="still does not match") as error:
        workflow.create(run_id, "Create an account action.")

    attempt = tmp_path / "runs" / run_id / "creations" / error.value.creation_id / "attempt-1"
    assert (attempt / "generation.raw.txt").read_text() == '{"markup":"first"}'
    assert (attempt / "generation.repair.raw.txt").read_text() == '{"markup":"repair"}'
    assert store.load_run(run_id).creations[0].status is CreationStatus.FAILED


def test_weighted_rubric_enforces_protected_score_floors() -> None:
    review = RubricReview(
        scores=RubricScores(
            design_language_adherence=64,
            contextual_appropriateness=100,
            interaction_and_state_quality=100,
            responsive_behavior=100,
            accessibility_beyond_baseline=100,
            implementation_quality=100,
        ),
        cited_problems=[],
        revision_instructions=[],
    )
    diagnostics = {
        "errors": [],
        "blocked_requests": [],
        "inspection": {
            "root_found": True,
            "root_bounds": {"width": 100, "height": 40},
            "unnamed_interactive_count": 0,
        },
    }

    evaluation = evaluate_attempt(1, diagnostics, review)

    assert evaluation.aggregate_score > 80
    assert evaluation.protected_dimensions_passed is False
    assert evaluation.passed is False


def test_component_files_are_separate_and_directly_openable() -> None:
    files = component_files(generated("Open"))

    assert b'href="component.css"' in files["component.html"]
    assert b'src="component.js"' in files["component.html"]
    assert b"data-pg-component" in files["component.html"]


def test_creator_json_schema_has_one_required_shape_with_described_fields() -> None:
    schema = GeneratedComponent.model_json_schema()

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["markup", "css", "javascript", "inferred_choices"]
    assert set(schema["properties"]) == set(schema["required"])
    assert all("description" in field for field in schema["properties"].values())
    assert schema["properties"]["inferred_choices"]["items"] == {
        "maxLength": 500,
        "minLength": 1,
        "type": "string",
    }
    assert schema["properties"]["javascript"]["type"] == "string"
    assert "minLength" not in schema["properties"]["javascript"]
    assert "anyOf" not in json.dumps(schema)
    assert "oneOf" not in json.dumps(schema)


def test_reviewer_json_schema_has_one_required_shape_with_fixed_scores() -> None:
    schema = ReviewerResponse.model_json_schema()
    expected_scores = {
        "design_language_adherence",
        "contextual_appropriateness",
        "interaction_and_state_quality",
        "responsive_behavior",
        "accessibility_beyond_baseline",
        "implementation_quality",
    }
    expected_fields = expected_scores | {"concrete_problems"}

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == expected_fields
    assert set(schema["properties"]) == expected_fields
    assert all("description" in schema["properties"][name] for name in expected_scores)
    problem_schema = schema["$defs"]["ConcreteProblem"]
    assert problem_schema["additionalProperties"] is False
    assert problem_schema["required"] == [
        "dimension",
        "problem",
        "actionable_revision",
    ]
    assert set(problem_schema["properties"]) == set(problem_schema["required"])
    assert "anyOf" not in json.dumps(schema)
    assert "oneOf" not in json.dumps(schema)


def test_creator_and_reviewer_models_reject_missing_fields_and_type_coercion() -> None:
    with pytest.raises(ValidationError, match="inferred_choices"):
        GeneratedComponent.model_validate(
            {
                "markup": '<button data-pg-component type="button">Menu</button>',
                "css": "[data-pg-component] { color: orange; }",
                "javascript": "(() => {})();",
            }
        )

    coercible_scores: dict[str, object] = scores(80).model_dump()
    coercible_scores["design_language_adherence"] = "80"
    coercible_scores["concrete_problems"] = []
    with pytest.raises(ValidationError, match="design_language_adherence"):
        ReviewerResponse.model_validate(coercible_scores)

    with pytest.raises(ValidationError, match="concrete_problems"):
        ReviewerResponse.model_validate(scores(80).model_dump())


class FakeOllamaClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(message=SimpleNamespace(content=self.responses.pop(0)))


def test_ollama_creator_repairs_embedded_css_js_and_object_inferences_once() -> None:
    invalid = json.dumps(
        {
            "markup": (
                "<nav data-pg-component><button>Menu</button>"
                "<style>[data-pg-component] { color: orange; }</style>"
                "<script>(() => {})();</script></nav>"
            ),
            "inferred_choices": {"navigation_style": "Uses the source navigation rhythm."},
        }
    )
    repaired_component = generated("Repaired").model_copy(update={"javascript": ""})
    repaired_response = f"```json\n{repaired_component.model_dump_json()}\n```"
    client = FakeOllamaClient([invalid, repaired_response])
    progress: list[str] = []
    generator = OllamaComponentGenerator(client, model_name="test-model", reporter=progress.append)

    result = generator.generate(
        prompt="Create a menu.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert result.component == repaired_component
    assert result.raw_responses == (invalid, repaired_response)
    assert len(client.calls) == 2
    assert "images" not in client.calls[1]["messages"][0]
    assert "move their contents" in client.calls[1]["messages"][0]["content"]
    assert progress == ["Creator response needs repair; sending validation errors to model..."]


def test_ollama_creator_accepts_static_component_with_empty_javascript() -> None:
    static_component = generated("Static navigation").model_copy(update={"javascript": ""})
    response = static_component.model_dump_json()
    client = FakeOllamaClient([response])
    generator = OllamaComponentGenerator(client, model_name="test-model")

    result = generator.generate(
        prompt="Create a static navigation bar.",
        style_guide={"colors": {"action": "orange"}},
        source_screenshot=b"png",
    )

    assert result.component == static_component
    assert result.raw_responses == (response,)
    assert component_files(result.component)["component.js"] == b"\n"
    assert len(client.calls) == 1


def test_ollama_reviewer_repairs_schema_mismatch_once() -> None:
    invalid = '{"design_language_adherence": 90}'
    repaired_response = ReviewerResponse(**scores(88).model_dump(), concrete_problems=[])
    expected_review = RubricReview(scores=scores(88), cited_problems=[], revision_instructions=[])
    client = FakeOllamaClient([invalid, repaired_response.model_dump_json()])
    progress: list[str] = []
    reviewer = OllamaComponentReviewer(client, model_name="test-model", reporter=progress.append)

    result = reviewer.review(
        prompt="Create a menu.",
        style_guide={"colors": {}},
        source_screenshot=b"source",
        component=generated("Menu"),
        render_diagnostics={"errors": []},
        render_screenshot=b"render",
    )

    assert result.review == expected_review
    assert result.raw_responses == (invalid, repaired_response.model_dump_json())
    assert len(client.calls) == 2
    assert progress == ["Reviewer response needs repair; sending validation errors to model..."]


def test_ollama_reviewer_accepts_observed_flat_response_without_repair() -> None:
    response = """```json
{
  "design_language_adherence": 85,
  "contextual_appropriateness": 100,
  "interaction_and_state_quality": 90,
  "responsive_behavior": 100,
  "accessibility_beyond_baseline": 95,
  "implementation_quality": 100,
  "concrete_problems": [
    {
      "dimension": "design_language_adherence",
      "problem": "Navigation links use the small text token.",
      "actionable_revision": "Use the base text token for primary navigation links."
    }
  ]
}
```"""
    client = FakeOllamaClient([response])
    reviewer = OllamaComponentReviewer(client, model_name="test-model")

    result = reviewer.review(
        prompt="Create a navigation bar.",
        style_guide={"typography": {}},
        source_screenshot=b"source",
        component=generated("Navigation"),
        render_diagnostics={"errors": []},
        render_screenshot=b"render",
    )

    assert result.review.scores.design_language_adherence == 85
    assert result.review.cited_problems == [
        "design_language_adherence: Navigation links use the small text token."
    ]
    assert result.review.revision_instructions == [
        "Use the base text token for primary navigation links."
    ]
    assert result.raw_responses == (response,)
    assert len(client.calls) == 1
    assert set(client.calls[0]["format"]["required"]) == {
        "design_language_adherence",
        "contextual_appropriateness",
        "interaction_and_state_quality",
        "responsive_behavior",
        "accessibility_beyond_baseline",
        "implementation_quality",
        "concrete_problems",
    }
