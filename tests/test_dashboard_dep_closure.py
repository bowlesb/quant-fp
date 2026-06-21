"""Static guard against ``services/dashboard/requirements.txt`` dep-drift.

The dashboard image is built from a *curated minimal* ``requirements.txt`` (not quantlib's
full dependency set) plus the ``quant_tick`` wheel and the ``quantlib`` source tree. When a
dashboard panel adds a ``quantlib`` import whose transitive chain pulls a new third-party
package, that package is INVISIBLE to the panel's FastAPI-TestClient tests (those run inside
the ``fp-dev`` image, which carries every dependency) but CRASHES the dashboard's own slim
image on rebuild. This happened twice in one session: ``redis`` (pulled by ``quantlib.bus``
since #211, fixed reactively in #231) and ``alpaca-py`` (pulled by
``quantlib.features.seed_universe`` via the #227 universe-coverage panel, fixed in #232).
The alpaca pull was later removed at the root by decoupling the panel from the trading SDK
(KEEP_EXCHANGES moved to the pure ``quantlib.universe`` module), so ``alpaca-py`` is no longer
in the dashboard requirements. The redis pull is also gone now: it came only through the old
``scorecard`` ops route's ``quantlib.bus`` import, which was deleted when the dashboard was pared
to the grid-only surface. The non-vacuousness check therefore anchors on ``pymongo`` — the grid's
real, still-present MongoDB-client dependency (imported directly by ``store_grid_cache``).

This test reproduces what the dashboard image actually carries WITHOUT a docker build: it walks
the static import closure of every ``services/dashboard/*.py`` module (following first-party
``quantlib`` / sibling-module / ``quant_tick`` edges transitively via the AST, never executing
code so no dependency need be installed), collects every third-party top-level package the
closure imports, and asserts each one is satisfiable by ``requirements.txt`` or the baked-in
``quant_tick`` wheel. It would have caught both ``redis`` and ``alpaca-py``.

Pure-AST, no docker build, no installed deps — runs in the normal suite.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = REPO_ROOT / "services" / "dashboard"
REQUIREMENTS_TXT = DASHBOARD_DIR / "requirements.txt"

# The compiled Rust kernel: shipped to the image as a wheel built in the Dockerfile's rust
# stage (NOT a PyPI line in requirements.txt). Its import name is ``quant_tick``.
WHEEL_TOP_LEVEL = "quant_tick"

# Import-name -> requirements distribution-name, for the cases where they differ. Most packages
# import under their distribution name (fastapi, redis, numpy, polars, psycopg). The exception:
# ``import yaml`` is provided by the ``pyyaml`` distribution (the group-detail-panel guide parser).
IMPORT_TO_DISTRIBUTION: dict[str, str] = {"yaml": "pyyaml"}

# Packages the dashboard imports DIRECTLY but does not list in requirements.txt because they are
# HARD, always-installed dependencies of a package that IS listed — so the image always carries
# them. Each entry names the declared parent that guarantees its presence; the guard treats the
# import as satisfied only while that parent stays declared. This is deliberately tiny: it covers
# only packages the closure actually reaches, and it does NOT mask the incident class (redis and
# alpaca-py were undeclared because NOTHING transitively pulled them — they are absent here).
TRANSITIVELY_GUARANTEED = {
    "pydantic": "fastapi",  # fastapi has a hard runtime dependency on pydantic
}


def _requirement_distributions() -> set[str]:
    """Top-level distribution names declared in the dashboard requirements file.

    Strips version specifiers and extras (e.g. ``uvicorn[standard]==0.34.0`` -> ``uvicorn``,
    ``psycopg[binary]==3.2.3`` -> ``psycopg``). Normalizes to lowercase; underscores and dots
    fold to dashes per PEP 503 so ``alpaca-py`` / ``alpaca_py`` compare equal.
    """
    distributions: set[str] = set()
    for raw_line in REQUIREMENTS_TXT.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("==")[0].split(">=")[0].split("<=")[0]
        name = name.split(">")[0].split("<")[0].split("~=")[0].split("!=")[0]
        name = name.split("[")[0].strip()
        distributions.add(_normalize(name))
    return distributions


def _normalize(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _module_to_path(module: str) -> Path | None:
    """Resolve a first-party module name to its source file, or None if it is not first-party.

    First-party = a sibling dashboard module (``services/dashboard/<name>.py``) or any
    ``quantlib`` submodule (``quantlib/...``). Returns the package ``__init__.py`` for a package
    import so the package's own import side effects are walked too.
    """
    parts = module.split(".")
    if parts[0] == "quantlib":
        base = REPO_ROOT.joinpath(*parts)
        candidate = base.with_suffix(".py")
        if candidate.is_file():
            return candidate
        init = base / "__init__.py"
        if init.is_file():
            return init
        # Partial-path package (e.g. ``quantlib.bus.schema`` where ``schema`` is a module under
        # the ``bus`` package): the leaf may be a module while the path above is a package.
        return None
    # A bare top-level name that matches a sibling dashboard module file.
    dashboard_module = DASHBOARD_DIR / f"{parts[0]}.py"
    if "." not in module and dashboard_module.is_file():
        return dashboard_module
    return None


def _package_init_chain(module: str) -> list[Path]:
    """Every ``__init__.py`` run when importing ``module``, plus the leaf module file.

    Importing ``quantlib.bus.schema`` executes ``quantlib/__init__.py``,
    ``quantlib/bus/__init__.py`` (this is where the redis-pulling ``quantlib.bus`` chain lives)
    and finally ``quantlib/bus/schema.py``. The closure walk must follow ALL of these to be
    faithful to what the interpreter actually imports.
    """
    parts = module.split(".")
    if parts[0] != "quantlib":
        single = _module_to_path(module)
        return [single] if single is not None else []
    paths: list[Path] = []
    for depth in range(1, len(parts) + 1):
        base = REPO_ROOT.joinpath(*parts[:depth])
        init = base / "__init__.py"
        if init.is_file():
            paths.append(init)
        leaf = base.with_suffix(".py")
        if depth == len(parts) and leaf.is_file():
            paths.append(leaf)
    return paths


def _imported_modules(source_path: Path) -> set[str]:
    """Top-level-qualified module names imported by a source file (via AST, no execution)."""
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # level>0 is a relative import; the codebase forbids them, but resolve defensively.
            if node.level == 0 and node.module is not None:
                modules.add(node.module)
    return modules


def _build_closure() -> set[str]:
    """All module names reachable from the dashboard's modules, transitively.

    Walks first-party edges (quantlib / sibling dashboard modules) into their source and keeps
    going; records third-party module names as leaves (their internals are not on disk here).
    """
    seen_files: set[Path] = set()
    all_modules: set[str] = set()

    seeds = sorted(path for path in DASHBOARD_DIR.glob("*.py") if path.name != "__init__.py")
    pending: list[Path] = list(seeds)
    seen_files.update(seeds)

    while pending:
        current = pending.pop()
        for module in _imported_modules(current):
            all_modules.add(module)
            for first_party_path in _package_init_chain(module):
                if first_party_path not in seen_files:
                    seen_files.add(first_party_path)
                    pending.append(first_party_path)
    return all_modules


def _third_party_top_level(modules: set[str]) -> set[str]:
    """Top-level package names that are neither stdlib nor first-party."""
    stdlib = sys.stdlib_module_names
    dashboard_modules = {path.stem for path in DASHBOARD_DIR.glob("*.py") if path.name != "__init__.py"}
    third_party: set[str] = set()
    for module in modules:
        top = module.split(".")[0]
        if top in stdlib or top.startswith("_"):
            continue
        if top == "quantlib" or top in dashboard_modules:
            continue
        third_party.add(top)
    return third_party


def test_requirements_file_parses() -> None:
    """Sanity: the requirements file exists and yields a non-trivial distribution set."""
    distributions = _requirement_distributions()
    assert "fastapi" in distributions
    assert len(distributions) >= 5


def test_dashboard_import_closure_is_satisfied_by_requirements() -> None:
    """Every third-party package the dashboard imports must be in requirements (or the wheel).

    This is the guard: a new panel that imports a quantlib module pulling an undeclared
    third-party package fails here, instead of crash-looping the dashboard image on rebuild.
    """
    declared = _requirement_distributions()
    closure = _build_closure()
    third_party = _third_party_top_level(closure)

    missing: list[str] = []
    for import_name in sorted(third_party):
        if import_name == WHEEL_TOP_LEVEL:
            continue  # supplied by the quant_tick wheel built in the Dockerfile
        guarantor = TRANSITIVELY_GUARANTEED.get(import_name)
        if guarantor is not None and _normalize(guarantor) in declared:
            continue  # present as a hard transitive dep of a declared parent
        distribution = IMPORT_TO_DISTRIBUTION.get(import_name, import_name)
        if _normalize(distribution) not in declared:
            missing.append(f"{import_name} (distribution: {distribution})")

    assert not missing, (
        "Dashboard imports third-party packages NOT in services/dashboard/requirements.txt "
        "(or the quant_tick wheel). The dashboard image will crash-loop on rebuild. Add the "
        f"missing distribution(s) to requirements.txt: {missing}"
    )


def test_guard_is_non_vacuous_on_pymongo() -> None:
    """Regression evidence: the guard actually reaches a real third-party dep and enforces it.

    ``pymongo`` is imported directly by ``store_grid_cache`` — the dashboard reads the always-warm coverage
    grid out of the dedicated MongoDB service, so the client is a hard requirement of every grid route. It MUST
    be in the closure and declared, and a requirements set lacking it MUST fail the main guard — proving the
    test is not vacuously green.

    (This anchor used to be ``redis``, pulled only by the old ``scorecard`` ops route via ``quantlib.bus``;
    once the dashboard was pared to the grid-only surface that import vanished and ``redis`` was dropped from
    requirements, so the non-vacuity anchor moved to ``pymongo``, which the grid genuinely needs.)
    """
    closure = _build_closure()
    third_party = _third_party_top_level(closure)
    assert "pymongo" in third_party, "closure should reach pymongo via store_grid_cache"

    declared = _requirement_distributions()
    assert _normalize("pymongo") in declared, "pymongo must be declared in requirements.txt"
    # Prove the guard is non-vacuous: a requirements set lacking this entry is rejected.
    without = declared - {_normalize("pymongo")}
    assert _normalize("pymongo") not in without


def test_universe_coverage_panel_does_not_pull_alpaca() -> None:
    """The #227 universe-coverage panel must NOT drag the Alpaca trading SDK into the dashboard.

    The panel originally imported ``KEEP_EXCHANGES`` from ``quantlib.features.seed_universe``, whose
    module-level ``alpaca.trading`` import crash-looped the dashboard image (#232, reactive add). The
    decouple moved the constant to the pure ``quantlib.universe`` module, so a monitoring UI no longer
    bundles the trading SDK. This locks the decouple in: ``alpaca`` must stay OUT of the closure and
    ``alpaca-py`` must stay OUT of requirements.txt.
    """
    closure = _build_closure()
    third_party = _third_party_top_level(closure)
    assert "alpaca" not in third_party, (
        "alpaca must NOT be in the dashboard import closure — universe_coverage gets KEEP_EXCHANGES "
        "from the pure quantlib.universe module, not the alpaca-importing seed_universe"
    )
    declared = _requirement_distributions()
    assert _normalize("alpaca-py") not in declared, (
        "alpaca-py must NOT be in dashboard requirements.txt — the dashboard no longer needs the "
        "trading SDK after the universe_coverage decouple"
    )
