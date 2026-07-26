import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from playgrounds.analyzer_workflow import (
    AnalyzerWorkflow,
    AnalyzerWorkflowError,
    OllamaStyleGuideSynthesizer,
    validate_style_guide_response,
)
from playgrounds.runs import AnalysisStatus, RunStore
from playgrounds.sandbox import SandboxJobResult
from playgrounds.style_guide import (
    CaptureMetadata,
    ComponentPattern,
    InteractionState,
    LayoutRule,
    StyleGuide,
    StyleGuideContent,
    Viewport,
)
from playgrounds.synthesis_evidence import summarize_observations


def guide(url: str) -> StyleGuide:
    return StyleGuide(
        source_url=url,
        capture=CaptureMetadata(title="Example", viewport=Viewport(width=1280, height=720)),
        colors={"text": "rgb(20, 20, 20)"},
        typography={"font_family": "monospace"},
        spacing={"page_padding": "24px"},
        surfaces={"page": "rgb(0, 0, 0)"},
        component_patterns=[
            ComponentPattern(
                name="navigation",
                description="Primary navigation links.",
                evidence_refs=["element-0"],
            )
        ],
        interaction_states=[
            InteractionState(
                component_pattern="navigation",
                state="default",
                description="Navigation links use their default presentation.",
                evidence_refs=["element-0"],
            )
        ],
        layout_rules=[
            LayoutRule(
                name="Centered content",
                description="Main content is constrained to a readable column.",
                value="readable column",
                evidence_refs=["element-0"],
            )
        ],
    )


class FakeSandboxRunner:
    def __init__(self) -> None:
        self.request: Any | None = None

    def run(self, request: object, input_files: Mapping[str, bytes]) -> SandboxJobResult:
        self.request = request
        assert input_files == {}
        return SandboxJobResult(
            succeeded=True,
            exit_code=0,
            outputs={
                "page.json": b'{"title":"Example","viewport":{"width":1280,"height":720}}',
                "observations.json": b'{"observations":[{"id":"element-0"}]}',
                "screenshot.png": b"png-bytes",
            },
            logs="analyzer completed",
        )


class FakeSynthesizer:
    model_name = "test-model"

    def __init__(self, response: str, *, repaired_response: str | None = None) -> None:
        self.response = response
        self.repaired_response = repaired_response or response
        self.calls: list[dict[str, object]] = []
        self.repair_calls: list[dict[str, str]] = []

    def synthesize(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.response

    def repair(self, *, response: str, validation_error: str) -> str:
        self.repair_calls.append({"response": response, "validation_error": validation_error})
        return self.repaired_response


def test_analyzer_workflow_persists_validated_evidence_and_guide(tmp_path: Path) -> None:
    url = "https://www.mitravasu.com/"
    sandbox = FakeSandboxRunner()
    synthesizer = FakeSynthesizer(guide(url).model_dump_json())
    progress: list[str] = []
    workflow = AnalyzerWorkflow(
        store=RunStore(tmp_path),  # type: ignore[arg-type]
        sandbox_runner=sandbox,
        synthesizer=synthesizer,
        reporter=progress.append,
    )

    run = workflow.analyze(url)

    assert run.analysis.status is AnalysisStatus.COMPLETE
    assert run.analysis.model is not None
    assert run.analysis.model.name == "test-model"
    assert sandbox.request is not None
    assert synthesizer.calls[0]["screenshot"] == b"png-bytes"
    compact_observations = cast(Mapping[str, object], synthesizer.calls[0]["observations"])
    assert compact_observations["observation_count"] == 1
    assert progress == [
        "Validating public URL...",
        "Starting analyzer sandbox...",
        "Persisting analyzer evidence...",
        "Synthesizing style guide...",
        "Validating and persisting style guide...",
    ]
    persisted = (tmp_path / run.run_id / "analysis" / "style-guide.json").read_text()
    assert '"source_url": "https://www.mitravasu.com/"' in persisted
    assert (tmp_path / run.run_id / "analysis" / "style-guide.raw.txt").is_file()
    assert (tmp_path / run.run_id / "analysis" / "sandbox.log").is_file()


def test_analyzer_workflow_uses_one_canonical_url_when_input_omits_slash(
    tmp_path: Path,
) -> None:
    supplied_url = "https://www.mitravasu.com"
    canonical_url = "https://www.mitravasu.com/"
    sandbox = FakeSandboxRunner()
    synthesizer = FakeSynthesizer(guide(supplied_url).model_dump_json())
    workflow = AnalyzerWorkflow(
        store=RunStore(tmp_path),
        sandbox_runner=sandbox,
        synthesizer=synthesizer,
    )

    run = workflow.analyze(supplied_url)

    assert run.analysis.status is AnalysisStatus.COMPLETE
    assert run.source_url == canonical_url
    assert sandbox.request is not None
    assert sandbox.request.url == canonical_url
    assert synthesizer.calls[0]["source_url"] == canonical_url
    persisted = (tmp_path / run.run_id / "analysis" / "style-guide.json").read_text()
    assert f'"source_url": "{canonical_url}"' in persisted


def test_analyzer_workflow_marks_run_failed_when_the_guide_is_for_another_url(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandboxRunner()
    workflow = AnalyzerWorkflow(
        store=RunStore(tmp_path),  # type: ignore[arg-type]
        sandbox_runner=sandbox,
        synthesizer=FakeSynthesizer('{"colors": {}}'),
    )

    with pytest.raises(AnalyzerWorkflowError, match="does not match") as error:
        workflow.analyze("https://www.mitravasu.com/")

    failed = RunStore(tmp_path).load_run(error.value.run_id)
    assert failed.analysis.status is AnalysisStatus.FAILED
    assert (tmp_path / failed.run_id / "analysis" / "observations.json").is_file()
    assert (tmp_path / failed.run_id / "analysis" / "style-guide.raw.txt").is_file()
    assert (tmp_path / failed.run_id / "analysis" / "style-guide.repair.raw.txt").is_file()


def test_analyzer_workflow_repairs_once_and_keeps_both_model_responses(tmp_path: Path) -> None:
    url = "https://www.mitravasu.com/"
    synthesizer = FakeSynthesizer('{"colors": {}}', repaired_response=guide(url).model_dump_json())
    workflow = AnalyzerWorkflow(
        store=RunStore(tmp_path),  # type: ignore[arg-type]
        sandbox_runner=FakeSandboxRunner(),
        synthesizer=synthesizer,
    )

    run = workflow.analyze(url)

    assert run.analysis.status is AnalysisStatus.COMPLETE
    assert len(synthesizer.repair_calls) == 1
    assert "does not match the required schema" in synthesizer.repair_calls[0]["validation_error"]
    assert (tmp_path / run.run_id / "analysis" / "style-guide.raw.txt").is_file()
    assert (tmp_path / run.run_id / "analysis" / "style-guide.repair.raw.txt").is_file()
    assert (tmp_path / run.run_id / "analysis" / "style-guide.json").is_file()


def test_ollama_synthesizer_reports_a_truncated_single_line_response_preview() -> None:
    url = "https://www.mitravasu.com/"
    content = guide(url).model_dump_json(indent=2) + " " * 200
    client = SimpleNamespace(
        chat=lambda **_: SimpleNamespace(message=SimpleNamespace(content=content))
    )
    progress: list[str] = []

    synthesizer = OllamaStyleGuideSynthesizer(
        client, model_name="test-model", reporter=progress.append
    )
    response = synthesizer.synthesize(
        source_url=url,
        page={"title": "Example", "viewport": {"width": 1280, "height": 720}},
        observations={"observations": []},
        screenshot=b"png-bytes",
    )

    result = validate_style_guide_response(
        response,
        source_url=url,
        page={"title": "Example", "viewport": {"width": 1280, "height": 720}},
    )
    assert str(result.source_url) == url
    assert progress[-1].startswith("Style-guide model response: ")
    assert progress[-1].endswith("...")
    assert len(progress[-1]) == len("Style-guide model response: ") + 153


def test_ollama_synthesizer_accepts_a_json_code_fence() -> None:
    url = "https://www.mitravasu.com/"
    content = f"```json\n{guide(url).model_dump_json()}\n```"
    client = SimpleNamespace(
        chat=lambda **_: SimpleNamespace(message=SimpleNamespace(content=content))
    )
    synthesizer = OllamaStyleGuideSynthesizer(client, model_name="test-model")

    response = synthesizer.synthesize(
        source_url=url,
        page={"title": "Example", "viewport": {"width": 1280, "height": 720}},
        observations={"observation_count": 0},
        screenshot=b"png-bytes",
    )

    result = validate_style_guide_response(
        response,
        source_url=url,
        page={"title": "Example", "viewport": {"width": 1280, "height": 720}},
    )
    assert str(result.source_url) == url


def test_ollama_synthesizer_accepts_the_model_natural_guide_shape() -> None:
    content = json.dumps(
        {
            "schema_version": "1",
            "source_url": "https://model.example/",
            "capture": {"timestamp": "2026-07-25T00:00:00Z"},
            "colors": {"background": {"primary": "rgb(0, 0, 0)"}},
            "typography": {"font_families": [{"name": "monospace"}]},
            "spacing": {"layout": {"container_width": "1200px"}},
            "surfaces": {"page_background": "rgb(0, 0, 0)"},
            "component_patterns": [
                {
                    "name": "Navigation",
                    "description": "Top-level site links.",
                    "styles": {"display": "flex"},
                    "evidence_refs": ["element-1"],
                }
            ],
            "interaction_states": [
                {
                    "component_pattern": "Navigation",
                    "state": "default",
                    "description": "Navigation links use their default presentation.",
                    "styles": {"color": "rgb(255, 226, 189)"},
                    "evidence_refs": ["element-1"],
                }
            ],
            "layout_rules": [
                {
                    "name": "Centered content",
                    "description": "Main content uses a constrained column.",
                    "value": "constrained column",
                    "evidence_refs": ["element-1"],
                }
            ],
        }
    )
    client = SimpleNamespace(
        chat=lambda **_: SimpleNamespace(message=SimpleNamespace(content=content))
    )
    synthesizer = OllamaStyleGuideSynthesizer(client, model_name="test-model")

    response = synthesizer.synthesize(
        source_url="https://www.mitravasu.com/",
        page={"title": "Host title", "viewport": {"width": 1280, "height": 720}},
        observations={"observation_count": 0},
        screenshot=b"png-bytes",
    )

    result = validate_style_guide_response(
        response,
        source_url="https://www.mitravasu.com/",
        page={"title": "Host title", "viewport": {"width": 1280, "height": 720}},
    )
    assert str(result.source_url) == "https://www.mitravasu.com/"
    assert result.capture.title == "Host title"
    assert result.component_patterns[0].variants == []
    assert result.interaction_states[0].inferred is False


def test_style_guide_requires_one_fixed_shape_for_each_section() -> None:
    result = validate_style_guide_response(
        json.dumps(
            {
                "colors": {},
                "typography": {},
                "spacing": {},
                "surfaces": {},
                "component_patterns": [],
                "interaction_states": [
                    {
                        "component_pattern": "links",
                        "state": "default",
                        "description": "Interactive text uses the primary color.",
                        "evidence_refs": ["element-1"],
                    }
                ],
                "layout_rules": [
                    {
                        "name": "alignment",
                        "description": "Content aligns to its starting edge.",
                        "value": "start",
                        "evidence_refs": ["element-1"],
                    }
                ],
            }
        ),
        source_url="https://www.mitravasu.com/",
        page={"title": "Host title", "viewport": {"width": 1280, "height": 720}},
    )

    assert result.interaction_states[0].component_pattern == "links"
    assert result.layout_rules[0].value == "start"


def test_style_guide_json_schema_exposes_fixed_section_shapes() -> None:
    schema = StyleGuideContent.model_json_schema()

    assert schema["required"] == [
        "colors",
        "typography",
        "spacing",
        "surfaces",
        "component_patterns",
        "interaction_states",
        "layout_rules",
    ]
    assert schema["$defs"]["ComponentPattern"]["required"] == [
        "name",
        "description",
        "evidence_refs",
    ]
    assert schema["$defs"]["InteractionState"]["required"] == [
        "component_pattern",
        "state",
        "description",
        "evidence_refs",
    ]
    assert schema["$defs"]["LayoutRule"]["required"] == [
        "name",
        "description",
        "value",
        "evidence_refs",
    ]


def test_evidence_summary_uses_representative_observations_and_common_values() -> None:
    summary = summarize_observations(
        {
            "observations": [
                {
                    "id": "element-0",
                    "tag": "button",
                    "role": "button",
                    "visual_role": "button_like_link",
                    "text": "A" * 200,
                    "bounds": {"width": 20},
                    "styles": {"color": "rgb(1, 2, 3)"},
                },
                {
                    "id": "element-1",
                    "tag": "button",
                    "role": "button",
                    "visual_role": "button_like_link",
                    "text": "Duplicate",
                    "bounds": {"width": 20},
                    "styles": {"color": "rgb(1, 2, 3)"},
                },
            ]
        }
    )

    assert summary["observation_count"] == 2
    assert summary["role_counts"] == {"button": 2}
    assert summary["common_style_values"]["color"] == [{"value": "rgb(1, 2, 3)", "count": 2}]
    assert len(summary["representative_observations"]) == 1
    assert len(summary["representative_observations"][0]["text"]) == 160
    assert summary["representative_observations"][0]["visual_role"] == "button_like_link"
