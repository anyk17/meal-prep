"""Enforce the layered dependency rule documented in docs/ARCHITECTURE.md."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = "mealplanner"
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / PACKAGE

# layer -> layers it may import (itself is always allowed)
ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "application": {"domain"},
    "adapters": {"domain", "application"},
    "entrypoints": {"domain", "application", "adapters", "entrypoints"},
}

# Third-party / stdlib modules the domain layer must not touch (I/O, frameworks, solver).
DOMAIN_FORBIDDEN = {
    "flask",
    "sqlite3",
    "yaml",
    "ortools",
    "pathlib",
    "os",
    "shutil",
    "tempfile",
    "io",
    "glob",
    "fnmatch",
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "http",
}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _layer(module: str) -> str | None:
    parts = module.split(".")
    if parts[0] != PACKAGE or len(parts) < 2:
        return None
    return parts[1]


def _imports(path: Path) -> list[tuple[int, str]]:
    """Return (line, absolute module) for every import, resolving relative imports."""
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                target = f"{prefix}.{node.module}" if node.module else prefix
                if not node.module:  # "from . import x" imports submodules
                    found.extend((node.lineno, f"{target}.{a.name}") for a in node.names)
                    continue
            else:
                target = node.module or ""
            found.append((node.lineno, target))
    return found


def _modules() -> list[Path]:
    return sorted(PACKAGE_ROOT.rglob("*.py"))


def test_every_layer_is_known() -> None:
    layers = {
        _layer(_module_name(p)) for p in _modules() if _layer(_module_name(p)) is not None
    }
    assert layers <= set(ALLOWED), f"unknown top-level subpackages: {sorted(layers - set(ALLOWED))}"


def test_layer_dependency_rule() -> None:
    violations = []
    for path in _modules():
        module = _module_name(path)
        source_layer = _layer(module)
        if source_layer is None:
            continue
        for line, target in _imports(path):
            target_layer = _layer(target)
            if target_layer is None or target_layer == source_layer:
                continue
            if target_layer not in ALLOWED[source_layer]:
                violations.append(
                    f"{path.relative_to(PACKAGE_ROOT.parent)}:{line}: layer '{source_layer}' "
                    f"must not import '{target}' (layer '{target_layer}'); "
                    f"allowed: {sorted(ALLOWED[source_layer]) or 'none'}"
                )
    assert not violations, "Forbidden layer imports:\n" + "\n".join(violations)


def test_domain_has_no_io_or_framework_imports() -> None:
    violations = []
    for path in sorted((PACKAGE_ROOT / "domain").rglob("*.py")):
        for line, target in _imports(path):
            root = target.split(".")[0]
            if root in DOMAIN_FORBIDDEN:
                violations.append(
                    f"{path.relative_to(PACKAGE_ROOT.parent)}:{line}: domain must not import '{target}'"
                )
    assert not violations, "Domain purity violations:\n" + "\n".join(violations)


def test_only_adapters_import_ortools() -> None:
    """The OR-Tools engine is an adapter behind the PlanGenerator port."""
    violations = []
    for path in _modules():
        layer = _layer(_module_name(path))
        for line, target in _imports(path):
            if target.split(".")[0] == "ortools" and not (
                layer == "adapters" and "ortools" in path.relative_to(PACKAGE_ROOT).parts[:2]
            ):
                violations.append(f"{path.relative_to(PACKAGE_ROOT.parent)}:{line}: imports '{target}'")
    assert not violations, "ortools may only be imported by adapters/ortools:\n" + "\n".join(violations)


def test_application_and_domain_are_solver_free() -> None:
    for layer in ("domain", "application"):
        for path in sorted((PACKAGE_ROOT / layer).rglob("*.py")):
            for line, target in _imports(path):
                assert not target.startswith("mealplanner.adapters"), f"{path}:{line} imports {target}"
                assert target.split(".")[0] != "ortools", f"{path}:{line} imports {target}"


def test_import_resolver_handles_relative_imports(tmp_path: Path) -> None:
    sample = PACKAGE_ROOT / "adapters" / "sqlite" / "store.py"
    targets = {t for _, t in _imports(sample)}
    assert "mealplanner.application.errors" in targets
    assert "mealplanner.adapters.sqlite.migrations" in targets
