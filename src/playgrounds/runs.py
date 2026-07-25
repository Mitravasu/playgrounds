"""Atomic local persistence for POC analysis and creation runs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

RUN_SCHEMA_VERSION = 1
ANALYSIS_DIRECTORY = "analysis"
CREATIONS_DIRECTORY = "creations"
ANALYSIS_EVIDENCE = {
    "page.json": "application/json",
    "observations.json": "application/json",
    "screenshot.png": "image/png",
}


class AnalysisStatus(StrEnum):
    """The bounded lifecycle of POC page analysis."""

    PENDING = "pending"
    EVIDENCE_CAPTURED = "evidence_captured"
    COMPLETE = "complete"
    FAILED = "failed"


class CreationStatus(StrEnum):
    """The bounded lifecycle of one POC component generation."""

    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class ArtifactRecord(BaseModel):
    """Integrity metadata for one file held within a run directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value in {"", "."}:
            raise ValueError("artifact paths must be relative and must not contain '..'")
        return path.as_posix()


class ModelRecord(BaseModel):
    """Non-secret model provenance needed to reproduce an output."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


class AnalysisRecord(BaseModel):
    """The evidence and synthesized guide for the run's one source page."""

    model_config = ConfigDict(extra="forbid")

    status: AnalysisStatus = AnalysisStatus.PENDING
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    model: ModelRecord | None = None
    error: str | None = None


class CreationRecord(BaseModel):
    """Metadata that pins a generated component to its source style guide."""

    model_config = ConfigDict(extra="forbid")

    creation_id: str = Field(pattern=r"^creation_[0-9a-f]{32}$")
    created_at: datetime
    status: CreationStatus = CreationStatus.PENDING
    prompt: str = Field(min_length=1)
    style_guide_path: str
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    model: ModelRecord | None = None
    error: str | None = None

    @field_validator("style_guide_path")
    @classmethod
    def validate_style_guide_path(cls, value: str) -> str:
        return ArtifactRecord(
            path=value, media_type="application/json", bytes=0, sha256="0" * 64
        ).path


class RunRecord(BaseModel):
    """The complete local index for one POC source-page run."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = RUN_SCHEMA_VERSION
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    created_at: datetime
    updated_at: datetime
    source_url: str = Field(min_length=1)
    analysis: AnalysisRecord = Field(default_factory=AnalysisRecord)
    creations: list[CreationRecord] = Field(default_factory=list)


class RunStore:
    """Persist POC artifacts under one local root without partial JSON records."""

    def __init__(self, root: Path = Path("runs")) -> None:
        self._root = root

    def create_run(self, source_url: str) -> RunRecord:
        """Create an empty run before an analyzer job begins."""

        now = _utc_now()
        record = RunRecord(
            run_id=f"run_{uuid4().hex}",
            created_at=now,
            updated_at=now,
            source_url=source_url,
        )
        self._run_directory(record.run_id).mkdir(parents=True, exist_ok=False)
        self._write_record(record)
        return record

    def load_run(self, run_id: str) -> RunRecord:
        """Load and validate an existing run index."""

        path = self._run_directory(run_id) / "run.json"
        try:
            return RunRecord.model_validate_json(path.read_bytes())
        except FileNotFoundError as error:
            raise ValueError(f"run does not exist: {run_id}") from error

    def persist_analysis_evidence(self, run_id: str, artifacts: dict[str, bytes]) -> RunRecord:
        """Atomically save the sandbox's declared evidence artifacts."""

        if set(artifacts) != set(ANALYSIS_EVIDENCE):
            raise ValueError("analysis evidence must match the declared sandbox artifacts")
        record = self.load_run(run_id)
        if record.analysis.status is not AnalysisStatus.PENDING:
            raise ValueError("analysis evidence may only be persisted once")
        stored = self._write_artifacts(
            self._run_directory(run_id),
            ANALYSIS_DIRECTORY,
            artifacts,
            ANALYSIS_EVIDENCE,
        )
        record.analysis.status = AnalysisStatus.EVIDENCE_CAPTURED
        record.analysis.artifacts = stored
        return self._write_record(record)

    def persist_style_guide(
        self, run_id: str, content: bytes, *, model_name: str, prompt_version: str
    ) -> RunRecord:
        """Save the synthesized guide and its non-secret model provenance."""

        record = self.load_run(run_id)
        if record.analysis.status is not AnalysisStatus.EVIDENCE_CAPTURED:
            raise ValueError("style-guide synthesis requires captured analysis evidence")
        stored = self._write_artifacts(
            self._run_directory(run_id),
            ANALYSIS_DIRECTORY,
            {"style-guide.json": content},
            {"style-guide.json": "application/json"},
        )
        record.analysis.status = AnalysisStatus.COMPLETE
        record.analysis.artifacts.extend(stored)
        record.analysis.model = ModelRecord(name=model_name, prompt_version=prompt_version)
        return self._write_record(record)

    def persist_style_guide_response(self, run_id: str, content: str, *, attempt: int) -> RunRecord:
        """Keep an inspectable raw model response before schema validation."""

        if attempt not in {1, 2}:
            raise ValueError("style-guide response attempts must be 1 or 2")
        record = self.load_run(run_id)
        if record.analysis.status is not AnalysisStatus.EVIDENCE_CAPTURED:
            raise ValueError("raw style-guide responses require captured analysis evidence")
        filename = "style-guide.raw.txt" if attempt == 1 else "style-guide.repair.raw.txt"
        stored = self._write_artifacts(
            self._run_directory(run_id),
            ANALYSIS_DIRECTORY,
            {filename: content.encode()},
            {filename: "text/plain"},
        )
        record.analysis.artifacts.extend(stored)
        return self._write_record(record)

    def create_creation(self, run_id: str, prompt: str) -> CreationRecord:
        """Reserve a creation directory pinned to this run's style guide."""

        record = self.load_run(run_id)
        if record.analysis.status is not AnalysisStatus.COMPLETE:
            raise ValueError("component creation requires a completed style guide")
        creation = CreationRecord(
            creation_id=f"creation_{uuid4().hex}",
            created_at=_utc_now(),
            prompt=prompt,
            style_guide_path=f"{ANALYSIS_DIRECTORY}/style-guide.json",
        )
        (self._run_directory(run_id) / CREATIONS_DIRECTORY / creation.creation_id).mkdir(
            parents=True, exist_ok=False
        )
        record.creations.append(creation)
        self._write_record(record)
        return creation

    def mark_analysis_failed(self, run_id: str, error: str) -> RunRecord:
        """Record a bounded failure message without writing partial artifacts."""

        record = self.load_run(run_id)
        record.analysis.status = AnalysisStatus.FAILED
        record.analysis.error = error
        return self._write_record(record)

    def _write_artifacts(
        self,
        run_directory: Path,
        relative_directory: str,
        artifacts: dict[str, bytes],
        media_types: dict[str, str],
    ) -> list[ArtifactRecord]:
        destination = run_directory / relative_directory
        destination.mkdir(parents=True, exist_ok=True)
        records: list[ArtifactRecord] = []
        for name, content in artifacts.items():
            path = destination / name
            if path.exists():
                raise ValueError(f"artifact already exists: {relative_directory}/{name}")
            _atomic_write(path, content)
            records.append(
                ArtifactRecord(
                    path=f"{relative_directory}/{name}",
                    media_type=media_types[name],
                    bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                )
            )
        return records

    def _write_record(self, record: RunRecord) -> RunRecord:
        record.updated_at = _utc_now()
        _atomic_write(
            self._run_directory(record.run_id) / "run.json",
            json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True).encode() + b"\n",
        )
        return record

    def _run_directory(self, run_id: str) -> Path:
        RunRecord.model_validate(
            {
                "run_id": run_id,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "source_url": "validation-only",
            }
        )
        return self._root / run_id


def _atomic_write(path: Path, content: bytes) -> None:
    """Replace one file only after its complete replacement is durable on disk."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)
