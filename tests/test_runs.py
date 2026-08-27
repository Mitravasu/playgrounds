import hashlib
import json
from pathlib import Path

import pytest

from playgrounds.runs import AnalysisStatus, RunStore


def evidence() -> dict[str, bytes]:
    return {
        "page.json": b'{"title":"Example"}',
        "observations.json": b'{"observations":[]}',
        "screenshot.png": b"png-bytes",
    }


def test_store_persists_evidence_and_style_guide_with_provenance(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    created = store.create_run("https://www.mitravasu.com/")

    captured = store.persist_analysis_evidence(created.run_id, evidence())
    completed = store.persist_style_guide(
        created.run_id,
        b'{"schema_version":1}',
        model_name="gemma4:cloud",
        prompt_version="style-guide-v1",
    )

    assert captured.analysis.status is AnalysisStatus.EVIDENCE_CAPTURED
    assert completed.analysis.status is AnalysisStatus.COMPLETE
    assert completed.analysis.model is not None
    assert completed.analysis.model.name == "gemma4:cloud"
    assert [artifact.path for artifact in completed.analysis.artifacts] == [
        "analysis/page.json",
        "analysis/observations.json",
        "analysis/screenshot.png",
        "analysis/style-guide.json",
    ]
    page = tmp_path / "runs" / created.run_id / "analysis" / "page.json"
    assert page.read_bytes() == evidence()["page.json"]
    assert completed.analysis.artifacts[0].sha256 == hashlib.sha256(page.read_bytes()).hexdigest()
    run_json = json.loads((tmp_path / "runs" / created.run_id / "run.json").read_text())
    assert run_json["source_url"] == "https://www.mitravasu.com/"
    assert "api_key" not in json.dumps(run_json)


def test_store_creates_a_creation_pinned_to_the_run_style_guide(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    run = store.create_run("https://www.mitravasu.com/")
    store.persist_analysis_evidence(run.run_id, evidence())
    store.persist_style_guide(run.run_id, b"{}", model_name="gemma4:cloud", prompt_version="v1")

    creation = store.create_creation(run.run_id, "Create an account menu.")

    assert creation.style_guide_path == "analysis/style-guide.json"
    assert (tmp_path / run.run_id / "creations" / creation.creation_id).is_dir()
    assert store.load_run(run.run_id).creations == [creation]


def test_store_rejects_out_of_order_or_duplicate_evidence(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    run = store.create_run("https://www.mitravasu.com/")

    with pytest.raises(ValueError, match="requires captured"):
        store.persist_style_guide(run.run_id, b"{}", model_name="gemma4:cloud", prompt_version="v1")
    with pytest.raises(ValueError, match="declared sandbox artifacts"):
        store.persist_analysis_evidence(run.run_id, {"page.json": b"{}"})

    store.persist_analysis_evidence(run.run_id, evidence())
    with pytest.raises(ValueError, match="only be persisted once"):
        store.persist_analysis_evidence(run.run_id, evidence())


def test_store_records_an_analysis_failure(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    run = store.create_run("https://www.mitravasu.com/")

    failed = store.mark_analysis_failed(run.run_id, "sandbox job exited unsuccessfully")

    assert failed.analysis.status is AnalysisStatus.FAILED
    assert failed.analysis.error == "sandbox job exited unsuccessfully"


def test_store_persists_a_bounded_sandbox_log_before_evidence(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    run = store.create_run("https://www.mitravasu.com/")

    recorded = store.persist_analysis_sandbox_log(run.run_id, "analyzer navigation failed")

    assert (tmp_path / run.run_id / "analysis" / "sandbox.log").read_text() == (
        "analyzer navigation failed"
    )
    assert recorded.analysis.artifacts[-1].path == "analysis/sandbox.log"
