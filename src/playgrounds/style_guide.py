"""Strict POC schema for model-synthesized single-page style guides."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class EvidenceBackedFact(BaseModel):
    """One observed or inferred guide claim tied to extracted evidence."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool


class ComponentFamily(BaseModel):
    """A reusable component pattern observed or inferred from the page."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    variants: list[str]
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool


class CaptureMetadata(BaseModel):
    """Identity of the browser capture that supplied the guide evidence."""

    model_config = ConfigDict(extra="forbid")

    title: str
    viewport_width: int = Field(gt=0)
    viewport_height: int = Field(gt=0)


class StyleGuide(BaseModel):
    """The small, evidence-backed POC guide consumed by the creator workflow."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    source_url: HttpUrl
    capture: CaptureMetadata
    colors: list[EvidenceBackedFact]
    typography: list[EvidenceBackedFact]
    spacing: list[EvidenceBackedFact]
    surfaces: list[EvidenceBackedFact]
    component_families: list[ComponentFamily]
    interaction_states: list[EvidenceBackedFact]
    layout_principles: list[EvidenceBackedFact]
