from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from playgrounds.analyzer_workflow import AnalyzerWorkflow, AnalyzerWorkflowError
from playgrounds.runs import AnalysisStatus, RunStore
from playgrounds.sandbox import SandboxJobResult
from playgrounds.style_guide import (
    CaptureMetadata,
    ComponentFamily,
    EvidenceBackedFact,
    StyleGuide,
)


def guide(url: str) -> StyleGuide:
    fact = EvidenceBackedFact(
        name="primary text",
        value="rgb(20, 20, 20)",
        evidence_refs=["element-0"],
        inferred=False,
    )
    return StyleGuide(
        source_url=url,
        capture=CaptureMetadata(title="Example", viewport_width=1280, viewport_height=720),
        colors=[fact],
        typography=[fact],
        spacing=[fact],
        surfaces=[fact],
        component_families=[
            ComponentFamily(
                name="navigation",
                description="Primary navigation links.",
                variants=[],
                evidence_refs=["element-0"],
                inferred=False,
            )
        ],
        interaction_states=[fact],
        layout_principles=[fact],
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
            logs="",
        )


class FakeSynthesizer:
    model_name = "test-model"

    def __init__(self, result: StyleGuide) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def synthesize(self, **kwargs: object) -> StyleGuide:
        self.calls.append(kwargs)
        return self.result


def test_analyzer_workflow_persists_validated_evidence_and_guide(tmp_path: Path) -> None:
    url = "https://www.mitravasu.com/"
    sandbox = FakeSandboxRunner()
    synthesizer = FakeSynthesizer(guide(url))
    workflow = AnalyzerWorkflow(
        store=RunStore(tmp_path),  # type: ignore[arg-type]
        sandbox_runner=sandbox,
        synthesizer=synthesizer,
    )

    run = workflow.analyze(url)

    assert run.analysis.status is AnalysisStatus.COMPLETE
    assert run.analysis.model is not None
    assert run.analysis.model.name == "test-model"
    assert sandbox.request is not None
    assert synthesizer.calls[0]["screenshot"] == b"png-bytes"
    persisted = (tmp_path / run.run_id / "analysis" / "style-guide.json").read_text()
    assert '"source_url": "https://www.mitravasu.com/"' in persisted


def test_analyzer_workflow_marks_run_failed_when_the_guide_is_for_another_url(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandboxRunner()
    workflow = AnalyzerWorkflow(
        store=RunStore(tmp_path),  # type: ignore[arg-type]
        sandbox_runner=sandbox,
        synthesizer=FakeSynthesizer(guide("https://www.mitravasu.com/other")),
    )

    with pytest.raises(AnalyzerWorkflowError, match="does not match") as error:
        workflow.analyze("https://www.mitravasu.com/")

    failed = RunStore(tmp_path).load_run(error.value.run_id)
    assert failed.analysis.status is AnalysisStatus.FAILED
    assert (tmp_path / failed.run_id / "analysis" / "observations.json").is_file()
