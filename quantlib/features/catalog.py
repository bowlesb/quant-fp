"""Generate the feature catalog from the registry (``make feature-catalog``).

The catalog is the human/agent-readable surface (FEATURE_PLATFORM.md §5). It is generated, never
hand-edited; CI fails if the committed ``docs/FEATURES.md`` differs from the regenerated output.
"""
from __future__ import annotations

import sys
from pathlib import Path

from quantlib.features.registry import REGISTRY


def render_markdown() -> str:
    catalog = REGISTRY.catalog().sort(["group", "feature"])
    header = [
        "# Feature Catalog (generated — do not edit by hand; run `make feature-catalog`)",
        "",
        f"{catalog.height} features across {catalog['group'].n_unique()} group(s).",
        "",
        "| feature | group | type | layer | parity | dtype | nan_policy | valid_range | description |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    rows = [
        f"| `{row['feature']}` | {row['group']} | {row['type']} | {row['layer']} | "
        f"{row['parity_method']} | {row['dtype']} | {row['nan_policy']} | {row['valid_range']} | "
        f"{row['description']} |"
        for row in catalog.iter_rows(named=True)
    ]
    return "\n".join(header + rows) + "\n"


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/FEATURES.md")
    out.write_text(render_markdown())
    print(f"wrote {out} ({REGISTRY.catalog().height} features)")


if __name__ == "__main__":
    main()
