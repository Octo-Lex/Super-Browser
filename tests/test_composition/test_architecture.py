"""Composition architecture gate — AST-level dependency rules (PR 1 P6).

One of the rare cases where a source-level test earns its keep: the
architectural boundary itself is the invariant. After P6, the façade and
the MCP server must reach native pages ONLY through ``NormalizedPage``;
the wrapper is the sole place allowed to adapt backend specifics.

The gate parses each module with ``ast`` and inspects real attribute
accesses — comments and docstrings cannot trip it.

Forbidden:
- ``agent/facade.py``: ``.backend_page`` access, ``.query_selector``,
  ``engine_page.cdp``, the dead ``_current_frame``.
- ``mcp_server.py``: ``.backend_page`` access, direct ``wait_for_*`` calls.

Allowed: ``EnginePage`` protocol operations (upload/download/route/frame
locators/evaluate) — those are the portable contract, not leaks.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "super_browser"


def _attribute_names(source: str) -> set[tuple[str, str]]:
    """All (base_name, attribute) pairs accessed in the module."""
    tree = ast.parse(source)
    pairs: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            pairs.add((node.value.id, node.attr))
    return pairs


def _function_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_facade_has_no_native_page_dependencies() -> None:
    src = (_SRC / "agent" / "facade.py").read_text(encoding="utf-8")
    accesses = _attribute_names(src)

    offenders = {attr for _, attr in accesses if attr == "backend_page"}
    assert not offenders, f"facade reaches .backend_page: {sorted(offenders)}"
    assert "query_selector" not in {attr for _, attr in accesses}, (
        "facade must use NormalizedPage.selector_bounds"
    )
    assert ("engine_page", "cdp") not in accesses, (
        "facade must use NormalizedPage.cdp, not engine_page.cdp"
    )
    assert "_current_frame" not in _function_names(src), (
        "the dead raw-page path was removed in P6"
    )


def test_mcp_server_has_no_native_page_dependencies() -> None:
    src = (_SRC / "mcp_server.py").read_text(encoding="utf-8")
    accesses = _attribute_names(src)

    offenders = {attr for _, attr in accesses if attr == "backend_page"}
    assert not offenders, f"mcp_server reaches .backend_page: {sorted(offenders)}"
    for native_call in (
        "wait_for_selector",
        "wait_for_function",
        "wait_for_url",
        "wait_for_load_state",
    ):
        assert native_call not in {attr for _, attr in accesses}, (
            f"mcp_server must not call .{native_call} — use NormalizedPage.wait_for"
        )


def test_tabs_module_has_no_raw_page_ownership() -> None:
    src = (_SRC / "browser" / "tabs.py").read_text(encoding="utf-8")
    accesses = _attribute_names(src)
    assert not {attr for _, attr in accesses if attr == "new_cdp_session"}
    tree = ast.parse(src)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    assert not any("context" in name.lower() for name in imported_modules), (
        "TabManager must not import a browser context — it owns the engine"
    )


def test_normalized_page_is_the_adapter_boundary() -> None:
    """The positive side of the gate: the wrapper owns the adaptation."""
    src = (_SRC / "browser" / "page.py").read_text(encoding="utf-8")
    functions = _function_names(src)
    for surface in (
        "attach_diagnostics",
        "selector_bounds",
        "wait_for",
        "reload",
        "go_back",
        "go_forward",
    ):
        assert surface in functions, f"NormalizedPage missing adapter surface: {surface}"
