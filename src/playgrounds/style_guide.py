"""POC schema separating host-owned capture facts from model interpretation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue


class ComponentPattern(BaseModel):
    """One reusable UI structure observed across the rendered page."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    styles: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool = False


class InteractionState(BaseModel):
    """One named interaction state belonging to a component pattern."""

    model_config = ConfigDict(extra="forbid")

    component_pattern: str = Field(min_length=1)
    state: str = Field(min_length=1)
    description: str = Field(min_length=1)
    styles: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool = False


class LayoutRule(BaseModel):
    """One page-level arrangement rule with a concrete value."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    value: JsonValue
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool = False


class StyleGuideContent(BaseModel):
    """The model-owned design interpretation for one analyzed page."""

    model_config = ConfigDict(extra="forbid")

    colors: dict[str, JsonValue]
    typography: dict[str, JsonValue]
    spacing: dict[str, JsonValue]
    surfaces: dict[str, JsonValue]
    component_patterns: list[ComponentPattern]
    interaction_states: list[InteractionState]
    layout_rules: list[LayoutRule]


class Viewport(BaseModel):
    """The host-observed browser viewport used for the capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    width: int = Field(gt=0)
    height: int = Field(gt=0)


class CaptureMetadata(BaseModel):
    """Host-observed identity of the browser capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    viewport: Viewport


class StyleGuide(StyleGuideContent):
    """The complete guide: trusted capture facts plus validated model inference."""

    schema_version: Literal[1] = 1
    source_url: HttpUrl
    capture: CaptureMetadata
