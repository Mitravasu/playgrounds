"""POC schema separating host-owned capture facts from model interpretation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, JsonValue, model_validator


class ComponentFamily(BaseModel):
    """One reusable component pattern inferred from rendered evidence."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    variants: list[str] = Field(default_factory=list)
    styles: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool = False


class InteractionState(BaseModel):
    """An observed or inferred state for a component family."""

    model_config = ConfigDict(extra="forbid")

    component: str | None = Field(default=None, min_length=1)
    state: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, min_length=1)
    styles: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool = False

    @model_validator(mode="after")
    def validate_identity(self) -> "InteractionState":
        if (self.component and self.state) or (self.name and self.description):
            return self
        raise ValueError("interaction states require component/state or a name/description pair")


class LayoutPrinciple(BaseModel):
    """A concise rule describing an observed layout pattern."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str | None = Field(default=None, min_length=1)
    value: JsonValue | None = None
    evidence_refs: list[str] = Field(min_length=1)
    inferred: bool = False

    @model_validator(mode="after")
    def validate_explanation(self) -> "LayoutPrinciple":
        if self.description is not None or self.value is not None:
            return self
        raise ValueError("layout principles require a description or value")


class StyleGuideContent(BaseModel):
    """The model-owned design interpretation for one analyzed page."""

    model_config = ConfigDict(extra="forbid")

    colors: dict[str, JsonValue]
    typography: dict[str, JsonValue]
    spacing: dict[str, JsonValue]
    surfaces: dict[str, JsonValue]
    component_families: list[ComponentFamily]
    interaction_states: list[InteractionState]
    layout_principles: list[LayoutPrinciple]


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
