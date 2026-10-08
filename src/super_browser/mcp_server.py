"""MCP server exposing SuperBrowser over stdio using a four-tier tool model.

This is a *tested, permissioned* restoration of the MCP server that was
deleted in ``a370cf9`` ("untested, 290 lines"). The deleted server shipped
10 tools including 6 side-effecting ones with no permission model; the current
server partitions its surface into tiers and gates each appropriately.

Default tools: inspect (browser_status, current_url, observe, extract_text,
screenshot, list_tabs) + navigation (navigate, wait_for).

Action tools (scroll, press_key, click, fill, open_tab, close_tab):
hidden unless action mode is enabled via --allow-actions or
SB_MCP_ALLOW_ACTIONS=1|true|yes|on.

Navigation is always security-checked (injection detection and secret
redaction by default; domain allow/block lists when
SB_MCP_DOMAIN_ALLOWLIST or SB_MCP_DOMAIN_BLOCKLIST is set). It does not
consume the action budget.

Still excluded: download, upload, act, arbitrary JS execution.

Run via:
    python -m super_browser.mcp_server
    superbrowser-mcp [--allow-actions]
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger("super_browser.mcp")

# ============================================================================
# Tool schema definitions — read-only tools
# ============================================================================

PHASE1_TOOLS: list[types.Tool] = [
    types.Tool(
        name="browser_status",
        description="Get the MCP browser runtime status: running state, backend, and session health. Safe to call before the browser has started.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="current_url",
        description="Get the current page URL only. Returns a structured 'not started' state if the browser has not been started; does not force a lazy launch.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="observe",
        description="Get the current page state: URL, title, count of interactive elements, and total element count. Starts the browser lazily on first call.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="extract_text",
        description="Extract text content from the page, optionally scoped to a CSS selector. Starts the browser lazily on first call.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text query or empty to extract all text."},
                "selector": {"type": "string", "description": "Optional CSS selector to scope extraction."},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="screenshot",
        description=(
            "Take a screenshot of the current page. Captures the rendered viewport, "
            "or the full page when full_page=true, as the browser sees it. "
            "By default returns a lossless PNG; request format=jpeg with a quality "
            "value to produce a smaller image that fits under host inline limits "
            "(typical hosts inline images up to ~200 KiB). "
            "Starts the browser lazily on first call."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "full_page": {
                    "type": "boolean",
                    "description": "Capture the full scrollable page (default: viewport only).",
                    "default": False,
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpeg"],
                    "description": "Output format (default: png). Use jpeg with a quality value for smaller images.",
                    "default": "png",
                },
                "quality": {
                    "type": "integer",
                    "description": "JPEG quality 1-100. Only valid when format=jpeg; rejected for PNG.",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="list_tabs",
        description="List all open browser tabs. Starts the browser lazily on first call.",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    types.Tool(
        name="extract_image_text",
        description=(
            "Extract text from page screenshots via OCR (Tesseract). Reads text "
            "embedded in images — flyer/catalog sites, product photos, scanned "
            "content — that extract_text and observe cannot see. Inspect-tier; "
            "no page mutation. Requires the [vision] extra (pytesseract + Pillow) "
            "and a system Tesseract binary; returns a structured ocr_unavailable "
            "error if not installed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": "CSS selector of an image/element to OCR. Mutually exclusive with bounds.",
                },
                "bounds": {
                    "type": "object",
                    "description": "Bounding box {x, y, width, height} of the screen region to OCR. Mutually exclusive with selector.",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "full_page": {
                    "type": "boolean",
                    "description": "OCR the full scrollable page (default: viewport only).",
                    "default": False,
                },
                "language": {
                    "type": "string",
                    "description": "Tesseract language code(s), e.g. 'eng', 'ara', 'eng+ara'. Default: 'eng'.",
                    "default": "eng",
                },
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum word confidence 0.0-1.0; words below this are filtered out. Default: 0.0 (keep all).",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.0,
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="analyze_image",
        description=(
            "Answer a natural-language question about a page screenshot via a "
            "vision-LLM provider (semantic visual QA — e.g. 'which card shows "
            "the cheapest milk?', 'describe the layout'). Unlike OCR "
            "(extract_image_text), this understands image content. Inspect-tier; "
            "no page mutation. Requires a configured vision provider "
            "(SB_ANTHROPIC_API_KEY / SB_OPENAI_API_KEY / SB_UITARS_MODEL_PATH); "
            "returns a structured vision_unavailable error if none is configured."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to answer about the screenshot.",
                },
                "selector": {
                    "type": "string",
                    "description": "CSS selector of an element to analyze. Mutually exclusive with bounds.",
                },
                "bounds": {
                    "type": "object",
                    "description": "Bounding box {x, y, width, height} of the region to analyze. Mutually exclusive with selector.",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                    },
                    "required": ["x", "y", "width", "height"],
                },
                "full_page": {
                    "type": "boolean",
                    "description": "Analyze the full scrollable page (default: viewport only).",
                    "default": False,
                },
                "format": {
                    "type": "string",
                    "enum": ["png", "jpeg"],
                    "description": "Screenshot format sent to the provider. Default: png (lossless).",
                    "default": "png",
                },
                "quality": {
                    "type": "integer",
                    "description": "JPEG quality 1-100. Only valid when format=jpeg; ignored for png.",
                    "minimum": 1,
                    "maximum": 100,
                },
            },
            "required": ["question"],
        },
    ),
]

_PHASE1_TOOL_NAMES = frozenset(t.name for t in PHASE1_TOOLS)


# --- Navigation-tier tool: wait_for (page-acquisition / read workflow) ---

NAVIGATION_AUX_TOOLS: list[types.Tool] = [
    types.Tool(
        name="wait_for",
        description=(
            "Wait for a page condition: selector present, text visible, URL "
            "reached, or load state. Navigation-tier tool (default-allowed; "
            "does not consume the action budget)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector to wait for."},
                "text": {"type": "string", "description": "Text to wait for in the page body."},
                "url": {"type": "string", "description": "URL pattern to wait for (glob)."},
                "load_state": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                },
                "timeout_ms": {
                    "type": "integer", "default": 10000,
                    "description": "Max wait time (ms). Must be 100-60000.",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="switch_tab",
        description=(
            "Switch to a different browser tab by ID. Navigation-tier tool "
            "(default-allowed; does not consume the action budget). Use "
            "list_tabs to discover tab IDs. Diagnostics remain session-wide "
            "after switching; per-tab diagnostics are not supported yet."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tab_id": {
                    "type": "integer",
                    "description": "The tab ID to switch to (from list_tabs).",
                },
            },
            "required": ["tab_id"],
        },
    ),
    types.Tool(
        name="reload",
        description="Reload the current page. Navigation-tier tool (default-allowed).",
        inputSchema={
            "type": "object",
            "properties": {
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                    "default": "domcontentloaded",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="go_back",
        description="Go back one step in browser history. Navigation-tier tool (default-allowed).",
        inputSchema={
            "type": "object",
            "properties": {
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                    "default": "domcontentloaded",
                },
            },
            "required": [],
        },
    ),
    types.Tool(
        name="go_forward",
        description="Go forward one step in browser history. Navigation-tier tool (default-allowed).",
        inputSchema={
            "type": "object",
            "properties": {
                "wait_until": {
                    "type": "string",
                    "enum": ["load", "domcontentloaded", "networkidle"],
                    "default": "domcontentloaded",
                },
            },
            "required": [],
        },
    ),
]

_NAVIGATION_AUX_TOOL_NAMES = frozenset(t.name for t in NAVIGATION_AUX_TOOLS)


# --- Write tools: navigation and input (navigate, scroll, press_key) ---

PHASE2B_TOOLS: list[types.Tool] = [
    types.Tool(
        name="navigate",
        description="Navigate the browser to a URL. Side-effecting: routed through the permission gate (domain allow/block lists apply).",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Destination URL."},
                "wait_until": {"type": "string", "description": "Wait condition: load, domcontentloaded, or networkidle.", "default": "domcontentloaded"},
            },
            "required": ["url"],
        },
    ),
    types.Tool(
        name="scroll",
        description="Scroll the page in a direction. Side-effecting: routed through the permission gate.",
        inputSchema={
            "type": "object",
            "properties": {
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "default": "down"},
                "amount": {"type": "integer", "description": "Scroll amount (units/pages).", "default": 3},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="press_key",
        description="Press a keyboard key. Side-effecting: routed through the permission gate. Can submit forms depending on the key.",
        inputSchema={
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key to press (e.g. 'Enter', 'Tab', 'Escape')."},
            },
            "required": ["key"],
        },
    ),
]

_PHASE2B_TOOL_NAMES = frozenset(t.name for t in PHASE2B_TOOLS)


# --- Write tools: elements and tabs (click, fill, open_tab, close_tab) ---

PHASE2B_WAVE2_TOOLS: list[types.Tool] = [
    types.Tool(
        name="click",
        description="Click an element by CSS selector. Side-effecting: routed through the permission gate.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the element to click."},
                "description": {"type": "string", "description": "Optional human-readable note about what is being clicked."},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="fill",
        description="Fill a form field with a value. Sends only the literal value supplied by the caller — does not retrieve, infer, store, or auto-fill credentials. Side-effecting: routed through the permission gate.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the input element."},
                "value": {"type": "string", "description": "Value to type into the field."},
                "clear_first": {"type": "boolean", "description": "Clear the field before filling (default: true).", "default": True},
                "description": {"type": "string", "description": "Optional human-readable note."},
            },
            "required": ["target", "value"],
        },
    ),
    types.Tool(
        name="open_tab",
        description="Open a new browser tab, optionally navigating to a URL. When a URL is provided, domain allow/block lists apply. Side-effecting: routed through the permission gate.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional URL to navigate the new tab to."},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="close_tab",
        description="Close a browser tab by ID. Side-effecting: routed through the permission gate.",
        inputSchema={
            "type": "object",
            "properties": {
                "tab_id": {"type": "integer", "description": "The tab ID to close (from list_tabs)."},
            },
            "required": ["tab_id"],
        },
    ),
]

_PHASE2B_WAVE2_TOOL_NAMES = frozenset(t.name for t in PHASE2B_WAVE2_TOOLS)


# --- Four-tier tool model (P1) ---
#
# The tool surface is partitioned into three advertised tiers (Inspect,
# Navigation, Action) plus a future High-risk tier. The advertised-default
# surface is Inspect + Navigation (reading requires page acquisition).
#
#   Inspect tier     - no page mutation (always advertised)
#   Navigation tier  - page acquisition / read workflow (always advertised)
#   Action tier      - page interaction (requires allow_actions)
#   High-risk tier   - JS, files, storage, credentials (future, per-capability)
#
# Navigation mutates browser state (page acquisition) but is default-allowed
# because reading requires a page to read. It is security-checked (injection,
# redaction, domain policy when configured) but NOT action-gated and does NOT
# consume the action budget.

# Diagnostics inspect-tier tools (P2): explainability layer for failed
# reads/rendering. Read-only snapshots of console/page-error/network buffers.
# No action gate, no audit, no budget. No response bodies, no raw header
# values (header NAMES only).
DIAGNOSTICS_TOOLS: list[types.Tool] = [
    types.Tool(
        name="get_console_messages",
        description="Return buffered browser console messages (snapshot, non-destructive). Inspect-tier.",
        inputSchema={
            "type": "object",
            "properties": {
                "level": {"type": "string", "description": "Filter by console type: log, error, warning, info, debug."},
                "limit": {"type": "integer", "default": 100, "description": "Return the last N entries."},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="get_page_errors",
        description="Return buffered uncaught page errors with stack traces (snapshot). Inspect-tier.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100, "description": "Return the last N entries."},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="get_network_errors",
        description="Return buffered network requests that failed (status>=400, no response, or net error). Inspect-tier.",
        inputSchema={
            "type": "object",
            "properties": {
                "url_filter": {"type": "string", "description": "Substring filter on URL."},
                "limit": {"type": "integer", "default": 100, "description": "Return the last N entries."},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="list_requests",
        description="Return buffered network request summaries (request_id, method, url, status). Use request_id with get_request for detail. Inspect-tier.",
        inputSchema={
            "type": "object",
            "properties": {
                "url_filter": {"type": "string", "description": "Substring filter on URL."},
                "resource_type": {"type": "string", "description": "Exact resource type: fetch, xhr, document, image, ..."},
                "limit": {"type": "integer", "default": 100, "description": "Return the last N entries."},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="get_request",
        description="Return one network request's metadata by request_id (from list_requests). No response body, header names only. Inspect-tier.",
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id returned by list_requests."},
            },
            "required": ["request_id"],
        },
    ),
]
_DIAGNOSTICS_TOOL_NAMES = frozenset(t.name for t in DIAGNOSTICS_TOOLS)

# Inspect-tier tools: the existing read-only set + diagnostics.
INSPECT_TOOLS: list[types.Tool] = [*PHASE1_TOOLS, *DIAGNOSTICS_TOOLS]
INSPECT_TOOL_NAMES = frozenset(t.name for t in INSPECT_TOOLS)

# Navigation-tier tools: page acquisition (navigate) + read condition (wait_for).
# `navigate` is lifted out of the write-tool gate into its own dispatch path.
NAVIGATE_TOOL = PHASE2B_TOOLS[0]  # the navigate Tool definition lives here
NAVIGATION_TOOLS: list[types.Tool] = [NAVIGATE_TOOL, *NAVIGATION_AUX_TOOLS]
NAVIGATION_TOOL_NAMES = frozenset(t.name for t in NAVIGATION_TOOLS)

# Action-tier tools: page interaction (requires allow_actions).
# navigate is removed from the action set; scroll/press_key remain.
# P3.1 adds: hover, select_option, check, uncheck, focus, type_text.
INTERACTION_TOOLS: list[types.Tool] = [
    types.Tool(
        name="hover",
        description="Hover over an element by selector. Action-tier (requires --allow-actions).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the element to hover."},
                "description": {"type": "string", "description": "Optional natural-language description for vision-based fallback."},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="select_option",
        description="Select an option in a <select> element by selector. Action-tier (requires --allow-actions).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the <select> element."},
                "option": {"type": "string", "description": "The option value to select."},
                "by": {"type": "string", "enum": ["text", "value", "label"], "default": "text",
                        "description": "How to match the option."},
                "description": {"type": "string", "description": "Optional natural-language description."},
            },
            "required": ["target", "option"],
        },
    ),
    types.Tool(
        name="check",
        description="Check a checkbox or radio button by selector. Action-tier (requires --allow-actions).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the checkbox/radio."},
                "description": {"type": "string", "description": "Optional natural-language description."},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="uncheck",
        description="Uncheck a checkbox by selector. Action-tier (requires --allow-actions).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the checkbox."},
                "description": {"type": "string", "description": "Optional natural-language description."},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="focus",
        description="Focus an element by selector. Action-tier (requires --allow-actions).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the element to focus."},
                "description": {"type": "string", "description": "Optional natural-language description."},
            },
            "required": ["target"],
        },
    ),
    types.Tool(
        name="type_text",
        description="Type text character-by-character (per-keystroke) into an element. Unlike fill, this triggers JS key listeners. Action-tier (requires --allow-actions).",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "CSS selector for the input element."},
                "text": {"type": "string", "description": "The text to type."},
                "delay_ms": {"type": "integer", "default": 0, "description": "Delay between keystrokes in ms (0-1000)."},
                "description": {"type": "string", "description": "Optional natural-language description."},
            },
            "required": ["target", "text"],
        },
    ),
]

ACTION_TOOLS: list[types.Tool] = [*PHASE2B_TOOLS[1:], *PHASE2B_WAVE2_TOOLS, *INTERACTION_TOOLS]
ACTION_TOOL_NAMES = frozenset(t.name for t in ACTION_TOOLS)

# Default advertised surface: Inspect + Navigation.
DEFAULT_TOOLS: list[types.Tool] = [*INSPECT_TOOLS, *NAVIGATION_TOOLS]
DEFAULT_TOOL_NAMES = frozenset(t.name for t in DEFAULT_TOOLS)


# ============================================================================
# Browser runtime lifecycle (encapsulated, not module globals)
# ============================================================================


class MCPBrowserRuntime:
    """Owns the single SuperBrowser instance for one MCP server process.

    Lazy-starts the browser on the first browser-dependent tool call and
    exposes ``status()`` / ``current_url()`` which are safe before startup.
    The caller is responsible for calling ``shutdown()`` on server close.

    The runtime wraps the SDK's :class:`SuperBrowser` facade; it does not
    invent its own permission model (Phase 2 write tools will go through
    ``SecurityManager``).
    """

    def __init__(self, config: Any | None = None) -> None:
        self._config = config
        self._sb: Any = None  # SuperBrowser | None
        self._backend_name: str = "(not started)"

    @property
    def sb(self) -> Any:
        """The live SuperBrowser, or None if not started."""
        return self._sb

    async def get_browser(self) -> Any:
        """Lazily start and return the SuperBrowser.

        If a cached instance exists but its page/context is dead (closed by
        the browser, OS, or a crash), the stale instance is torn down and a
        fresh one is launched transparently. This prevents the runtime from
        returning a dead handle that causes every subsequent tool call to fail
        with TargetClosedError until the server process is manually restarted.

        Imported lazily so the MCP module imports cleanly even when the full
        SDK extra set is not installed -- only the server *runtime* needs the
        SDK, not module import time.
        """
        if self._sb is not None:
            if not self._is_alive(self._sb):
                logger.warning("MCP runtime: browser/page is dead, recovering...")
                await self._cleanup_stale()
            else:
                return self._sb

        from super_browser import SuperBrowser

        sb = SuperBrowser(config=self._config) if self._config is not None else SuperBrowser()
        await sb.start()
        self._sb = sb
        # Best-effort backend label for status reporting.
        try:
            self._backend_name = type(sb._engine).__name__  # type: ignore[union-attr]
        except Exception:
            self._backend_name = "started"
        logger.info("MCP runtime: browser started (%s)", self._backend_name)
        return self._sb

    @staticmethod
    def _is_alive(sb: Any) -> bool:
        """Check whether the SuperBrowser instance is still usable.

        Uses the facade's ``is_alive`` property if available; falls back to
        True (assume alive) for older facades that don't expose it.
        """
        try:
            return bool(sb.is_alive)
        except (AttributeError, Exception):
            return True

    async def _cleanup_stale(self) -> None:
        """Tear down a dead SuperBrowser instance without raising."""
        if self._sb is None:
            return
        try:
            await self._sb.stop()
        except Exception as e:  # noqa: BLE001 -- cleanup must not raise
            logger.warning("MCP runtime: error during stale cleanup: %s", e)
        finally:
            self._sb = None
            self._backend_name = "(not started)"

    async def status(self) -> dict[str, Any]:
        """Runtime status. Safe before startup (does not lazy-start).

        Detects dead browser sessions and reports ``running: False``.
        """
        if self._sb is None:
            return {
                "running": False,
                "backend": "(not started)",
                "note": "browser will start on first browser-dependent tool call",
            }
        if not self._is_alive(self._sb):
            return {
                "running": False,
                "backend": self._backend_name,
                "note": "browser/page is dead; will recover on next tool call",
            }
        return {
            "running": True,
            "backend": self._backend_name,
        }

    async def current_url(self) -> dict[str, Any]:
        """Current URL. Returns a 'not started' state without lazy-launch.

        Returns a 'not started' state if the browser is dead.
        """
        if self._sb is None or not self._is_alive(self._sb):
            return {"started": False, "url": None, "note": "browser not started"}
        page = getattr(self._sb, "_page", None)
        url = getattr(page, "url", None) if page is not None else None
        return {"started": True, "url": url}

    async def shutdown(self) -> None:
        """Tear down the browser. Idempotent; safe to call multiple times."""
        if self._sb is not None:
            try:
                await self._sb.stop()
            except Exception as e:  # noqa: BLE001 -- shutdown must not raise
                logger.warning("MCP runtime: error during shutdown: %s", e)
            finally:
                self._sb = None
                self._backend_name = "(not started)"


# ============================================================================
# Serialization (ActionResult / arbitrary objects -> MCP content)
# ============================================================================


def _to_jsonable(obj: Any) -> Any:
    """Recursively coerce an ActionResult / dataclass / dict into JSON-able form."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}  # type: ignore[arg-type]
    # Enums, paths, datetime, etc.: stringify as a fallback.
    return str(obj)


def _serialize_action_result(result: Any) -> dict[str, Any]:
    """Turn a facade ActionResult into a stable JSON dict for MCP responses."""
    payload: dict[str, Any] = {"ok": bool(getattr(result, "ok", False))}
    data = getattr(result, "data", None)
    if data is not None:
        payload["data"] = _to_jsonable(data)
    err = getattr(result, "error", None)
    if err is not None:
        payload["error"] = _to_jsonable(err)
    # Surface a couple of useful meta fields if present (timing, category).
    meta = getattr(result, "meta", None)
    if meta is not None:
        payload["meta"] = _to_jsonable(meta)
    return payload


def _text_content(payload: dict[str, Any]) -> list[types.TextContent]:
    """Wrap a JSON payload as a single MCP TextContent block."""
    return [types.TextContent(type="text", text=json.dumps(payload, default=str, indent=2))]


def _error_content(message: str, *, kind: str = "error") -> list[types.TextContent]:
    """Wrap an error message as a structured MCP TextContent block."""
    return _text_content({"ok": False, kind: message})


def _image_content(png_bytes: bytes, mime: str = "image/png") -> types.ImageContent:
    """Wrap screenshot bytes as an MCP ImageContent block."""
    return types.ImageContent(
        type="image",
        data=base64.b64encode(png_bytes).decode("ascii"),
        mimeType=mime,
    )


# ============================================================================
# Permission substrate
# ============================================================================

# Action-tool names (formerly "write tools"). navigate has been lifted into
# the Navigation tier; these six are the action-budget-gated set. Retained
# under the old name for backward-compat with older imports.
WRITE_TOOL_NAMES = frozenset(ACTION_TOOL_NAMES)

# Default security level per action tool. `fill` is SENSITIVE by default; it
# becomes DANGEROUS only if later tied to credential stores (#180).
WRITE_TOOL_SECURITY_LEVELS: dict[str, str] = {
    name: "sensitive" for name in ACTION_TOOL_NAMES
}
# navigate is still SENSITIVE (used by the navigation dispatch path's audit).
NAVIGATION_SECURITY_LEVELS: dict[str, str] = {name: "sensitive" for name in NAVIGATION_TOOL_NAMES}


@dataclass(init=False)
class MCPSessionPolicy:
    """Per-session tool policy across the four tiers.

    ``allow_actions`` is the primary knob: it gates the Action tier
    (scroll/press_key/click/fill/open_tab/close_tab). ``allow_writes`` is a
    backward-compatibility alias for callers written before the tier split
    (released code may still pass ``allow_writes=True``). When both are
    supplied, ``allow_writes`` wins (legacy callers expect their explicit
    value to take effect).

    Navigation-tier tools (navigate, wait_for) are default-allowed and are
    NOT controlled by this flag. The Inspect tier is always allowed.

    Mutable: ``actions_used`` increments as action-tier calls are authorized.
    """

    allow_actions: bool
    max_actions: int
    timeout_seconds: float
    started_at_monotonic: float
    actions_used: int

    def __init__(
        self,
        *,
        allow_actions: bool = False,
        allow_writes: bool | None = None,
        max_actions: int = 25,
        timeout_seconds: float = 120.0,
        started_at_monotonic: float | None = None,
        actions_used: int = 0,
    ) -> None:
        # Legacy callers passed allow_writes; honor it when explicit.
        if allow_writes is not None:
            allow_actions = allow_writes
        self.allow_actions = allow_actions
        self.max_actions = max_actions
        self.timeout_seconds = timeout_seconds
        self.started_at_monotonic = started_at_monotonic or time.monotonic()
        self.actions_used = actions_used

    @property
    def allow_writes(self) -> bool:
        """Backward-compat alias for ``allow_actions``."""
        return self.allow_actions

    @allow_writes.setter
    def allow_writes(self, value: bool) -> None:
        self.allow_actions = value


@dataclass(frozen=True)
class MCPAuditEntry:
    """One audit record per write-tool attempt (allowed OR denied)."""

    timestamp_ms: float
    tool: str
    arguments: dict[str, Any]
    security_level: str
    allowed: bool
    blocked_by: str | None
    reason: str | None


@dataclass(frozen=True)
class MCPAuthorizationResult:
    """Outcome of the central authorization path."""

    allowed: bool
    security_level: str
    blocked_by: str | None  # e.g. "mcp_policy", "action_count", "timeout", "security_manager"
    reason: str | None


def _refusal_content(
    tool: str, result: MCPAuthorizationResult,
) -> list[types.TextContent]:
    """Structured refusal returned as normal MCP content (never raised)."""
    return _text_content({
        "ok": False,
        "refusal": {
            "tool": tool,
            "blocked_by": result.blocked_by,
            "reason": result.reason,
            "security_level": result.security_level,
        },
    })


class MCPAuthorizer:
    """Central write-tool authorization: write-enabled → count → timeout →
    SecurityManager → audit.

    Write handlers MUST call :meth:`authorize` before any facade dispatch.
    Read-only tools do not route through here.
    """

    def __init__(
        self,
        policy: MCPSessionPolicy,
        security_manager: Any | None = None,
    ) -> None:
        self.policy = policy
        self.security_manager = security_manager
        self.audit_log: list[MCPAuditEntry] = []

    async def authorize(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        url: str = "",
        security_level: str | None = None,
    ) -> MCPAuthorizationResult:
        level = security_level or WRITE_TOOL_SECURITY_LEVELS.get(tool, "sensitive")

        # 1. Action-enabled gate. (allow_writes is a compat alias; reads the
        #    same underlying flag. Message says "actions are disabled" because
        #    the Action tier is what this gate protects.)
        if not self.policy.allow_actions:
            result = MCPAuthorizationResult(
                allowed=False, security_level=level,
                blocked_by="mcp_policy", reason="actions are disabled",
            )
            self._record(tool, arguments, level, result)
            return result

        # 2. Action-count budget.
        if self.policy.actions_used >= self.policy.max_actions:
            result = MCPAuthorizationResult(
                allowed=False, security_level=level,
                blocked_by="action_count",
                reason=f"max_actions ({self.policy.max_actions}) exceeded",
            )
            self._record(tool, arguments, level, result)
            return result

        # 3. Timeout budget.
        elapsed = time.monotonic() - self.policy.started_at_monotonic
        if elapsed > self.policy.timeout_seconds:
            result = MCPAuthorizationResult(
                allowed=False, security_level=level,
                blocked_by="timeout",
                reason=f"timeout_seconds ({self.policy.timeout_seconds}) exceeded",
            )
            self._record(tool, arguments, level, result)
            return result

        # 4. SecurityManager (reusable SDK layer; not an MCP-only system).
        if self.security_manager is not None:
            try:
                from super_browser.security.types import SecurityLevel

                sec_level = SecurityLevel(level)
                url_for_check = url or arguments.get("url", "")
                sec_result = await self.security_manager.check_action(
                    tool, arguments, str(url_for_check), sec_level,
                )
                if not sec_result.passed:
                    result = MCPAuthorizationResult(
                        allowed=False, security_level=level,
                        blocked_by="security_manager",
                        reason=sec_result.blocked_by or "denied by security policy",
                    )
                    self._record(tool, arguments, level, result)
                    return result
            except Exception as e:  # noqa: BLE001 -- a security-layer failure must deny, not crash
                result = MCPAuthorizationResult(
                    allowed=False, security_level=level,
                    blocked_by="security_manager",
                    reason=f"security check raised: {type(e).__name__}: {e}",
                )
                self._record(tool, arguments, level, result)
                return result

        # 5. Allowed: consume one action and record.
        self.policy.actions_used += 1
        result = MCPAuthorizationResult(
            allowed=True, security_level=level,
            blocked_by=None, reason=None,
        )
        self._record(tool, arguments, level, result)
        return result

    def _record(
        self, tool: str, arguments: dict[str, Any], level: str,
        result: MCPAuthorizationResult,
    ) -> None:
        self.audit_log.append(MCPAuditEntry(
            timestamp_ms=time.time() * 1000.0,
            tool=tool,
            arguments=dict(arguments),
            security_level=level,
            allowed=result.allowed,
            blocked_by=result.blocked_by,
            reason=result.reason,
        ))

    def record_audit(
        self,
        *,
        tool: str,
        arguments: dict[str, Any],
        security_level: str,
        allowed: bool,
        blocked_by: str | None = None,
        reason: str | None = None,
    ) -> MCPAuthorizationResult:
        """Public audit helper for navigation-tier entries.

        Navigation tools bypass the action-budget authorization path but still
        need audit records (for both approvals and denials). This constructs the
        ``MCPAuthorizationResult``, records it, and returns it so the caller can
        pass it straight to ``_refusal_content()`` without rebinding.

        Does NOT touch ``policy.actions_used`` — navigation does not consume the
        action budget.
        """
        result = MCPAuthorizationResult(
            allowed=allowed,
            security_level=security_level,
            blocked_by=blocked_by,
            reason=reason,
        )
        self._record(tool, arguments, security_level, result)
        return result


# ============================================================================
# Tool dispatcher (one central path, so permissions can hook in later)
# ============================================================================


class ToolDispatcher:
    """Central tool dispatcher.

    Read-only tools are allow-by-construction (no side effects), so no
    permission check runs for them. Write tools MUST go through
    ``MCPAuthorizer.authorize()`` before reaching a handler.
    """

    def __init__(
        self,
        runtime: MCPBrowserRuntime,
        authorizer: MCPAuthorizer | None = None,
    ) -> None:
        self.runtime = runtime
        self.authorizer = authorizer

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> list[types.TextContent | types.ImageContent]:
        # Action tier: gated by allow_actions + full authorization path.
        if name in ACTION_TOOL_NAMES:
            return await self._dispatch_action(name, arguments)
        # Navigation tier: default-allowed, security-checked, audited, but
        # NOT action-gated or action-budgeted.
        if name in NAVIGATION_TOOL_NAMES:
            return await self._dispatch_navigation(name, arguments)
        # Inspect tier: allow-by-construction, no permission check.
        if name in INSPECT_TOOL_NAMES:
            handler = getattr(self, f"_tool_{name}", None)
            if handler is None:
                return _error_content(f"Tool {name!r} has no handler", kind="error")
            try:
                return await handler(arguments)
            except Exception as e:  # noqa: BLE001 -- structured error, no crash
                logger.exception("MCP tool %s failed", name)
                return _error_content(f"{type(e).__name__}: {e}", kind="error")
        # Unknown tool.
        return _error_content(
            f"Unknown tool: {name!r}. Available: {sorted(DEFAULT_TOOL_NAMES | ACTION_TOOL_NAMES)}",
            kind="error",
        )

    async def _dispatch_navigation(
        self, name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent | types.ImageContent]:
        """Navigation-tier dispatch: validate → SecurityManager check (navigate
        only) → audit → handler.

        Navigation does NOT require ``allow_actions``, does NOT increment
        ``actions_used``, and does NOT go through action-count/timeout checks.
        navigate IS security-checked (injection + redaction + domain policy
        when a SecurityManager is configured). Both approvals and denials are
        audited via ``record_audit()``.
        """
        # 1. Validate args before any browser/security call. Invalid args must
        #    not produce an audit entry.
        validation_error = self._validate_navigation_args(name, arguments)
        if validation_error is not None:
            return validation_error

        # 2. navigate: domain + injection + redaction check via SecurityManager,
        #    then audit the outcome (approval OR denial). Both the security
        #    check and the audit are conditional on an authorizer being
        #    attached; navigation itself is default-allowed and proceeds even
        #    with a bare ToolDispatcher(runtime) (no audit, no security check).
        #    wait_for needs no security check (it reads).
        if name == "navigate" and self.authorizer is not None:
            url = str(arguments.get("url", ""))

            if self.authorizer.security_manager is not None:
                from super_browser.security.types import SecurityLevel

                sec_result = await self.authorizer.security_manager.check_action(
                    "navigate", arguments, url, SecurityLevel.SENSITIVE,
                )
                if not sec_result.passed:
                    # Audit the denial via the PUBLIC helper and reuse the
                    # returned MCPAuthorizationResult for the refusal body.
                    result = self.authorizer.record_audit(
                        tool="navigate",
                        arguments=arguments,
                        security_level="sensitive",
                        allowed=False,
                        blocked_by="security_manager",
                        reason=sec_result.blocked_by or "denied by security policy",
                    )
                    return _refusal_content("navigate", result)

            # Audit the approval (whether or not a SecurityManager ran).
            self.authorizer.record_audit(
                tool="navigate",
                arguments=arguments,
                security_level="sensitive",
                allowed=True,
            )

        # 3. Execute the handler (after validation + security).
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            return _error_content(f"Tool {name!r} has no handler", kind="error")
        try:
            return await handler(arguments)
        except Exception as e:  # noqa: BLE001 -- structured error, no crash
            logger.exception("MCP navigation tool %s failed", name)
            return _error_content(f"{type(e).__name__}: {e}", kind="error")

    async def _dispatch_action(
        self, name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent | types.ImageContent]:
        """Action-tier dispatch: validate → action gate → authorize → handler.

        Authorization happens before ANY facade/browser call. If denied, the
        facade is never touched. All 12 action tools (scroll, press_key, click,
        fill, open_tab, close_tab, hover, select_option, check, uncheck, focus,
        type_text) have real handlers that call the facade/controller
        after the authorizer approves.
        """
        # 1. Validate args before authorization (invalid args must not consume
        #    the action budget).
        validation_error = self._validate_action_args(name, arguments)
        if validation_error is not None:
            return validation_error

        # 2. No authorizer configured -> action tools not available.
        if self.authorizer is None:
            return _text_content({
                "ok": False,
                "refusal": {
                    "tool": name, "blocked_by": "mcp_policy",
                    "reason": "action tools not configured on this server",
                    "security_level": "sensitive",
                },
            })

        # 3. Full authorization path: action-gate → action-count → timeout →
        #    SecurityManager → audit. The action-gate denial is audited here
        #    (reason="actions are disabled"), preserving the audit-on-deny
        #    contract.
        url = str(arguments.get("url", ""))
        result = await self.authorizer.authorize(
            tool=name, arguments=arguments, url=url,
        )
        if not result.allowed:
            return _refusal_content(name, result)

        # 4. Execute handler.
        handler = getattr(self, f"_tool_{name}", None)
        if handler is not None:
            try:
                return await handler(arguments)
            except Exception as e:  # noqa: BLE001 -- structured error, no crash
                logger.exception("MCP action tool %s failed", name)
                return _error_content(f"{type(e).__name__}: {e}", kind="error")

        # Should never reach here: every ACTION_TOOL_NAMES entry has a handler.
        return _error_content(f"Tool {name!r} authorized but has no handler", kind="error")

    # --- argument validation (before authorization, before budget consumed) ---

    @staticmethod
    def _validate_navigation_args(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent] | None:
        """Return a structured error if args are invalid, else None.

        Covers navigate (existing rules) and wait_for (new rules). Validation
        runs BEFORE any security check and produces NO audit entry.
        """
        if name == "navigate":
            url = arguments.get("url")
            if not isinstance(url, str) or not url.strip():
                return _error_content(
                    "'url' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
            wait_until = arguments.get("wait_until", "domcontentloaded")
            if not isinstance(wait_until, str) or wait_until not in ("load", "domcontentloaded", "networkidle"):
                return _error_content(
                    "'wait_until' must be one of: load, domcontentloaded, networkidle",
                    kind="invalid_arguments",
                )
            return None

        if name == "switch_tab":
            tab_id = arguments.get("tab_id")
            if not isinstance(tab_id, int) or isinstance(tab_id, bool) or tab_id < 0:
                return _error_content(
                    "'tab_id' is required and must be a non-negative integer",
                    kind="invalid_arguments",
                )
            return None

        if name in ("reload", "go_back", "go_forward"):
            wait_until = arguments.get("wait_until", "domcontentloaded")
            if wait_until not in ("load", "domcontentloaded", "networkidle"):
                return _error_content(
                    "'wait_until' must be one of: load, domcontentloaded, networkidle",
                    kind="invalid_arguments",
                )
            return None

        if name == "wait_for":
            # timeout_ms: integer, 100 <= x <= 60000. Reject bool (int subtype).
            timeout_ms = arguments.get("timeout_ms", 10000)
            if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
                return _error_content(
                    "'timeout_ms' must be an integer",
                    kind="invalid_arguments",
                )
            if timeout_ms < 100 or timeout_ms > 60000:
                return _error_content(
                    "'timeout_ms' must be between 100 and 60000",
                    kind="invalid_arguments",
                )
            # Exactly one condition. (P1 rationale: deterministic single-condition
            # results; compound waits can be added later as an explicit AND mode.)
            conditions = [
                key for key in ("selector", "text", "url", "load_state")
                if isinstance(arguments.get(key), str) and arguments[key].strip()
            ]
            if len(conditions) != 1:
                return _error_content(
                    "Provide exactly one of: selector, text, url, load_state",
                    kind="invalid_arguments",
                )
            if "load_state" in conditions and \
                    arguments["load_state"] not in ("load", "domcontentloaded", "networkidle"):
                return _error_content(
                    "'load_state' must be one of: load, domcontentloaded, networkidle",
                    kind="invalid_arguments",
                )
            return None

        return None

    @staticmethod
    def _validate_action_args(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent] | None:
        """Return a structured error if args are invalid, else None.

        This is the navigate-free subset of the former ``_validate_write_args``.
        Covers: scroll, press_key, click, fill, open_tab, close_tab.
        """
        if name == "scroll":
            direction = arguments.get("direction", "down")
            if direction not in ("up", "down", "left", "right"):
                return _error_content(
                    "'direction' must be one of: up, down, left, right",
                    kind="invalid_arguments",
                )
            amount = arguments.get("amount", 3)
            if not isinstance(amount, int) or amount < 1:
                return _error_content(
                    "'amount' must be a positive integer",
                    kind="invalid_arguments",
                )
        elif name == "press_key":
            key = arguments.get("key")
            if not isinstance(key, str) or not key.strip():
                return _error_content(
                    "'key' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
        elif name == "click":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                return _error_content(
                    "'target' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
        elif name == "fill":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                return _error_content(
                    "'target' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
            value = arguments.get("value")
            if not isinstance(value, str):
                return _error_content(
                    "'value' is required and must be a string",
                    kind="invalid_arguments",
                )
        elif name == "open_tab":
            url = arguments.get("url")
            if url is not None and (not isinstance(url, str) or not url.strip()):
                return _error_content(
                    "'url' must be a non-empty string if provided",
                    kind="invalid_arguments",
                )
        elif name == "close_tab":
            tab_id = arguments.get("tab_id")
            if not isinstance(tab_id, int) or tab_id < 0:
                return _error_content(
                    "'tab_id' is required and must be a non-negative integer",
                    kind="invalid_arguments",
                )
        elif name in ("hover", "check", "uncheck", "focus"):
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                return _error_content(
                    "'target' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
        elif name == "select_option":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                return _error_content(
                    "'target' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
            option = arguments.get("option")
            if not isinstance(option, str) or not option.strip():
                return _error_content(
                    "'option' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
            by = arguments.get("by", "text")
            if by not in ("text", "value", "label"):
                return _error_content(
                    "'by' must be one of: text, value, label",
                    kind="invalid_arguments",
                )
        elif name == "type_text":
            target = arguments.get("target")
            if not isinstance(target, str) or not target.strip():
                return _error_content(
                    "'target' is required and must be a non-empty string",
                    kind="invalid_arguments",
                )
            text = arguments.get("text")
            if not isinstance(text, str):
                return _error_content(
                    "'text' is required and must be a string",
                    kind="invalid_arguments",
                )
            delay_ms = arguments.get("delay_ms", 0)
            if not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or delay_ms < 0 or delay_ms > 1000:
                return _error_content(
                    "'delay_ms' must be an integer between 0 and 1000",
                    kind="invalid_arguments",
                )
        return None

    @staticmethod
    def _validate_write_args(
        name: str, arguments: dict[str, Any],
    ) -> list[types.TextContent] | None:
        """DEPRECATED alias retained for backward-compat with older callers.

        Routes to the new tier-specific validator. New code should call
        ``_validate_navigation_args`` or ``_validate_action_args`` directly.
        """
        if name in NAVIGATION_TOOL_NAMES:
            return ToolDispatcher._validate_navigation_args(name, arguments)
        return ToolDispatcher._validate_action_args(name, arguments)

    # --- Navigation handlers (called after validation + security) ---

    async def _tool_navigate(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.navigate(arguments["url"], wait_until=arguments.get("wait_until", "domcontentloaded"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_switch_tab(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.switch_tab(arguments["tab_id"])
        return _text_content(_serialize_action_result(ar))

    async def _tool_reload(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.reload(wait_until=arguments.get("wait_until", "domcontentloaded"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_go_back(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.go_back(wait_until=arguments.get("wait_until", "domcontentloaded"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_go_forward(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.go_forward(wait_until=arguments.get("wait_until", "domcontentloaded"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_wait_for(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Wait for exactly one page condition (selector/text/url/load_state).

        Waits through the backend-neutral page adapter (P6): native
        Patchright/Playwright semantics when the backend supplies the
        ``wait_for_*`` family, a documented polling fallback otherwise, and
        a structured refusal for conditions that cannot be represented
        faithfully (e.g. ``networkidle`` without native support).
        """
        timeout_ms = arguments.get("timeout_ms", 10000)

        sb = await self.runtime.get_browser()
        page = getattr(sb, "_page", None)
        if page is None:
            return _error_content("browser has no active page", kind="error")

        try:
            matched = await page.wait_for(
                selector=arguments.get("selector"),
                text=arguments.get("text"),
                url=arguments.get("url"),
                load_state=arguments.get("load_state"),
                timeout_ms=timeout_ms,
            )
        except NotImplementedError as exc:
            return _error_content(str(exc), kind="invalid_arguments")
        except ValueError as exc:
            return _error_content(str(exc), kind="invalid_arguments")
        except Exception as e:  # noqa: BLE001 -- timeouts surface as structured errors
            return _text_content({"ok": False, "timeout": True, "reason": str(e)})

        return _text_content({"ok": True, "matched": matched})

    # --- Action handlers: input (called only after authorization) ---

    async def _tool_scroll(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.scroll(
            direction=arguments.get("direction", "down"),
            amount=arguments.get("amount", 3),
        )
        return _text_content(_serialize_action_result(ar))

    async def _tool_press_key(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.keypress(arguments["key"])
        return _text_content(_serialize_action_result(ar))

    # --- Write handlers: elements and tabs (called only after authorization) ---

    async def _tool_click(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.click(
            arguments["target"],
            description=arguments.get("description"),
        )
        return _text_content(_serialize_action_result(ar))

    async def _tool_fill(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.fill(
            arguments["target"],
            arguments["value"],
            clear_first=arguments.get("clear_first", True),
            description=arguments.get("description"),
        )
        return _text_content(_serialize_action_result(ar))

    async def _tool_open_tab(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        url = arguments.get("url")
        ar = await sb.open_tab(url)
        return _text_content(_serialize_action_result(ar))

    async def _tool_close_tab(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        ar = await sb.close_tab(arguments["tab_id"])
        return _text_content(_serialize_action_result(ar))

    # --- P3.1 interaction handlers (action-tier; delegate to controller) ---

    async def _tool_hover(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.hover(arguments["target"], description=arguments.get("description"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_select_option(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.select(
            arguments["target"], arguments["option"],
            by=arguments.get("by", "text"),
            description=arguments.get("description"),
        )
        return _text_content(_serialize_action_result(ar))

    async def _tool_check(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.check(arguments["target"], description=arguments.get("description"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_uncheck(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.uncheck(arguments["target"], description=arguments.get("description"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_focus(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.focus(arguments["target"], description=arguments.get("description"))
        return _text_content(_serialize_action_result(ar))

    async def _tool_type_text(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        controller = getattr(sb, "_controller", None)
        if controller is None:
            return _error_content("browser has no active controller", kind="error")
        ar = await controller.type_text(
            arguments["target"], arguments["text"],
            delay=arguments.get("delay_ms", 0),
            description=arguments.get("description"),
        )
        return _text_content(_serialize_action_result(ar))

    # --- read-only tools (none of these lazy-start except where noted) ---

    async def _tool_browser_status(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        _require_no_args(arguments)
        status = await self.runtime.status()
        return _text_content({"ok": True, "status": status})

    async def _tool_current_url(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        _require_no_args(arguments)
        result = await self.runtime.current_url()
        payload = {"ok": True, **result}
        return _text_content(self._redact_inspect_output(payload, url_fields=("url",)))

    # --- Inspect-output redaction (P2.3) ---

    # Fields whose string values should be pattern-scanned for secrets.
    _REDACT_TEXT_FIELDS = frozenset({
        "text", "message", "stack", "title", "failure_text",
    })
    # Fields treated as URLs (two-pass: redact_context + secret pattern scan).
    _REDACT_URL_FIELDS = frozenset({"url", "page_url"})

    def _redact_inspect_output(
        self,
        payload: dict[str, Any],
        *,
        url_fields: tuple[str, ...] = (),
        list_keys: dict[str, tuple[str, ...]] | None = None,
        nested_keys: dict[str, tuple[str, ...]] | None = None,
    ) -> dict[str, Any]:
        """Apply redaction policy to inspect-tier tool output at the MCP boundary.

        Redacts secret patterns in known text fields (text, message, stack, title,
        failure_text) and URLs (two-pass: redact_context for sensitive query-param
        names, then SecretRedactor for secret substrings). No-op when no
        SecurityManager is attached or redaction is disabled.

        - ``url_fields``: top-level keys in ``payload`` whose values are URLs.
        - ``list_keys``: maps a list-key name (e.g. "messages") to a tuple of
          field names within each list element that should be checked for
          text/URL redaction.
        - ``nested_keys``: maps a single-nested-dict key name (e.g. "data",
          "request") to a tuple of field names within that nested dict.

        Output shape is preserved; only string values may contain redaction
        markers.
        """
        sm = getattr(self.authorizer, "security_manager", None) if self.authorizer else None
        if sm is None:
            return payload  # bare dispatcher — no redaction configured
        config = getattr(sm, "_config", None)
        if config is not None and not getattr(config, "redaction_enabled", True):
            return payload  # explicitly disabled

        import copy

        from super_browser.security.action_redaction import redact_context

        # Deep-copy before mutating so we never contaminate the caller's payload
        # (diagnostics buffers return live dict references; mutating them would
        # violate the MCP-boundary-only redaction guarantee).
        payload = copy.deepcopy(payload)

        def _redact_text(value: str) -> str:
            try:
                return sm.redact_secrets(value).redacted_text
            except Exception:  # noqa: BLE001
                return value

        def _redact_url(value: str) -> str:
            try:
                scrubbed = redact_context(value)
                return sm.redact_secrets(scrubbed).redacted_text
            except Exception:  # noqa: BLE001
                return value

        def _redact_field(obj: dict, field: str, is_url: bool) -> None:
            val = obj.get(field)
            if isinstance(val, str) and val:
                obj[field] = _redact_url(val) if is_url else _redact_text(val)

        # Redact top-level fields.
        for field in url_fields:
            _redact_field(payload, field, is_url=True)
        for field in self._REDACT_TEXT_FIELDS:
            _redact_field(payload, field, is_url=False)
        for field in self._REDACT_URL_FIELDS:
            _redact_field(payload, field, is_url=True)

        # Redact list-of-dicts entries.
        # Supports dotted paths like "data.targets" for lists nested under a dict.
        if list_keys:
            for list_key, inner_fields in list_keys.items():
                # Navigate dotted path (e.g. "data.targets" → payload["data"]["targets"])
                items = payload
                for part in list_key.split("."):
                    if isinstance(items, dict):
                        items = items.get(part)
                    else:
                        items = None
                        break
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    for f in inner_fields:
                        is_url = f in self._REDACT_URL_FIELDS or f == "url"
                        _redact_field(item, f, is_url=is_url)

        # Redact single-nested-dict entries (e.g. data.text, request.url).
        if nested_keys:
            for nested_key, inner_fields in nested_keys.items():
                nested = payload.get(nested_key)
                if not isinstance(nested, dict):
                    continue
                for f in inner_fields:
                    is_url = f in self._REDACT_URL_FIELDS or f == "url"
                    _redact_field(nested, f, is_url=is_url)

        return payload

    # --- Diagnostics handlers (inspect-tier; read from sb.diagnostics) ---

    async def _tool_get_console_messages(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        level = arguments.get("level")
        limit = arguments.get("limit", 100)
        messages = sb.diagnostics.console_messages(level=level, limit=limit)
        payload = {"ok": True, "messages": messages, "count": len(messages)}
        return _text_content(self._redact_inspect_output(
            payload, list_keys={"messages": ("text", "page_url")},
        ))

    async def _tool_get_page_errors(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        limit = arguments.get("limit", 100)
        errors = sb.diagnostics.page_errors(limit=limit)
        payload = {"ok": True, "errors": errors, "count": len(errors)}
        return _text_content(self._redact_inspect_output(
            payload, list_keys={"errors": ("message", "stack", "page_url")},
        ))

    async def _tool_get_network_errors(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        url_filter = arguments.get("url_filter")
        limit = arguments.get("limit", 100)
        reqs = sb.diagnostics.requests(url_filter=url_filter, failed_only=True, limit=limit)
        payload = {"ok": True, "requests": reqs, "count": len(reqs)}
        return _text_content(self._redact_inspect_output(
            payload, list_keys={"requests": ("url", "page_url", "failure_text")},
        ))

    async def _tool_list_requests(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        sb = await self.runtime.get_browser()
        url_filter = arguments.get("url_filter")
        resource_type = arguments.get("resource_type")
        limit = arguments.get("limit", 100)
        reqs = sb.diagnostics.requests(
            url_filter=url_filter, resource_type=resource_type, failed_only=False, limit=limit,
        )
        payload = {"ok": True, "requests": reqs, "count": len(reqs)}
        return _text_content(self._redact_inspect_output(
            payload, list_keys={"requests": ("url", "page_url")},
        ))

    async def _tool_get_request(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        request_id = arguments.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return _error_content(
                "'request_id' is required and must be a non-empty string",
                kind="invalid_arguments",
            )
        sb = await self.runtime.get_browser()
        detail = sb.diagnostics.request_detail(request_id)
        if detail is None:
            return _text_content({"ok": False, "reason": "not_found", "request_id": request_id})
        payload = {"ok": True, "request": detail}
        # get_request nests url/page_url/failure_text under the "request" dict
        return _text_content(self._redact_inspect_output(
            payload, nested_keys={"request": ("url", "page_url", "failure_text")},
        ))

    async def _tool_observe(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        _require_no_args(arguments)
        sb = await self.runtime.get_browser()
        ar = await sb.observe()
        payload = _serialize_action_result(ar)
        # observe nests URL/title/targets/images under data dict.
        # Redact url/title (nested fields), target names, and image alt/name
        # (nested list-of-dicts) so secrets can't leak through inspect output.
        data = payload.get("data")
        nested = {"data": ("url", "title")} if isinstance(data, dict) else None
        list_keys: dict[str, tuple[str, ...]] | None = None
        if isinstance(data, dict):
            parts: dict[str, tuple[str, ...]] = {}
            if isinstance(data.get("targets"), list):
                parts["data.targets"] = ("name",)
            if isinstance(data.get("images"), list):
                parts["data.images"] = ("name", "alt")
            if parts:
                list_keys = parts
        return _text_content(self._redact_inspect_output(
            payload, nested_keys=nested,
            list_keys=list_keys,
        ))

    async def _tool_extract_text(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        query = arguments.get("query")
        if not isinstance(query, str) or not query:
            return _error_content("'query' is required and must be a non-empty string", kind="invalid_arguments")
        selector = arguments.get("selector")
        if selector is not None and not isinstance(selector, str):
            return _error_content("'selector' must be a string if provided", kind="invalid_arguments")
        sb = await self.runtime.get_browser()
        ar = await sb.extract(query, selector=selector)
        payload = _serialize_action_result(ar)
        # extract_text nests text under data dict
        nested = {"data": ("text",)} if isinstance(payload.get("data"), dict) else None
        return _text_content(self._redact_inspect_output(
            payload, nested_keys=nested,
        ))

    async def _tool_extract_image_text(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Extract text from page screenshots via OCR. Inspect-tier."""
        import re

        selector = arguments.get("selector")
        bounds = arguments.get("bounds")
        full_page = bool(arguments.get("full_page", False))
        language = arguments.get("language", "eng")
        min_confidence = arguments.get("min_confidence", 0.0)

        # --- Validation (before any screenshot/OCR work) ---

        # selector and bounds are mutually exclusive.
        if selector is not None and bounds is not None:
            return _error_content(
                "'selector' and 'bounds' are mutually exclusive; provide one or neither.",
                kind="invalid_arguments",
            )

        # Validate bounds structure.
        if bounds is not None:
            if not isinstance(bounds, dict):
                return _error_content("'bounds' must be an object", kind="invalid_arguments")
            for field in ("x", "y", "width", "height"):
                if field not in bounds:
                    return _error_content(
                        f"'bounds' must include '{field}'",
                        kind="invalid_arguments",
                    )
                if not isinstance(bounds[field], (int, float)) or isinstance(bounds[field], bool):
                    return _error_content(
                        f"'bounds.{field}' must be a number",
                        kind="invalid_arguments",
                    )
            if bounds["width"] <= 0:
                return _error_content(
                    "'bounds.width' must be positive",
                    kind="invalid_arguments",
                )
            if bounds["height"] <= 0:
                return _error_content(
                    "'bounds.height' must be positive",
                    kind="invalid_arguments",
                )

        # Validate language — must be a non-empty string before regex.
        if not isinstance(language, str) or not language:
            return _error_content(
                "'language' must be a non-empty string",
                kind="invalid_arguments",
            )
        # Allow letters, digits, plus (+) for multi-language, underscore.
        if not re.match(r"^[a-zA-Z0-9+_]+$", language):
            return _error_content(
                f"'language' must be a valid tesseract language code (e.g. 'eng', 'ara', 'eng+ara'), got {language!r}",
                kind="invalid_arguments",
            )

        # Validate min_confidence.
        if not isinstance(min_confidence, (int, float)) or isinstance(min_confidence, bool):
            return _error_content(
                "'min_confidence' must be a number between 0.0 and 1.0",
                kind="invalid_arguments",
            )
        if min_confidence < 0.0 or min_confidence > 1.0:
            return _error_content(
                f"'min_confidence' must be between 0.0 and 1.0, got {min_confidence}",
                kind="invalid_arguments",
            )

        # --- Execute OCR ---

        sb = await self.runtime.get_browser()
        try:
            ar = await sb.extract_image_text(
                selector=selector,
                bounds=bounds,
                full_page=full_page,
                language=language,
                min_confidence=float(min_confidence),
            )
        except RuntimeError as e:
            # OCR unavailable — structured error, not a crash.
            msg = str(e)
            if "tesseract" in msg.lower() or "ocr" in msg.lower() or "not installed" in msg.lower():
                return _error_content(msg, kind="ocr_unavailable")
            return _error_content(msg, kind="error")

        payload = _serialize_action_result(ar)

        # Apply min_confidence filtering at the MCP boundary if the facade
        # didn't already filter (belt-and-suspenders).
        data = payload.get("data")
        if isinstance(data, dict) and isinstance(data.get("words"), list) and min_confidence > 0:
            filtered = [
                w for w in data["words"]
                if isinstance(w, dict) and w.get("confidence", 0) >= min_confidence
            ]
            data["words"] = filtered
            data["text"] = " ".join(w.get("text", "") for w in filtered)

        # Redact: OCR text can contain secrets from screenshots.
        # Cover both the joined text (nested in data.text) and individual
        # word texts (data.words[].text).
        data = payload.get("data")
        nested_keys: dict[str, tuple[str, ...]] | None = None
        list_keys: dict[str, tuple[str, ...]] | None = None
        if isinstance(data, dict):
            if isinstance(data.get("text"), str):
                nested_keys = {"data": ("text",)}
            if isinstance(data.get("words"), list):
                list_keys = {"data.words": ("text",)}

        payload = self._redact_inspect_output(
            payload, nested_keys=nested_keys, list_keys=list_keys,
        )

        return _text_content(payload)

    async def _tool_analyze_image(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        """Answer a question about a page screenshot via a vision-LLM provider.
        Inspect-tier."""
        question = arguments.get("question")
        selector = arguments.get("selector")
        bounds = arguments.get("bounds")
        full_page = bool(arguments.get("full_page", False))
        fmt = arguments.get("format", "png")
        quality = arguments.get("quality")

        # --- Validation (before any screenshot/analysis work) ---

        # question is required and must be a non-empty string.
        if not isinstance(question, str) or not question.strip():
            return _error_content(
                "'question' is required and must be a non-empty string",
                kind="invalid_arguments",
            )

        # selector and bounds are mutually exclusive.
        if selector is not None and bounds is not None:
            return _error_content(
                "'selector' and 'bounds' are mutually exclusive; provide one or neither.",
                kind="invalid_arguments",
            )

        # Validate bounds structure.
        if bounds is not None:
            if not isinstance(bounds, dict):
                return _error_content("'bounds' must be an object", kind="invalid_arguments")
            for field in ("x", "y", "width", "height"):
                if field not in bounds:
                    return _error_content(
                        f"'bounds' must include '{field}'",
                        kind="invalid_arguments",
                    )
                if not isinstance(bounds[field], (int, float)) or isinstance(bounds[field], bool):
                    return _error_content(
                        f"'bounds.{field}' must be a number",
                        kind="invalid_arguments",
                    )
            if bounds["width"] <= 0:
                return _error_content(
                    "'bounds.width' must be positive",
                    kind="invalid_arguments",
                )
            if bounds["height"] <= 0:
                return _error_content(
                    "'bounds.height' must be positive",
                    kind="invalid_arguments",
                )

        # Validate format.
        if fmt not in ("png", "jpeg"):
            return _error_content(
                f"'format' must be 'png' or 'jpeg', got {fmt!r}",
                kind="invalid_arguments",
            )

        # Validate quality.
        if quality is not None:
            if not isinstance(quality, int) or isinstance(quality, bool):
                return _error_content(
                    "'quality' must be an integer (1-100)",
                    kind="invalid_arguments",
                )
            if quality < 1 or quality > 100:
                return _error_content(
                    f"'quality' must be between 1 and 100, got {quality}",
                    kind="invalid_arguments",
                )
            if fmt == "png":
                return _error_content(
                    "'quality' is only valid when format='jpeg'; PNG is always lossless",
                    kind="invalid_arguments",
                )

        # --- Execute ---

        sb = await self.runtime.get_browser()
        try:
            ar = await sb.analyze_image(
                question=question.strip(),
                selector=selector,
                bounds=bounds,
                full_page=full_page,
                format=fmt,
                quality=quality,
            )
        except Exception as e:
            return _error_content(str(e), kind="error")

        payload = _serialize_action_result(ar)

        # Map the facade's vision_unavailable signal to a structured error.
        if isinstance(payload, dict) and payload.get("error") == "vision_unavailable":
            return _error_content(
                "No vision provider configured. Set SB_ANTHROPIC_API_KEY, "
                "SB_OPENAI_API_KEY, or SB_UITARS_MODEL_PATH.",
                kind="vision_unavailable",
            )

        # Redact the answer text (vision output can surface page secrets).
        data = payload.get("data")
        nested_keys: dict[str, tuple[str, ...]] | None = None
        if isinstance(data, dict) and isinstance(data.get("answer"), str):
            nested_keys = {"data": ("answer",)}

        payload = self._redact_inspect_output(payload, nested_keys=nested_keys)

        return _text_content(payload)

    async def _tool_screenshot(self, arguments: dict[str, Any]) -> list[types.TextContent | types.ImageContent]:
        full_page = bool(arguments.get("full_page", False))

        # Validate format.
        fmt = arguments.get("format", "png")
        if fmt not in ("png", "jpeg"):
            return _error_content(
                f"'format' must be 'png' or 'jpeg', got {fmt!r}",
                kind="invalid_arguments",
            )

        # Validate quality.
        quality = arguments.get("quality")
        if quality is not None:
            if not isinstance(quality, int) or isinstance(quality, bool):
                return _error_content(
                    "'quality' must be an integer (1-100)",
                    kind="invalid_arguments",
                )
            if quality < 1 or quality > 100:
                return _error_content(
                    f"'quality' must be between 1 and 100, got {quality}",
                    kind="invalid_arguments",
                )
            if fmt == "png":
                return _error_content(
                    "'quality' is only valid when format='jpeg'; PNG is always lossless",
                    kind="invalid_arguments",
                )

        sb = await self.runtime.get_browser()
        page = getattr(sb, "_page", None)
        if page is None:
            return _error_content("browser has no active page", kind="error")

        raw = await page.screenshot(
            full_page=full_page, format=fmt, quality=quality,
        )

        # Determine actual mime: if the backend returned PNG despite jpeg
        # request (Selenium without Pillow), report the truth.
        actual_fmt = "jpeg" if fmt == "jpeg" and not raw.startswith(b"\x89PNG") else "png"
        mime = f"image/{actual_fmt}"
        return [
            _image_content(raw, mime=mime),
            _text_content({
                "ok": True,
                "format": actual_fmt,
                "requested_format": fmt,
                "full_page": full_page,
                "quality": quality if actual_fmt == "jpeg" else None,
                "bytes": len(raw),
            })[0],
        ]

    async def _tool_list_tabs(self, arguments: dict[str, Any]) -> list[types.TextContent]:
        _require_no_args(arguments)
        sb = await self.runtime.get_browser()
        ar = await sb.list_tabs()
        payload = _serialize_action_result(ar)
        return _text_content(self._redact_inspect_output(
            payload, list_keys={"data": ("url", "title")} if isinstance(payload.get("data"), list) else None,
        ))


def _require_no_args(arguments: dict[str, Any]) -> None:
    """Reject unexpected arguments for tools that take none."""
    if arguments:
        raise ValueError(f"unexpected arguments: {sorted(arguments.keys())}")


# ============================================================================
# Server wiring
# ============================================================================


def _tools_for_policy(policy: MCPSessionPolicy) -> list[types.Tool]:
    """Return the tool list a server should advertise for the given policy.

    The default advertised surface is the Inspect + Navigation tiers (8 tools):
    reading requires page acquisition. Action-tier tools are advertised only
    when ``policy.allow_actions`` is True (``allow_writes`` is a compat alias).
    Extracted so tests can assert on the advertisement without spawning the
    stdio loop.
    """
    tools = list(DEFAULT_TOOLS)
    if policy.allow_actions:
        tools += list(ACTION_TOOLS)
    return tools


def build_server(
    runtime: MCPBrowserRuntime | None = None,
    *,
    policy: MCPSessionPolicy | None = None,
    security_manager: Any | None = None,
) -> Server:
    """Construct the MCP Server wired to read-only and/or write tools.

    Default behavior is intentionally asymmetric:
    - ``list_tools()`` advertises only the 6 read-only tools.
    - ``call_tool()`` still recognizes write-tool names and returns a
      structured policy refusal (not an "unknown tool" error), so manual
      or unadvertised write calls are handled cleanly.

    When constructed with ``policy=MCPSessionPolicy(allow_writes=True)``,
    the server advertises all 13 tools and routes write calls through the
    ``MCPAuthorizer`` (``MCPSessionPolicy`` → ``SecurityManager`` → audit).

    Factored out so tests can drive the server object without spawning the
    stdio loop, and so the runtime and policy can be injected.
    """
    if runtime is None:
        runtime = MCPBrowserRuntime()
    if policy is None:
        policy = MCPSessionPolicy()
    authorizer = MCPAuthorizer(policy, security_manager=security_manager)
    dispatcher = ToolDispatcher(runtime, authorizer=authorizer)
    server = Server("super-browser")

    def _advertised_tools() -> list[types.Tool]:
        return _tools_for_policy(policy)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return _advertised_tools()

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent | types.ImageContent]:
        return await dispatcher.dispatch(name, arguments)

    # Attach runtime, policy, dispatcher, and authorizer so tests can exercise
    # the actual server-owned advertisement and dispatch paths without
    # spawning the stdio loop.
    server._sb_runtime = runtime  # type: ignore[attr-defined]
    server._sb_policy = policy  # type: ignore[attr-defined]
    server._sb_dispatcher = dispatcher  # type: ignore[attr-defined]
    server._sb_authorizer = authorizer  # type: ignore[attr-defined]
    return server


def _env_truthy(name: str) -> bool:
    """True if the named env var is one of the truthy sentinels.

    Accepts (case-insensitive, after strip): 1, true, yes, on. Anything else
    (including unset / empty / 0 / false) is False.
    """
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_redaction_enabled() -> bool:
    """Determine whether MCP inspect-output redaction is enabled.

    ``SB_MCP_REDACTION`` is a positive flag (on by default when unset).
    Explicitly disabling requires ``0``, ``false``, ``off``, or ``no``.
    """
    raw = os.environ.get("SB_MCP_REDACTION")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "off", "no"}


def _parse_domain_list(name: str) -> tuple[str, ...]:
    """Parse a comma- or whitespace-separated hostname/glob list from env.

    Returns a tuple (empty if unset/blank). Used by _build_default_security_manager.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ()
    parts = [p.strip() for p in raw.replace(",", " ").split()]
    return tuple(p for p in parts if p)


def _build_default_security_manager() -> Any:
    """Construct a SecurityManager from environment config so navigation is
    always security-checked.

    With empty lists (the default), DomainFilter is allow-all and the manager
    still runs injection detection + secret redaction on URL/params. Setting
    SB_MCP_DOMAIN_BLOCKLIST or SB_MCP_DOMAIN_ALLOWLIST adds domain enforcement.
    """
    from super_browser.security import SecurityConfig, SecurityManager

    # SB_MCP_REDACTION=0|false|off|no disables redaction (design: inspect_redaction.md).
    # Default is on (matches SecurityConfig.redaction_enabled=True).
    redaction_enabled = _env_redaction_enabled()
    config = SecurityConfig(
        domain_allowlist=_parse_domain_list("SB_MCP_DOMAIN_ALLOWLIST"),
        domain_blocklist=_parse_domain_list("SB_MCP_DOMAIN_BLOCKLIST"),
        redaction_enabled=redaction_enabled,
    )
    return SecurityManager(config)


async def run_server(
    config: Any | None = None,
    *,
    allow_actions: bool = False,
    security_manager: Any | None = None,
) -> None:
    """Run the stdio MCP server to completion (blocks).

    A default SecurityManager is constructed unless one is passed in, so
    navigate is ALWAYS security-checked (injection + redaction; domain filter
    when env lists are set).
    """
    if security_manager is None:
        security_manager = _build_default_security_manager()

    runtime = MCPBrowserRuntime(config=config)
    policy = MCPSessionPolicy(allow_actions=allow_actions)
    server = build_server(runtime, policy=policy, security_manager=security_manager)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        await runtime.shutdown()


def main() -> None:
    """Console-script entry point: ``superbrowser-mcp``."""
    import argparse
    import asyncio

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    parser = argparse.ArgumentParser(prog="superbrowser-mcp")
    parser.add_argument(
        "--allow-actions", action="store_true",
        help="Enable action tools (click, fill, scroll, etc.)",
    )
    args, _unknown = parser.parse_known_args()

    allow_actions = args.allow_actions or _env_truthy("SB_MCP_ALLOW_ACTIONS")

    try:
        asyncio.run(run_server(allow_actions=allow_actions))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
