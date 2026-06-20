"""Per-group INFO for the dashboard's group detail panel — registry-derived + the optional curated guide.

The detail panel (hover → "more detail") shows, per feature group:
  * WHAT it is — the group class docstring (registry) + the curated ``purpose``.
  * HOW its features differ — the per-feature ``description`` list (registry catalog; authored for all 728
    features) + the curated ``how_features_differ``.
  * WHY we compute it — the curated ``value`` (hypothesis / measure / honest test-status), if written.
  * an optional code ``example``.

The registry half is free and complete for ALL groups today. The curated half lives in
``docs/feature_group_guide.yaml`` (one optional entry per group); groups without an entry are returned with
``guide: null`` so the panel honestly says "guide entry not yet written" rather than fabricating a rationale.

This module is imported by ``store_grid`` at BUILD time: the worker bakes ``group_info`` into the matrix
document, so the dashboard serves it with the grid (zero request-path cost). Read-side only.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml
from feature_grid import _catalog_by_group

from quantlib.features.registry import REGISTRY

# The curated guide. Baked into the image at /app/feature_group_guide.yaml (see the Dockerfile); env-overridable
# so a test or a future mount can point elsewhere. Absent file -> empty guide (all groups honestly un-narrated).
GUIDE_PATH = Path(os.environ.get("FEATURE_GROUP_GUIDE_PATH", "/app/feature_group_guide.yaml"))


def _load_guide(path: Path = GUIDE_PATH) -> dict[str, dict[str, str]]:
    """Load the curated per-group guide YAML. Missing/empty/malformed -> {} (the registry half still serves)."""
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, value in loaded.items():
        if isinstance(value, dict):
            out[str(key)] = {str(field): str(text) for field, text in value.items() if text is not None}
    return out


def _group_docstrings() -> dict[str, str]:
    """{group_name: docstring} — the engineering "what it is" per group. Each group lives in its own module
    whose MODULE docstring describes the family (the class itself usually has none), so we read the module's
    ``__doc__`` (falling back to the class docstring if a module ever lacks one)."""
    out: dict[str, str] = {}
    for group in REGISTRY.groups():
        module = sys.modules.get(type(group).__module__)
        doc = (getattr(module, "__doc__", None) or type(group).__doc__ or "").strip()
        out[group.name] = doc
    return out


def build_group_info() -> dict[str, dict[str, Any]]:
    """{group_name: info} for every registry group, merging the registry-derived content with the curated
    guide. The info shape the detail panel consumes:

      {docstring, type, layer, n_features,
       features: [{name, description}],          # the per-feature differences, authored for all features
       guide: {purpose, how_features_differ, value, example} | null}   # curated; null when not yet written
    """
    catalog_by_group = _catalog_by_group()
    docstrings = _group_docstrings()
    guide = _load_guide(GUIDE_PATH)  # read the live module-level path (tests/env may override it)

    info: dict[str, dict[str, Any]] = {}
    for group, records in catalog_by_group.items():
        features = [
            {"name": str(record["feature"]), "description": str(record.get("description", "") or "")}
            for record in records
        ]
        first = records[0] if records else {}
        info[group] = {
            "docstring": docstrings.get(group, ""),
            "type": str(first.get("type", "") or ""),
            "layer": str(first.get("layer", "") or ""),
            "n_features": len(features),
            "features": features,
            "guide": guide.get(group),  # None when no curated entry — the panel stubs it honestly
        }
    return info
