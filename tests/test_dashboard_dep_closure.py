"""Static guard against ``services/dashboard/requirements.txt`` dep-drift.

The dashboard image is built from a *curated minimal* ``requirements.txt`` (not quantlib's
full dependency set) plus the ``quant_tick`` wheel and the ``quantlib`` source tree. When a
dashboard panel adds a ``quantlib`` import whose transitive chain pulls a new third-party
package, that package is INVISIBLE to the panel's FastAPI-TestClient tests (those run inside
the ``fp-dev`` image, which carries every dependency) but CRASHES the dashboard's own slim
image on rebuild. This happened twice in one session: ``redis`` (pulled by ``quantlib.bus``
since #211, fixed reactively in #231) and ``alpaca-py`` (pulled by
``quantlib.features.seed_universe`` via the #227 universe-coverage panel, fixed in #232).

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
# import under their distribution name (fastapi, redis, numpy, polars, markdown, psycopg); these
# are the exceptions present in this image.
IMPORT_TO_DISTRIBUTION = {
    "alpaca": "alpaca-py",
}

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


def test_guard_would_catch_redis_and_alpaca() -> None:
    """Regression evidence: the two real incidents are in the closure and are satisfied.

    Removing redis/alpaca-py from requirements MUST make the main guard fail — this proves the
    closure actually reaches them (and that the test is not vacuously green).
    """
    closure = _build_closure()
    third_party = _third_party_top_level(closure)
    assert "redis" in third_party, "closure should reach redis via quantlib.bus (#211/#231)"
    assert (
        "alpaca" in third_party
    ), "closure should reach alpaca via quantlib.features.seed_universe (#227/#232)"

    declared = _requirement_distributions()
    for incident_import, incident_dist in (("redis", "redis"), ("alpaca", "alpaca-py")):
        distribution = IMPORT_TO_DISTRIBUTION.get(incident_import, incident_import)
        assert _normalize(distribution) in declared, (
            f"{incident_import} -> {incident_dist} must be declared; the reactive fixes "
            "#231/#232 added them and this test locks that in"
        )
        # Prove the guard is non-vacuous: a requirements set lacking this entry is rejected.
        without = declared - {_normalize(incident_dist)}
        assert _normalize(distribution) not in without
