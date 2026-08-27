"""Compact deterministic evidence before it enters a model context."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

MAX_REPRESENTATIVE_OBSERVATIONS = 40
MAX_TEXT_CHARACTERS = 160


def summarize_observations(observations: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve recurring visual evidence without sending every DOM observation."""

    raw_observations = observations.get("observations")
    if not isinstance(raw_observations, list):
        raise TypeError("observations.json must contain an observations list")

    roles: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    style_values: dict[str, Counter[str]] = {}
    representatives: list[dict[str, Any]] = []
    seen_patterns: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    for item in raw_observations:
        if not isinstance(item, dict):
            continue
        tag = item.get("tag")
        role = item.get("role")
        visual_role = item.get("visual_role")
        styles = item.get("styles")
        if isinstance(tag, str):
            tags[tag] += 1
        if isinstance(role, str):
            roles[role] += 1
        if not isinstance(styles, dict):
            styles = {}
        normalized_styles = {
            name: value
            for name, value in styles.items()
            if isinstance(name, str) and isinstance(value, str)
        }
        for name, value in normalized_styles.items():
            style_values.setdefault(name, Counter())[value] += 1
        pattern = (
            tag if isinstance(tag, str) else "unknown",
            role if isinstance(role, str) else "",
            visual_role if isinstance(visual_role, str) else "",
            tuple(sorted(normalized_styles.items())),
        )
        if len(representatives) >= MAX_REPRESENTATIVE_OBSERVATIONS or pattern in seen_patterns:
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str):
            continue
        seen_patterns.add(pattern)
        text = item.get("text")
        representatives.append(
            {
                "id": identifier,
                "tag": tag,
                "role": role,
                "visual_role": visual_role,
                "text": text[:MAX_TEXT_CHARACTERS] if isinstance(text, str) else "",
                "bounds": item.get("bounds"),
                "styles": normalized_styles,
            }
        )

    return {
        "schema_version": 1,
        "observation_count": len(raw_observations),
        "role_counts": dict(sorted(roles.items())),
        "tag_counts": dict(sorted(tags.items())),
        "common_style_values": {
            name: [{"value": value, "count": count} for value, count in values.most_common(8)]
            for name, values in sorted(style_values.items())
        },
        "representative_observations": representatives,
    }
