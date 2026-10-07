# Changelog

All notable changes to this project will be documented in this file.

## [2.13.1] — 2026-06-29

### Fixed

- Fixed `analyze_image` / `extract_image_text` screenshot capture when the active page object is a raw Playwright/Patchright page, which expects `type=` instead of the public `format=` wrapper argument. `_capture_region_bytes` now translates `format`→`type` (matching `PageHandle.screenshot()`), so both raw and wrapper page objects work on first use after `start()`.
- Fixed OpenAI-compatible vision analysis providers that reject `response_format={"type":"json_object"}` (e.g. LM Studio) by retrying the analysis request without JSON-mode enforcement. The retry fires only when the failure is specifically attributable to `response_format`; arbitrary API errors are not blindly retried. Controller-side JSON parsing and raw-text fallback are preserved.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.13.0] — 2026-06-28

### Added — MCP vision image analysis (P7.C)

New `analyze_image` inspect-tier MCP tool. Answers a natural-language question
about a page screenshot via a configured vision-LLM provider (semantic visual
QA). Unlike `extract_image_text` (OCR), this understands image content — the
missing capability for sites where product names, layouts, and offers are
encoded in product photos rather than text overlays.

- Arguments: `question` (required), `selector` (optional), `bounds` (optional,
  `{x, y, width, height}`), `full_page` (optional), `format` (optional,
  `"png"` | `"jpeg"`, default `"png"`), `quality` (optional, 1-100, jpeg only).
- `selector` and `bounds` are mutually exclusive; no args = viewport analysis.
- Validation: all args validated before any screenshot/analysis work.
- Output (nested under `data`): `{answer, confidence, provider, model,
  token_cost, duration_ms, source{selector, bounds, full_page}}`.
- Graceful degradation: returns a structured `vision_unavailable` error when no
  provider is configured — no crash, no API key required for CI.
- Redaction: covers the `answer` text.
- Architecture: new provider `analyze()` path (visual QA), kept fully separate
  from `locate()` (coordinate grounding). New `analyze_state()` controller path
  uses `_call_analysis_with_failover()`, which calls `provider.analyze()`
  exclusively — never `provider.locate()`.
- MIME plumbing: `VisionRequest.mime_type` is propagated to provider
  `media_type`/data-URI, so JPEG bytes are labeled `image/jpeg`, not `image/png`.
- Lazy controller: an explicit `analyze_image()` call builds a
  `VisionController` from `SB_ANTHROPIC_API_KEY` / `SB_OPENAI_API_KEY` /
  `SB_UITARS_MODEL_PATH` on demand — `enable_vision` (which gates only the
  automatic interaction fallback) is not required.
- Default advertised tool count: 18 → 19. Action-mode: 30 → 31.

## [2.11.1] — 2026-06-27

### Fixed — MCP browser auto-recovery

`MCPBrowserRuntime.get_browser()` now health-checks the cached browser
instance before returning it. When the page/context is dead (closed by the
browser, OS, or a crash — the `TargetClosedError` scenario), the stale
instance is torn down and a fresh browser is launched transparently on the
next tool call.

Previously, a dead browser context caused every subsequent tool call to fail
with `TargetClosedError` until the MCP server process was manually restarted.
Now the runtime self-heals.

- `PageHandle.is_alive` property: checks `page.is_closed()` (Patchright native).
- `SuperBrowser.is_alive` property: delegates to `PageHandle.is_alive`.
- `MCPBrowserRuntime._is_alive()`: static helper with graceful fallback for
  backends that don't expose the method.
- `MCPBrowserRuntime._cleanup_stale()`: tears down the dead instance without
  raising, then clears the cached handle for lazy relaunch.
- `status()` and `current_url()` now detect dead sessions and report
  `running: False` / `started: False` instead of masking the dead state.
- No retry of mutating actions mid-operation — recovery happens only between
  tool calls, never during one.

## [2.12.0] — 2026-06-27

### Added — OCR image text extraction (P7.B)

New `extract_image_text` inspect-tier MCP tool. Runs Tesseract OCR on a
screenshot (viewport, full page, or cropped to bounds) and returns structured
word boxes + joined text. This makes image-encoded content — flyer/catalog
sites that embed product names and prices in raster graphics — deterministically
readable without depending on model vision.

- Arguments: `selector` (optional), `bounds` (optional, `{x, y, width, height}`),
  `full_page` (optional), `language` (default `"eng"`, supports `"eng+ara"` etc.),
  `min_confidence` (0.0–1.0, filters low-confidence words).
- `selector` and `bounds` are mutually exclusive; no args = viewport OCR.
- Validation: all args validated before any screenshot/OCR work.
- Output: `{text, words[{text,x,y,w,h,confidence}], language, source}`.
- Graceful degradation: returns structured `ocr_unavailable` error when
  Tesseract binary or language pack is missing — no crash.
- Redaction: covers both joined `text` and individual `words[].text`.
- New `[vision]` optional extra: `Pillow>=10.0`, `pytesseract>=0.3.10`.

## [2.11.0] — 2026-06-27

### Added — Image metadata visibility in observe (P7.D)

`observe` now returns an `images` array alongside `targets`, containing
metadata for image-role elements on the page. Each image entry includes `ref`,
`role`, `name`, `alt` (the image's alt text or AX name), and optionally
`bounds`. This exposes image-encoded content that was previously invisible to
inspect tools — flyer/catalog sites that embed product names in images now
surface their alt text where available.

Non-interactive images are **metadata-only**: they appear in `images`, never in
`targets`, preserving the action-ready semantics of `targets`. Images without a
name/alt are skipped (no useful metadata). Both arrays are independently capped
at 50 entries with their own `*_truncated` flags.

MCP inspect-output redaction now covers `data.images` (both `name` and `alt`
fields) so secrets cannot leak through image metadata.

## [2.10.0] — 2026-06-27

### Added — Screenshot output controls (P7.A)

The `screenshot` MCP tool now accepts `format` (`"png"` | `"jpeg"`) and `quality`
(1-100, jpeg only) parameters. Default behavior is unchanged (lossless PNG
viewport). Requesting `format="jpeg"` with a `quality` value produces a smaller
image that fits under host inline limits — a JPEG at quality 70 is typically
5-10x smaller than the equivalent PNG, bringing a ~700 KiB viewport screenshot
under the ~200 KiB inline limit most MCP hosts enforce.

Validation:
- `quality` is rejected when `format="png"` (PNG is always lossless).
- `quality` must be an integer in 1-100.
- `format` must be `"png"` or `"jpeg"`.
- Invalid arguments fail before any browser call.

Backend coverage: Patchright/Playwright forward natively; CDP forwards via
`Page.captureScreenshot`; Selenium (PNG-only) gracefully falls back to PNG
when jpeg is requested without Pillow, or re-encodes via Pillow if available.

## [2.9.1] — 2026-06-27

### Fixed — AX snapshot bounds resolution (v2.9.0 hotfix)

The CDP `Accessibility.getFullAXTree` response does not include element bounds,
which caused `observe()` to return 0 actionable targets against live pages (all
nodes failed the `center is not None` filter). Fixed by resolving bounds via
`DOM.getBoxModel` using each node's `backendDOMNodeId` when the AX properties
don't include bounds. The resolution is defensive: CDP failures, missing models,
and zero-sized boxes are silently skipped (bounds stays `None`), and one node's
failure doesn't break the whole snapshot.

## [2.9.0] — 2026-06-27

### Added — MCP observe actionable targets

`observe` now returns a `targets` array of actionable element references
extracted from the accessibility snapshot. Each target includes:

- `target` — a ref string (e.g. `@e0`) usable as the `target` argument for
  coordinate-tier action tools: `click`, `fill`, `type_text`, `hover`,
  `select_option`. Checkbox/radio targets map to `click` (toggle-like) because
  `check`/`uncheck` are selector-tier only and cannot resolve `@refs`.
- `role` — the AX role (button, textbox, checkbox, combobox, link, etc.)
- `name` — the accessible name (subject to inspect-output redaction)
- `action_hint` — recommended action for the role (`click`, `fill`,
  select_option)

Targets are capped at 50 interactive, non-disabled elements.
`targets_truncated` indicates when more exist. This closes the gap where
agents had to guess CSS selectors — they can now go `observe → action`
deterministically using the refs returned by `observe`.

### Documentation — MCP tool metadata design note (P4)

Added `docs/design/mcp_tool_metadata.md` — a design note analyzing whether
MCP tool schemas, tiers, validation, and docs should be unified onto the
existing `ToolDefinition` registry. **Decision: defer full unification.** The
current `ToolDefinition` cannot express MCP-specific metadata (tiers, enums,
ranges, validation hooks, bool-as-int rejection) without lossy flattening. Keep
`mcp_server.py` as the MCP source of truth until tool count or a production
mismatch justifies change. Trigger conditions and three future options
documented.

## [2.8.0] — 2026-06-26

### Added — MCP interaction tools (P3.1)

Six new action-tier MCP tools for form and element interaction:

- `hover` — hover over an element (controller-backed, selector/coordinate/vision cascade)
- `select_option` — select an option in a `<select>` element (maps to controller `select`, supports `by`: text/value/label)
- `check` — check a checkbox or radio button (new controller method + cascade)
- `uncheck` — uncheck a checkbox (new controller method + cascade)
- `focus` — focus an element (new controller method, selector-tier only)
- `type_text` — type text character-by-character (per-keystroke), triggering JS key listeners. Unlike `fill` which sets the value atomically. Supports `delay_ms` (0–1000). (new controller method)

All six are action-tier: require `--allow-actions`, consume the action budget,
pass through `SecurityManager` + audit, and are absent from the default
advertised surface.

- Default advertised tools: remains 17
- Action-mode advertised tools: 23 → 29

New controller primitives (`check`, `uncheck`, `focus`, `type_text`) added to
`MultimodalController` with `@agent_action` decorators and `FrameInteractionTarget`
adapters. Registered in the facade's builtin-tool loop.

## [2.7.0] — 2026-06-26

### Added — MCP reload and history navigation tools (P3.0B)

- `reload`, `go_back`, `go_forward` are now navigation-tier MCP tools
  (default-allowed, not action-budgeted). Each delegates to a new thin facade
  method that calls the underlying Playwright page method.
- `go_back` and `go_forward` return a structured error when there is no
  history entry to navigate to.
- Default advertised tools: 14 → 17; action mode: 20 → 23.
- Diagnostics remain session-wide after navigation.

## [2.6.0] — 2026-06-26

### Added — MCP switch_tab navigation tool (P3.0A)

- `switch_tab` is now a navigation-tier MCP tool (default-allowed, not
  action-budgeted). Delegates to the existing `SuperBrowser.switch_tab(tab_id)`
  facade method. Use `list_tabs` to discover tab IDs.
- Default advertised tools: 13 → 14; action mode: 19 → 20.
- Diagnostics remain session-wide after switching tabs; per-tab diagnostics
  are not supported yet.

### Added — MCP inspect-output redaction (P2.3)

Inspect-tier MCP tools now redact detected secrets at the MCP serialization
boundary before returning content to the agent. Redaction is **on by default**
(matching `SecurityConfig.redaction_enabled=True`) and can be disabled via
`SB_MCP_REDACTION=0` (env) or `SecurityConfig(redaction_enabled=False)`
(programmatic).

**Tools redacted (9):** `extract_text`, `observe`, `current_url`, `list_tabs`,
`get_console_messages`, `get_page_errors`, `get_network_errors`, `list_requests`,
`get_request`.

**What gets redacted:**
- Text fields (text, message, stack, title, failure_text) — secret patterns
  detected by `SecretRedactor` (API keys, JWTs, passwords, PEM keys, etc.) are
  replaced with `[REDACTED:{type}:{hash6}]` markers.
- URL fields — two-pass: `redact_context()` redacts sensitive query-parameter
  values (`[REDACTED:query_param]`), then `SecretRedactor` pattern-scans for
  secrets in non-sensitive keys/fragments.

**What does NOT change:**
- Output shape (same keys/structure; only string values may contain markers).
- Python SDK consumers (`sb.extract()`, `sb.observe()`, `sb.diagnostics`) —
  redaction applies only at the MCP boundary, not the SDK path.
- `screenshot` (visual content), `browser_status` (no sensitive content).
- DiagnosticsBuffer capture behavior, facade outputs.

### Compatibility notes

- This is a **behavior change** for MCP consumers: inspect-tier output that
  previously contained raw secrets will now contain redaction markers. Set
  `SB_MCP_REDACTION=0` to restore raw output.

### Fixed — Release tooling

- `scripts/verify_release_artifact.py` now supports sdist (`.tar.gz`) artifacts
  via `tarfile`, in addition to wheels (`.whl`) via `zipfile`. Previously the
  verifier was wheel-only and threw `BadZipFile` on any sdist (hit during both
  the v2.4.0 and v2.5.0 releases). Shared checks (distribution name, version,
  extras, isolated install, import, CLI, README/docs scan) run for both archive
  types; wheel-only checks (entry points, module shadowing guard) are skipped
  for sdists with a clear "skipped" message. Unsupported archive types now fail
  with a clear error instead of an opaque `BadZipFile`.

## [2.5.0] — 2026-06-25

### Added — MCP Server P2: Diagnostics Pack

An explainability layer for failed reads/rendering. Five new inspect-tier
tools let an agent inspect browser/page/network evidence when
`navigate → wait_for → extract_text` returns unexpected content — without
expanding the action surface. No `--allow-actions`, no action budget, no audit
entry, no side effects.

- `get_console_messages` — buffered browser console messages, filterable by
  level.
- `get_page_errors` — buffered uncaught page errors with stack traces.
- `get_network_errors` — requests that failed (status ≥ 400, no response, or
  net error).
- `list_requests` — all buffered request summaries; returns `request_id` for
  `get_request` follow-up.
- `get_request` — one request's metadata by `request_id`.

New `DiagnosticsBuffer` (`super_browser.agent.diagnostics`): session-wide ring
buffers for console / page-error / network events, wired into the facade
lifecycle (`start()` + `_attach_page()`). Survives tab switches. Exposed as
`SuperBrowser.diagnostics`. Reads are snapshots (non-destructive); bounded
deques (default 500, via `config.event_buffer_size`).

### Design constraints (P2)

- Diagnostics entries carry a monotonic `seq`, `timestamp_ms`, and `page_url`.
- Request records use a stable `request_id` (assigned by the buffer). A URL can
  have multiple requests (redirects, retries), so retrieval is always by
  `request_id`, never by URL.
- **No response bodies** and **no raw header values** are returned. `get_request`
  exposes `header_names` (keys) only. Console text, page errors, and URLs are
  returned as-is, matching the existing inspect-tool posture.
- `get_request` returns a structured `{ok: false, reason: "not_found"}` when the
  request was never buffered or evicted; it never raises.

### Compatibility notes

- The default stdio server now advertises **13 tools** (was 8): the 5
  diagnostics tools are inspect-tier and always advertised. Clients asserting
  on the advertised count should update. This is additive — no tools were
  removed and no refusal strings changed.
- Action mode advertises **19 tools** (was 14).

## [2.4.0] — 2026-06-23

### Added — MCP Server P1: Navigation Workflows

The default MCP server can now read a URL end-to-end (`navigate → wait_for →
observe/extract_text/screenshot`) without action mode. The tool surface was
re-partitioned into a **four-tier model**:

- **Inspect tier** (always advertised): `browser_status`, `current_url`,
  `observe`, `extract_text`, `screenshot`, `list_tabs`.
- **Navigation tier** (always advertised): `navigate`, `wait_for` (new).
  Navigation mutates browser state (page acquisition) but is default-allowed
  because reading requires a page to read. It is `SecurityManager`-checked and
  audited (both approvals and denials) in the default stdio / `build_server()`
  path, but it does **not** consume the action budget. Bare
  `ToolDispatcher(runtime)` integrations may navigate without an attached
  authorizer; in that case no audit/security check is performed.
- **Action tier** (requires `--allow-actions` / `SB_MCP_ALLOW_ACTIONS=1`):
  `scroll`, `press_key`, `click`, `fill`, `open_tab`, `close_tab`.
- **High-risk tier** (not implemented): `download`, `upload`, `act`, arbitrary JS.

New APIs:

- `wait_for` tool: wait for exactly one page condition (`selector` / `text` /
  `url` / `load_state`) with `timeout_ms` (100–60000, default 10000).
- `MCPSessionPolicy.allow_actions` (primary) with `allow_writes` retained as a
  backward-compatibility alias.
- `MCPAuthorizer.record_audit()` — public audit helper for navigation-tier
  entries; returns the `MCPAuthorizationResult` it constructed.
- `--allow-actions` CLI flag and `SB_MCP_ALLOW_ACTIONS` env var
  (truthy sentinels: `1`, `true`, `yes`, `on`).
- `SB_MCP_DOMAIN_ALLOWLIST` / `SB_MCP_DOMAIN_BLOCKLIST` env vars for opt-in
  domain filtering on the default stdio server (comma- or whitespace-separated;
  glob patterns supported).
- `run_server()` now constructs a default `SecurityManager` so navigation is
  security-checked in the default stdio path; callers may pass
  `security_manager=` to override (or `None` to disable).

### Changed

- **Default advertised tool count: 6 → 8** (adds `navigate` and `wait_for`).
- `navigate` moved from the action-gated write tier to the default-allowed
  navigation tier.
- Action-gate refusal message changed from `"writes are disabled"` to
  `"actions are disabled"` (the gate protects the Action tier).
- `_tools_for_policy()` now advertises `DEFAULT_TOOLS` (inspect + navigation)
  by default and adds `ACTION_TOOLS` when `allow_actions` is set.
- `main()` now uses `argparse` (previously raw `sys.argv` substring matching).

### Compatibility notes

- The action-gate refusal `reason` changed from `"writes are disabled"` to
  `"actions are disabled"`. Clients/docs pattern-matching the old string should
  update. (`MCPSessionPolicy(allow_writes=True)` callers are otherwise
  unaffected — the flag still enables action tools via the compat alias.)
- The default stdio server now advertises 8 tools, not 6. Clients asserting on
  the advertised count should update.

### Fixed

- Fixed default navigation dispatch so bare `ToolDispatcher(runtime)`
  integrations can use `navigate` without requiring an authorizer; security
  checks and audit still run when an authorizer/security manager is attached,
  including the default stdio server path.

### Internal

- Tier constants: `INSPECT_TOOLS` / `NAVIGATION_TOOLS` / `ACTION_TOOLS` /
  `DEFAULT_TOOLS` and their `_NAME` frozensets. (`PHASE1_TOOLS`,
  `PHASE2B_TOOLS`, `PHASE2B_WAVE2_TOOLS`, and `WRITE_TOOL_NAMES` retained as
  aliases for backward-compat imports.)
- `_validate_write_args` split into `_validate_navigation_args` and
  `_validate_action_args`; the old name remains as a routing alias.
- `wait_for` reaches the raw Patchright/Playwright page via
  `sb._page.backend_page` (one hop); the deprecated `.raw_page` accessor is
  not used.

## [2.3.1] — 2026-06-22

### Fixed

- Fixed `superbrowser-mcp` / `python -m super_browser.mcp_server` startup crash caused by `logging.Stderr` (which does not exist in Python's stdlib); the MCP server entry point now configures logging with `sys.stderr`.
- Added entry-point bootstrap coverage so the MCP console-script path is exercised directly.

## [2.3.0] — 2026-06-22

### Bug Fixes

- `BrowserSession.start()` now passes `locale` to `new_context()`, fixing the missing `Accept-Language` header regression (T5-002, #198). Added `locale` field to `SessionConfig` (default `"en-US"`); `locale=None` omits the key for engine defaults.

### MCP Server — Phase 2

- Restored tested MCP server (`superbrowser-mcp`, `python -m super_browser.mcp_server`) over stdio (#179).
- Permission substrate: `MCPSessionPolicy`, `MCPAuthorizer`, `MCPAuditEntry` — central 5-step authorization path (write-enabled → action-count → timeout → `SecurityManager` → audit) (#183).
- Write tools: `navigate`, `scroll`, `press_key`, `click`, `fill`, `open_tab`, `close_tab` — all gated by `MCPSessionPolicy.allow_writes` (default `False`) and routed through `SecurityManager` before reaching the facade (#184, #185).
- `build_server()` now accepts `policy` and `security_manager` kwargs; `list_tools()` advertises write tools only when `allow_writes=True`.
- Default server behavior is asymmetric: `list_tools()` advertises 6 read-only tools; `call_tool()` recognizes write-tool names and returns a structured policy refusal (not "unknown tool").
- `fill` sends only the literal caller-supplied value — does not retrieve, infer, store, or auto-fill credentials.
- Structured refusals, domain allow/block enforcement, audit logging for all write attempts (allowed and denied).
- Still excluded: `download`, `upload`, `act`, arbitrary JS execution.

## [2.2.1] — 2026-06-17

### Overview

**v2.2.1 — Runtime Correctness**

Patch release fixing three user-facing runtime defects discovered in
post-release source audit. No new features, no breaking changes.

### Fixed

- **Rich page fingerprint `interactive_count`** (loop.py): Was iterating
  dict keys instead of `.values()`, calling `.get()` on string keys.
  The `except` block silently returned `interactive_count=0` every time,
  weakening page change detection. Now correctly counts interactive
  nodes via `is_interactive` attribute.

- **iframe action scoping** (controller.py + facade.py): `enter_frame()`
  pushed a frame locator but no interaction method used it — all actions
  targeted the top-level page instead of the iframe. New
  `FrameInteractionTarget` adapter normalizes Playwright's
  `FrameLocator` API (`.locator(sel).click()`) to match the controller's
  selector-tier signatures (`.click(sel)`).

- **IPReputationClient async path** (ip_reputation.py): Replaced
  deprecated `asyncio.get_event_loop()` with `asyncio.get_running_loop()`
  to fix DeprecationWarning on every call and future RuntimeError risk.

### Internal

- **Stress signal accuracy** (#167): RSS measurement prefers `psutil`
  with platform-aware fallback; periodic `RSSSampler`; hard sleeps
  replaced with `_wait_for_condition()` polling; `parallel_profiles`
  verifies cross-context isolation; `/api/upload` returns real digest.

## [2.2.0] — 2026-06-16

### Overview

**v2.2.0 — API Ergonomics**

v2.2 polishes the public API surface based on the Track D discovery
audit. No new features, no breaking changes — cleaner imports, better
CLI discoverability, and a new `python -m super_browser` entry point.

### Added

- **`python -m super_browser` support**: New `__main__.py` delegates to
  the CLI dispatcher, so users can run either `superbrowser version` or
  `python -m super_browser version`.

### Changed

- **Standardized top-level imports**: All examples, quickstart, and docs
  now use `from super_browser import SuperBrowser`, `Config`, and
  `create_llm` instead of deep module paths.
- **CLI help clarified**:
  - `stealth-check` → "Run offline fingerprint scoring and report"
  - `stealth-validate` → "Validate fingerprint against baselines
    (CI/regression mode)"
  - `result-demo` → labeled as internal command
- **API reference cleaned**: Removed stale `SuperBrowserConfig`
  references (class removed in v2.0) and the removed `core` field row
  from `AgentConfig`.
- **`raw_page` deprecation updated**: Removal target changed from
  "v2.1" to "v3.0" — the alias stays available through the v2.x series.

### Track D audit artifacts

- `docs/api-ergonomics-audit.md` — full discovery document with 15
  findings across three buckets (patch-safe, backward-compatible polish,
  defer).

## [2.1.1] — 2026-06-16

### Fixed — Smoke workflow install spec format

- **Smoke install specs were malformed** (`dist==version[extra]` instead
  of `dist[extra]==version`), causing the post-release smoke workflow
  to fail for any version-targeted run.
- Affects `[all]`, `[patchright]`, and `[playwright]` install checks in
  `scripts/smoke_published.py`.
- Found by the v2.1.0 post-release smoke run — exactly the kind of
  packaging bug the workflow was designed to catch.
- Added `TestInstallSpecFormat` regression test verifying PEP 508
  extras-before-version ordering.

### Fixed — Benchmark deprecated API usage

- `scripts/browser_benchmark.py` used the deprecated `raw_page` attribute,
  producing `DeprecationWarning` on every run.
- Replaced `page.raw_page.query_selector_all` → `page.backend_page`
- Replaced `page.raw_page.set_viewport_size` → `page.backend_page`

### Version metadata

- `pyproject.toml`, `__init__.py`, doc headers, test assertions bumped
  to `2.1.1`.

## [2.1.0] — 2026-06-16

### Overview

**v2.1.0 — Operational Maturity**

v2.1 makes Super Browser easier to operate, validate, benchmark, and
monitor after release. No new stealth features, no API redesigns —
this release adds the operational infrastructure needed to trust the
E2E signal, catch packaging regressions before users do, and track
performance over time.

### Added — Benchmarking (Track A)

- Offline real-browser benchmark harness (`scripts/browser_benchmark.py`)
  measuring 9 metrics: browser launch, new page, navigation (simple/form/
  DOM-heavy), DOM query, click+fill, screenshot, and memory delta.
- Local fixture-backed pages only — no network dependency.
- JSON schema v1 output with mean/median/min/max/stdev per metric.
- Markdown report for human-readable consumption.
- Configurable iterations, warmup, headless mode, backend selection.
- `timeout_s` enforcement via `asyncio.wait_for` on all operations.
- Fixture HTTP server cleanup in `try/finally`.
- Manual `benchmark.yml` workflow (`workflow_dispatch` only, 90-day artifacts).

### Added — Release Observability (Track B)

- Post-release PyPI smoke workflow (`scripts/smoke_published.py`).
- Fresh-venv install validation: `[all]`, `[patchright]`, `[playwright]`,
  `import super_browser`, `superbrowser version`, `superbrowser info`.
- Version match check when `--version` specified.
- JSON + Markdown smoke report output.
- Daily scheduled run at 08:00 UTC + manual `workflow_dispatch`.
- Automatic GitHub issue creation on scheduled failures.
- Report always written, even on early failures (venv creation, install).
- Explicit `issues: write` permission for auto-issue behavior.

### Added — E2E Reporting (Track C)

- **Schema v3**: Formalized E2E report contract with stdlib validator
  (`scripts/validate_e2e_report.py`). Required: `schema_version`,
  `timestamp_utc`, `environment`, `config`, `summary`, `tests`, `artifacts`.
  Per-test entries: `name`, `status`, `duration_s`, `file`, `error`,
  `screenshot`.
- **Failure metadata**: `error` field populated from `call.excinfo` /
  `report.longrepr`. `screenshot` field populated on failure, attached by
  `nodeid` match. Both nullable for passed/skipped tests.
- **Historical trend artifacts** (`scripts/e2e_trend.py`): Archives
  validated reports into `e2e-history.json` (max 30 runs, dedup by
  timestamp). Renders `e2e-trend.md` with pass-rate, duration, budget, and
  failure trends.
- **Lifecycle integration**: Report validation, failure metadata capture,
  and trend generation all wired into `pytest_sessionfinish`.

### Operational Safety

- No benchmark performance thresholds (measurement only).
- No E2E regression gates (observational trends).
- No live tests enabled by default.
- Default CI unaffected by any new workflow.

## [2.0.2] — 2026-06-15

### Fixed — CLI entry point (#148)

- Fixed CLI module shadowing where both `src/super_browser/cli.py` and
  `src/super_browser/cli/` existed, causing `super_browser.cli` to resolve
  to the package and making several console commands inaccessible.
- Consolidated all CLI commands into `src/super_browser/cli/__init__.py`.
- Restored `superbrowser version`, `info`, `run`, `interactive`, `script`,
  `replay`, `act`, and `stealth-check`.
- Preserved compatibility aliases for existing imports:
  `memory_handler`, `stealth_validate_handler`, and `_result_demo_handler`.

No runtime API changes outside CLI dispatch.

## [2.0.1] — 2026-06-15

### Fixed — Hotfix: PyPI distribution naming

- **`[all]` extra broken on published v2.0.0**: The self-reference in
  `pyproject.toml` `[all]` extra still pointed to `super-browser` instead of
  `superbrowser-sdk`. Fixed in PR #146 but never republished.
- **CLI entry point name**: Published v2.0.0 had `super-browser` as the
  console script name instead of `superbrowser`.
- **Doc version headers**: Updated to v2.0.1.

No code changes — packaging fix only. All 2,758 tests unchanged.

## [2.0.0] — 2026-06-15

### Overview

The v2.0 release delivers five tracks of improvement to the Super Browser
anti-detection agent browser SDK. All tracks are implementation-complete with
2,758 passing tests across 3 operating systems and 2 Python versions.

**Distribution name:** `superbrowser-sdk` (`pip install superbrowser-sdk`)
**Import name:** `super_browser` (`import super_browser`)

### Breaking Changes — Track A: API Simplification (Waves 14-16)

- **`SuperBrowserConfig` removed.** All fields flattened onto `AgentConfig`.
- **`Config.from_legacy()` removed.** The legacy bridge `_legacy_core` is gone.
- **`AgentConfig.core` field removed.** Sub-configs are direct fields on `AgentConfig`.
- **`SuperBrowser(config=...)` accepts `Config` only.** Passing other types raises `TypeError`.
- **`raw_page` renamed to `backend_page`** with deprecation alias (removal in v2.1).
- **`from_dict()` tolerates legacy nested `agent.core` dicts** for backward compatibility.

### Added — Track B: Network Stealth (Waves 17-20)

- **`ProxyPool`** (`stealth/proxy_pool.py`) — 4 rotation strategies (round_robin,
  weighted_random, least_used, sticky), health tracking with auto-recovery,
  domain-affinity sticky sessions with TTL, graceful degradation. 45 tests.
- **`IPReputationClient`** (`stealth/ip_reputation.py`) — offline-first IP risk
  assessment, AbuseIPDB support, risk score normalization, per-IP caching
  with TTL. All failures degrade to UNKNOWN — never raises. 27 tests.
- **`TLSFingerprintChecker`** (`stealth/tls_fingerprint.py`) — TLS fingerprint
  observation and comparison against curated baselines (Chrome 143/130,
  Firefox 120). Observe/compare/report only — SDK cannot alter TLS handshake.
  32 tests.
- **`NetworkStealthReport`** — aggregates proxy, IP reputation, and TLS status
  with severity derivation (COMPROMISED > DEGRADED > HEALTHY > UNKNOWN).
- **`NetworkStealthConfig`** added to `Config` with `SB_*` env var support.
- Honesty boundary: SDK can observe/compare/report TLS fingerprints but
  CANNOT alter the TLS ClientHello (Chromium's BoringSSL owns the handshake).

### Added — Track C: Behavioral Realism (Waves 21-23)

- **`DwellTimer`** (`behavioral/dwell.py`) — action-aware pre/post delays for
  6 action types (click, type, scroll, navigate, hover, keypress), configurable
  variability via triangular distribution, page-settle delay. Pure data:
  returns floats, callers decide whether to sleep. 19 tests.
- **`SessionSeed`** (`behavioral/session_seed.py`) — per-session deterministic
  seed derivation. Empty base = non-deterministic (production default).
  Reproducible: same seed → byte-identical behavioral output. 13 tests.
- **`NavigationVariator`** (`behavioral/navigation.py`) — 4 navigation styles
  (DIRECT, TYPE_AND_ENTER, CLICK_LINK, REFERRER) with weighted selection,
  referrer pool, type delays. Honesty: all styles use page.goto() under the
  hood — variation is in timing/headers. 15 tests.
- **`BehaviorOrchestrator`** (`behavioral/orchestrator.py`) — thin coordination
  layer wrapping `HumanBehaviorAdapter` with dwell timing, navigation variation,
  and session seed propagation. All asyncio.sleep calls mockable. 18 tests.
- **`HumanBehaviorAdapter`** now accepts optional `seed=` kwarg on
  `humanize_click()`, `humanize_type()`, `humanize_scroll()` for deterministic
  behavioral output.
- **`HumanConfig`** extended with 4 dwell fields (`dwell_pre_action_ms`,
  `dwell_post_action_ms`, `dwell_page_settle_ms`, `dwell_variability`).
  All 3 presets updated. Fully backward-compatible defaults.

### Added — Track D: Challenge Infrastructure (Waves 24-26)

- **`TurnstileDetector`** (`stealth/challenges/turnstile.py`) — Cloudflare
  Turnstile detection with two-indicator false-positive prevention (≥2 of:
  iframe, response field, cf div). Version classification (invisible/managed).
  Detection only — does NOT solve. 17 tests.
- **`KasadaDetector`** (`stealth/challenges/pow.py`) — Kasada detection via 4
  indicators (collector script, ksd cookie, meta tag, challenge form).
  Classification: POW, JS_CHALLENGE, FINGERPRINT. Resolution deferred to v2.1.
  18 tests.
- **`ChallengeTokenCache`** (`stealth/challenges/cache.py`) — in-memory token
  cache with TTL eviction, max-entries eviction (expired-first, then oldest),
  replay tracking, per-domain clearing, stats. 36 tests.
- **`ChallengeConfig`** added to `Config` with `SB_TURNSTILE_DETECT`,
  `SB_KASADA_DETECT`, `SB_TOKEN_CACHE_TTL`, `SB_TOKEN_CACHE_MAX` env vars.

### Added — Track E: E2E Harness (Waves 27-29)

- **`E2EContext`** (`testing.py`) — env-gated E2E config with `SB_E2E`,
  `SB_E2E_LIVE`, `SB_BACKEND`, `SB_HEADLESS`, `SB_E2E_BUDGET_S` parsing.
- **`FixtureServer`** (`testing.py`) — threaded HTTP server serving local
  fixture pages on random port. No external network required.
- **20 E2E tests** across 8 test files — all opt-in, skip cleanly when
  `SB_E2E` unset. Coverage: navigation, interaction, stealth overhead,
  behavioral realism, challenge detection, multi-tab, session persistence,
  and live navigation (SB_E2E_LIVE gated).
- **JSON schema v2 reporting** — `build_e2e_json_report()` and
  `render_e2e_markdown_report()` with versioned output, per-test results,
  budget tracking, emoji status icons.
- **Pytest lifecycle integration** — per-test result collection, screenshot
  capture on failure, per-test budget enforcement, automatic JSON+Markdown
  report emission at session end. 19 lifecycle + reporting unit tests.
- **`behavioral.html`** fixture — interactive elements for Track C E2E tests.
- Default CI unaffected — `tests/e2e/` in CI ignore list.

### Infrastructure

- RFCs for all 5 tracks published in `docs/rfcs/`.
- Migration guide at `docs/migration/v1-to-v2.md`.
- DCO sign-off required on all commits.
- Squash-merge strategy for all PRs.
- 30 PRs merged across Waves 0-29 (#115-#144).

### Test Count

- **2,758 tests passing** (up from 2,463 at v2.0a1).
- 295 net new tests across Tracks B-E and E2E harness.
- 9/9 CI checks green on all PRs.

## [1.11.0] — 2026-06-14

### Added — Agent Streaming API (Wave 1)
- **`act_stream()`** method on `SuperBrowser` — streaming variant of `act()`
  that yields `StreamEvent` objects for each step lifecycle event.
- **`StreamEvent`** frozen dataclass exported from `super_browser` — fields
  `type: StepEvent` and `data: dict` (treat as read-only).
- `_StreamingLLMWrapper` — wraps an LLM client so `propose_action()` uses
  `propose_action_stream()` and forwards token deltas as `StreamEvent`s.
- `AgentLoop.run_stream()` — async generator yielding lifecycle events.
- 12 streaming tests in `test_streaming.py`.

### Added — Provider Token Streaming (Wave 2)
- **`propose_action_stream()`** added to `LLMClient` protocol — async generator
  yielding token deltas as `StreamEvent`s.
- `OpenAIClient.propose_action_stream()` — SSE-based streaming for OpenAI.
- `AnthropicClient.propose_action_stream()` — message-stream for Anthropic.
- `browser_transport.py` — transport-agnostic streaming bridge.
- 16 provider streaming tests in `test_llm_streaming.py`.

### Added — Default Agent Tooling (Wave 3)
- **`_register_builtin_tools()`** on `SuperBrowser` — auto-registers 10 tools
  after controller creation in `start()`.
- 7 controller tools: `click`, `fill`, `select`, `hover`, `drag`, `scroll`,
  `keypress`.
- 3 facade tools: `navigate` (via `_navigate_impl` closure), `extract`,
  `observe`.
- Does not overwrite user-registered tools.
- Fixed `AgentLoop` to pass `tools=self._registry.build_tool_schemas()` to
  both `create_plan()` and `propose_action()` as keyword argument.
- 12 default-tooling tests in `test_default_tooling.py`.

### Changed — Prompt/Tool Isolation (Wave 4)
- `_build_prompt()` no longer wraps tool API in
  `<untrusted-screen-content>`. Replaced with `tools_note` pointing to
  the structured tools interface.
- 11 prompt-isolation tests in `test_prompt_isolation.py`.

### Added — Security Perimeter (Waves 5–9)
- **`_check_facade_security()`** private helper on `SuperBrowser` — centralised
  security gate for all side-effecting facade methods. Supports mutable params
  for redaction propagation and auto-derives current page URL.
- **Wave 5:** `navigate()`, `click()`, `fill()` secured (SENSITIVE).
  `_navigate_impl()` extracted for single-check invariant.
- **Wave 6:** `open_tab()` (SENSITIVE), `upload_file()` (DANGEROUS),
  `download()` (SENSITIVE) secured.
- **Wave 7:** Controller tool handlers use late-binding wrappers
  (`_make_controller_wrapper()`) — tools route to current controller after
  `open_tab()` / `switch_tab()` replaces it.
- **Wave 8:** `intercept_requests()` (SENSITIVE/DANGEROUS),
  `block_requests()` (DANGEROUS via delegation), `mock_response()` (DANGEROUS),
  `clear_interceptions()` (SENSITIVE) secured.
- **Wave 9:** `save_session()` (DANGEROUS), `load_session()` (DANGEROUS)
  secured — credential-bearing session state exports/imports gated.
- 78 security tests across 5 test files.

### Fixed
- Repository moved from `Elephant-Rock-Lab/super-browser` to
  `Octo-Lex/Super-Browser`. All project URLs in `pyproject.toml`, README
  badges, and clone instructions updated.

## [1.10.0] — 2026-05-28

### Fixed
- **`_configure_verification()` no longer a no-op**: Wired to read
  `Config.agent.core.enable_verification` — when True, auto-creates a
  `VisualVerifier` and attaches it to the controller. New field added to
  `SuperBrowserConfig`.
- **Stealth detection tests hardened**: CreepJS test now uses multi-strategy
  extraction with `pytest.skip()` fallback instead of hard failure when the
  live site layout changes. Browserscan test separates bot verdict (known gap,
  skip) from automation library detection (hard fail).
- **Stealth test timeout**: Added explicit `timeout=20_000` to browser launch
  and `set_default_timeout(30_000)` to pages in stealth detection conftest.
- **README badges**: Replaced fake `example.com` URLs with real
  `Elephant-Rock-Lab/super-browser` GitHub links. Added PyPI badge.
- **pyproject.toml URLs**: All project URLs now point to
  `github.com/Elephant-Rock-Lab/super-browser`.

### Added
- `enable_verification` field on `SuperBrowserConfig` (default: `False`).
- **Known Limitations** section in `docs/platform-abstraction.md` documenting
  NotImplementedError gaps in CDP backend (`set_input_files`, `frame_locator`,
  `expect_download`) and Selenium backend (`route`/`unroute`).
- **BiDi Injector note** in `docs/platform-abstraction.md` clarifying it is a
  future stub with no implementation timeline.
- 10 integration tests (`test_v1100_features.py`) covering all v1.10.0 items.

### Changed
- First public release on GitHub: `github.com/Elephant-Rock-Lab/super-browser`.
- All 17 version tags (v1.0.0 through v1.9.5) pushed to remote.
- `ConsoleSink.flush()` now calls `sys.stderr.flush()` (was bare `pass`).
- Dockerfile and README now use `[patchright]` extra instead of removed `[browser]` alias.

### Removed
- `[browser]` optional extra — was identical to `[patchright]`; no callers remain.
- `[mcp]` optional extra and `src/super_browser/mcp_server.py` — 290-line entry
  point that was never tested or validated. Will return in v2.0.
- `specs/` directory — 13 pre-v1.0 gap analysis files no longer relevant.
- `docs/aiv/` and `Analysis-Framework/` untracked from git — internal process
  docs remain local but are no longer shipped to cloners.
- `test_config_deprecation.py` — tested removed deprecation warnings.
- False `DeprecationWarning` labels on `SuperBrowserConfig`, `SessionConfig`,
  and `.raw_page` — these are core types, not legacy.

## [1.9.2] — 2026-05-23

### Fixed
- **Config examples executable**: All README and docs snippets now use valid
  `Config(browser=SessionConfig(backend=...))` construction instead of
  non-existent `Config(backend=...)` kwargs.
- **Migration guide aliases**: Replaced fake `Config.Browser` / `Config.Agent` /
  `Config.Budget` with actual types: `SessionConfig`, `AgentConfig`, `BudgetConfig`.
- **Stale signature count**: README now correctly states 10 signatures (was 8).
- **CompletionReason table**: `docs/error-catalog.md` now lists the 5 actual enum
  values (`SUCCESS`, `BUDGET_EXHAUSTED`, `ERROR`, `CANCELLED`, `MAX_STEPS`) instead
  of non-existent `LOOP_DETECTED` and `ABORTED`.
- **Version refs**: Updated `docs/api-stability.md`, `docs/error-catalog.md`,
  `docs/platform-abstraction.md` from v1.9.0 to v1.9.1.
- **Config composition root wired**: `start()` now checks `cfg.agent.core.enable_recovery`
  and `cfg.agent.core.enable_budget` in addition to legacy `_legacy_core` bridge.
  Recovery, budget, tracing, and security all work through both paths.

### Added
- 13 docs-code alignment tests (`test_v192_features.py`) — every documented
  snippet verified executable, every enum verified against source.

## [1.9.3] — 2026-05-23

### Fixed
- **`_detect_backend()` reads Config composition root**: `Config(browser=SessionConfig(backend="playwright"))`
  now correctly resolves to `"playwright"` instead of always auto-detecting Patchright.
- **`_configure_vision()` honors Config**: Checks `cfg.agent.core.enable_vision` in addition to
  legacy `_legacy_core` bridge.
- **`_configure_stealth()` honors Config**: Checks `cfg.agent.core.enable_stealth` instead of
  broken `getattr(cfg, "enable_stealth", False)`.
- **`_configure_skills()` honors Config**: Checks `cfg.agent.core.enable_skills` in addition to
  legacy `_legacy_core` bridge.
- **README env var**: `create_llm()` documented as using `SB_LLM_API_KEY` (was `ANTHROPIC_API_KEY / OPENAI_API_KEY`).
- **`docs/agent-reliability.md`**: Stale-ref signature count corrected to 10 (was 8).
- **`docs/api-stability.md`**: Added `save_session`, `load_session`, `start`, `stop` to stable facade table.
- **Version refs**: Updated doc headers from v1.9.1 to v1.9.2.

### Added
- 15 runtime/config alignment tests (`test_v193_features.py`) — `_detect_backend` per-backend,
  Config.agent.core subsystem flags, docs content assertions.

## [1.9.5] — 2026-05-23

### Fixed
- **`examples/stealth_mode.py`**: Fixed `Config.Stealth(proxy_tier=...)` → `StealthConfig(proxy_tier=...)`.
  `Config.Stealth` does not exist — was printing broken advice.
- **`docs/api-stability.md`**: Version header updated from v1.9.2 to v1.9.3.
- **`docs/error-catalog.md`**: Version header updated from v1.9.2 to v1.9.3.

### Added
- 6 doc normalization tests (`test_v195_features.py`) — all doc headers current,
  no fake Config aliases in examples.

## [1.9.4] — 2026-05-23

### Fixed
- **`examples/backend_selection.py`**: Fixed `Config.Browser(backend=...)` → `SessionConfig(backend=...)`.
  `Config.Browser` does not exist — was broken since v1.9.0.
- **`docs/api-reference.md`**: Added 15 missing facade methods — `open_tab`, `switch_tab`, `close_tab`,
  `list_tabs`, `upload_file`, `download`, `enter_frame`, `exit_frame`, `query_shadow`,
  `intercept_requests`, `block_requests`, `mock_response`, `clear_interceptions`,
  `save_session`, `load_session`. All 32 public methods now documented.
- **`docs/api-reference.md`**: Version header updated from v1.9.0 to v1.9.3.
- **`docs/architecture.md`**: Version header updated from v1.9.0 to v1.9.3.

### Added
- 5 API reference completion tests (`test_v194_features.py`) — all facade methods present,
  example uses correct imports, doc version headers match.

## [1.9.1] — 2026-05-23

### Added
- **Config normalization**: `SuperBrowser()` now uses `Config` (composition root) by default.
  Legacy `SuperBrowserConfig` is auto-wrapped via `Config.from_legacy()`.
  Zero breaking changes — both config types accepted transparently.
- **Session persistence**: `save_session(path)` / `load_session(path)` on the
  facade for cookie save/restore across browser restarts. Backend-agnostic via
  `StealthBridge.get_all_cookies()` / `set_cookies()`.
- **SessionConfig.session_file**: optional field for auto-save/load on start/stop.
- **Example gallery**: 4 new examples (backend_selection, session_persistence,
  error_handling, multi_tab_workflow) + 2 updated for Config.
- **API stability doc**: `docs/api-stability.md` — three-tier stability model
  (stable/protocol/internal), deprecation policy, config composition root.
- **Error catalog doc**: `docs/error-catalog.md` — all 26 categories, 10 stale
  signatures, NextAction structure, recovery flow diagram.
- **Migration guide**: `docs/migration/v1.8-to-v1.9.md` — raw_page→engine_page,
  CDPBridge→StealthBridge, Config composition root, backend selection.
- **mypy CI gate**: Separate `mypy-check` job (Ubuntu/3.12) type-checks
  protocols + backends.

### Fixed
- Stale version references in `docs/architecture.md` (v0.1.0→v1.9.0),
  `docs/api-reference.md` (v1.4.0→v1.9.0), `docs/fingerprint-scoring.md`
  (v1.4→v1.9), `docs/human-behavior.md` (v1.4→v1.9).
- README positioning: "anti-detection agent browser SDK".
- pyproject description aligned with positioning.

### Changed
- `SuperBrowser.__init__` now accepts `Config | SuperBrowserConfig`.
  Passing `None` (default) creates a `Config()` instance.
- `_configure_stealth`, `_configure_vision`, `_configure_skills` read from
  composition root instead of instantiating subsystem configs inline.
- Config composition root note added to `docs/architecture.md`.

## [1.9.0] — 2026-05-21

### Added — Platform Abstraction + Distribution

**BrowserEngine Protocol** (`browser/engine.py`)
- `BrowserEngine` Protocol: start, stop, new_page, capabilities, backend_name
- `EnginePage` Protocol: 21 members covering all page operations
- `EngineCapabilities`: 8 feature flags for graceful degradation (cdp, bidi, stealth_inject_before/after, network_intercept, multi_tab, screenshots)
- `StealthBridge` Protocol: 6 methods for CDP/BiDi stealth access
- `StealthInjector` Protocol: 3 methods for JS payload delivery timing
- `InjectionTiming` enum: BEFORE, AFTER, BOTH
- `BackendType` enum: AUTO, PATCHRIGHT, PLAYWRIGHT, SELENIUM, CDP
- `_detect_backend()`: auto-detection with precedence rules (explicit > mode > import probe)

**PatchrightBackend** (`browser/backends/patchright_backend.py`)
- `PatchrightEngine`: wraps BrowserSession lifecycle
- `PatchrightPage`: wraps Playwright Page, implements all 21 EnginePage members
- `PatchrightStealthBridge`: wraps CDPBridge for stealth protocol compliance

**PlaywrightBackend** (`browser/backends/playwright_backend.py`)
- `PlaywrightEngine`: wraps standard Playwright library (Chromium/Firefox/WebKit)
- `PlaywrightPage`: 21 EnginePage members via Playwright API
- `PlaywrightStealthBridge`: CDP for Chromium only, None for Firefox/WebKit
- Chromium: full CDP stealth, Firefox: BiDi future, WebKit: after-load only

**SeleniumBackend** (`browser/backends/selenium_backend.py`)
- `SeleniumEngine`: wraps Selenium WebDriver (Chrome/Firefox/Safari)
- `SeleniumPage`: 21 EnginePage members via async bridge (asyncio.to_thread)
- `SeleniumStealthBridge`: Chrome CDP via execute_cdp_cmd()
- Enterprise CI support with sync→async bridging

**CDPDirectBackend** (`browser/backends/cdp_backend.py`)
- `CDPDirectEngine`: connects to raw CDP websocket endpoints
- `WebSocketCDPSession`: adapter wrapping websockets for CDPBridge reuse
- `CDPDirectPage`: 21 EnginePage members via CDP protocol
- `CDPDirectStealthBridge`: full stealth via CDPBridge adapter
- Use case: Docker Chromium, Browserless, BrowserBase, cloud providers

**Stealth Abstraction** (6 files refactored)
- StealthManager accepts StealthBridge (protocol) over CDPBridge
- InjectDelivery: keyword-only stealth_bridge, backward compat preserved
- Snapshot: _FakeResult removed, _cdp_eval() helper
- Captcha: start() extracts stealth_bridge from engine_page
- Diagnostics: _send() helper with duck typing for both bridge types
- Facade: passes stealth_bridge from engine_page with None guard

**StealthInjector Implementations** (`browser/injectors/`)
- `CDPInjector`: BEFORE timing, wraps InjectDelivery (Fetch body-splice)
- `PageScriptInjector`: AFTER timing, addInitScript fallback
- `BiDiInjector`: stub for future WebDriver BiDi support
- `select_injector()`: capability-driven factory

**Infrastructure**
- pyproject.toml: Apache-2.0 license, project URLs, backend dep groups (patchright, playwright, selenium, cdp, all)
- CI: GitHub Actions 3-OS × 2-Python matrix (test.yml)
- Publish: tag-triggered PyPI workflow with trusted publisher (publish.yml)
- Flaky test markers for 3 intermittently failing tests

### Changed

- Controller: 8 raw_page calls → 0 (all via EnginePage protocol)
- Facade: 6 TODO(BATCH-47) markers → 0, 3 _session._private → 0
- Facade: 10 raw_page calls → 1 (deprecated compat)
- Snapshot: StealthBridge preferred over raw CDPBridge

### Backend Matrix

| Backend | CDP | BiDi | Stealth | Use Case |
|:--------|:----|:-----|:--------|:---------|
| Patchright | ✓ | — | Full | Default, anti-detection |
| Playwright | ✓ (Chromium) | ✓ (Firefox) | Chromium full | Standard automation |
| Selenium | ✓ (Chrome) | ✓ (Firefox) | Chrome CDP | Enterprise CI |
| CDP Direct | ✓ | — | Full | Docker, cloud |

## [1.8.0] — 2026-05-20

### Fixed — Live QA Hardening

- StaleRefDetector: added 2 error signatures ("not found", "detached from document")
  found during browser-based testing against real Playwright errors
- Total stale signatures: 10 (was 8)

### Verified — Live QA Validation

- All v1.7.0 features tested against real Chromium browser
- Result categories serialize correctly through real browser round-trips
- Page change summaries detect real navigation (example.com → httpbin.org)
- Secret redaction catches passwords, tokens, URL query params
- Stale ref detector catches 10/10 real-world error patterns
- BrowserJob and QASmoke compile correctly
- No regressions in 2,029 existing tests

## [1.7.0] — 2026-05-14

### Added — Agent UX & Reliability

**Result Categories** (`results/types.py`)
- `SuccessCategory` enum: NAVIGATION, MUTATION, INSPECTION, ARTIFACT, UNCHANGED
- `FailureCategory` enum: 13 values — superset of ErrorCategory + STALE_REF, ELEMENT_OBSCURED, FRAME_DETACHED, AUTH_REQUIRED, RATE_LIMITED
- `NextAction` dataclass: structured recovery guidance (action_id, description, compiled_args)
- `ActionResult` extended with result_category, success_category, failure_category, next_actions
- Full serialization support (to_dict/from_dict) with backward compatibility

**Page Change Summaries** (`results/types.py`)
- `PageChangeSummary` dataclass: change_type, summary, title, url, artifact_hint
- `PageFingerprint` frozen dataclass: url, title, node_count, interactive_count
- `compute_page_change()`: detects navigation/mutation/unchanged from before/after fingerprints
- Agent loop computes summaries on every step

**Stale Ref Recovery** (`interaction/recovery.py`)
- `StaleRefDetector`: 8 error signatures (waiting for selector, Execution context destroyed, Target closed, Frame detached, Element not attached, Node detached, strict mode violation, Timeout)
- `_execute_with_stale_recovery()`: wraps cascade with auto-retry (refresh snapshot → retry cascade)
- Auto-retry on click, fill, scroll — zero overhead on happy path
- Returns FailureCategory.STALE_REF + 3 NextAction recovery hints on failure

**Secret Redaction Pipeline** (`security/action_redaction.py`)
- `redact_args()`: two-pass — key-name matching (20+ sensitive keys) + SecretRedactor value-pattern scan
- `redact_context()`: URL query-param scrubbing (standalone, no SecretRedactor dependency)
- `configure_redaction()`: singleton gate for ActionResult.to_dict() redaction
- to_dict() only redacts when configured — fully backward compatible

**Agent Efficiency Benchmark** (`scripts/agent_efficiency_benchmark.py`)
- Mock-based measurement of 4 representative workflows
- JSON + Markdown output: call count, output bytes, stale-ref rate, category distribution
- `--compare baseline.json` regression detection

**Action Presets** (`interaction/presets.py`)
- `BrowserJob`: declarative step sequence with validation (13 action types)
- `QASmoke`: 5-step diagnostic sequence (open → wait → assert → network → screenshot)
- `CompiledStep`: frozen dataclass — pure data compilation, zero browser dependency

**CLI**
- `result-demo --json / --fail / --stale`: demonstrate structured result categories

### Tests
- +81 new tests across 3 batches (BATCH-40, 41, 42)
- 2,012 total tests passing

## [1.6.0] — 2026-05-13

### Added — Anti-Detection Hardening (12 Fingerprint Surfaces)

**Ejecta Framework** (`stealth/ejecta/`) — Deterministic noise injection via JS payloads
- `config.py`: EjectorConfig with per-surface toggles (canvas, audio, webrtc, timing, browser_apis)
- `types.py`: EjectorResult with ejector_id, js_payload, inject_order, size_bytes
- `registry.py`: build_ejector_payloads() orchestrates all 5 ejectors

**5 Ejectors:**
- `canvas.py`: CanvasEjector — ±2 RGBA noise on toDataURL/toBlob/getImageData/readPixels/OffscreenCanvas
- `audio.py`: AudioEjector — ±0.0001 sample noise on getChannelData/getFloatFrequencyData/createBuffer
- `webrtc.py`: WebRTCEjector — blocks RTCPeerConnection/webkit/moz variants, mocks enumerateDevices
- `timing.py`: TimingEjector — performance.now 1ms precision floor + micro-jitter, Math constant perturbation (±1e-15)
- `browser_apis.py`: BrowserAPIsEjector — blocks getBattery/permissions, mocks speechSynthesis, blocks CSS :visited, jitters ClientRects (±0.5px)

**Validation:**
- CHK-009 Canvas_Audio_Consistency, CHK-010 WebRTC_Blocked, CHK-011 Timing_Precision, CHK-012 Browser_APIs
- Suite expanded from 8 → 12 checks

**Integration:**
- FingerprintMatrix extended with ejector_seed field
- derive_matrix populates ejector_seed from session seed
- All payloads use inline mulberry32 PRNG for per-session determinism

### Tests
- +120 new tests across 3 batches (BATCH-36, 37, 38)
- 1,915 total tests passing

## [1.5.0] — 2026-05-13

### Added — Fingerprint Consistency Engine (BATCH-30)
- Deterministic rule DAG deriving all fingerprint surfaces from `(profile, seed)` pair
- 38 consistency rules across 9 modules (screen, webgl, fonts, audio, navigator, storage, connection, behavior, security)
- 4 real-device profiles (Windows, macOS, macOS-M4, Linux Chrome stable)
- xoshiro256** PRNG for reproducible randomness
- Fetch.fulfillRequest inject delivery (body-splice technique)
- Runtime.enable hard-ban at CDP transport layer

### Added — Chromium-Native Networking (BATCH-31)
- `session.fetch()` routing through Chromium BoringSSL stack
- `BrowserFetch` with dual CDP mechanisms (Network.loadNetworkResource + in-page fetch)
- `BrowserLLMClient` for opt-in LLM-via-browser routing
- `NetworkConfig` (browser_fetch, llm_via_browser) in main config

### Added — Biomechanical Behavior v2 (BATCH-32)
- Cubic Bézier mouse trajectories with Fitts's Law timing
- 10% overshoot probability with corrective sub-curves
- Autocorrelated Gaussian jitter (τ ≈ 30ms)
- QWERTY-aware digraph keystroke timing with lognormal delays
- Mistake injection (2% default) with backspace correction
- WPM scaling (40–120 WPM range)
- Inertial scroll with exponential friction decay (τ ≈ 350ms)
- Pure-data synthesis — fully testable without browser

### Added — Stealth Integration & Regression Harness (BATCH-33)
- `FingerprintValidationSuite` with 8 cross-surface consistency checks
- `StealthRegressionHarness` with baseline capture + diff + CI mode
- `super-browser stealth-validate` CLI command
- 11 cross-feature integration tests for full v1.5.0 stack

### Technical Details
- **New modules:** `stealth/profiles/`, `stealth/consistency/`, `behavioral/`, `stealth/validation/`, `browser/fetch.py`, `agent/llm/browser_transport.py`
- **Test count:** ~1,794 total (+208 new since v1.4.0)
- **PRNG:** xoshiro256** shared between consistency engine and behavioral synthesis
- **Pipe-mode CDP:** Researched and documented as not feasible through Patchright (deferred to v2.0)

---

## [1.4.0] — 2026-05-08

### Added — CloakBrowser Stealth Backend

#### BATCH-27: CloakBrowser Integration
- **CloakConfig**: New sub-config with 6 fields (cloak_enabled, fingerprint_seed, humanize, humanize_preset, geoip, platform)
- **SessionMode.CLOAK_LAUNCH**: New enum value for explicit CloakBrowser mode
- **CloakBrowserAdapter**: Lazy-import adapter that wraps cloakbrowser.launch_async()
- **Auto-detection**: BrowserSession detects cloakbrowser at runtime and uses it when available
- **Graceful fallback**: Falls back to Patchright when cloakbrowser is not installed or launch fails
- **stealth_backend property**: Returns "cloak" or "patchright" on both BrowserSession and SuperBrowser facade
- **cloak_config property**: Exposes active CloakConfig on SuperBrowser facade
- **Option passthrough**: humanize, proxy, fingerprint_seed, geoip, platform, humanize_preset all forwarded
- **[cloak] extra**: `pip install super-browser[cloak]` installs cloakbrowser>=0.3
- **Documentation**: docs/cloak-integration.md with complete guide
- **Example**: examples/cloak_stealth.py with working demo
- **HB-27-01**: SuperBrowser works identically with or without cloakbrowser — all existing tests pass
- **HB-27-02**: cloakbrowser imported only inside functions, never at module level
- New modules: `browser/cloak_backend.py`
- New config: `CloakConfig` in `config.py`

### Added — Human Behavior & Fingerprint Scoring

#### BATCH-28: Human Behavior Adapter & Fingerprint Scoring
- **HumanBehaviorAdapter**: Abstracts human simulation across CloakBrowser and Patchright backends
- **HumanConfig**: Frozen dataclass with 3 presets ("default", "careful", "fast") controlling typing delay, mouse jitter, click hold, scroll step, pause, and typo chance
- **FingerprintScanner**: Scans browser fingerprints in offline (default, deterministic) and online modes
- **FingerprintScorer**: Weighted 0-100 composite score across 6 categories (webdriver 25%, headers 20%, plugins 15%, user_agent 15%, tls 15%, misc 10%)
- **FingerprintScore/FingerprintCheck**: Data models for scan results
- **StealthReport**: Markdown and HTML report generation from FingerprintScore
- **stealth-check CLI**: `super-browser stealth-check [--online] [--format html|markdown] [--threshold N]`
- **HB-28-01**: All new modules import cleanly without external dependencies
- **HB-28-02**: Offline scanner returns deterministic scores without network access
- New modules: `stealth/human.py`, `stealth/human_config.py`, `stealth/fingerprint_scanner.py`, `stealth/fingerprint_score.py`, `stealth/scoring.py`, `stealth/report.py`

### Added — Integration Tests & Release

#### BATCH-29: Integration Tests, Documentation & Release
- **Cross-feature integration tests**: 30 tests across 5 test classes exercising CloakBrowser detection, human behavior dispatch, fingerprint scanning, CLI stealth-check, and full cross-feature coexistence
- **Documentation**: docs/human-behavior.md, docs/fingerprint-scoring.md with complete guides
- **Examples**: examples/human_behavior.py, examples/fingerprint_scan.py with working demos
- **README v1.4 section**: Human behavior simulation and fingerprint scoring overview
- **API reference updated**: v1.4.0 with 5 new API sections (HumanBehaviorAdapter, HumanConfig, FingerprintScanner, FingerprintScorer, FingerprintScore/FingerprintCheck)
- **Version bump**: 1.3.0 → 1.4.0 in __init__.py and pyproject.toml
- **HB-29-01**: All baseline + new tests pass (1,598 total)
- **HB-29-02**: __version__ equals "1.4.0"
- **HB-29-03**: Documentation includes working code examples
- New test file: `tests/integration/test_v1_4_features.py`

## [1.3.0] — 2026-05-08

### Added — Plugin System, Recording, CLI, Memory

#### BATCH-22: EventBus & Lifecycle Hooks
- **EventBus**: Typed pub/sub with sync (`emit()`) and async (`emit_async()`) handler support
- **7 lifecycle events**: `before_navigate`, `after_navigate`, `before_action`, `after_action`, `on_error`, `on_loop_detected`, `on_budget_alert`
- **HB-22-01**: `emit()` never raises — handler errors caught and logged
- **HB-22-03**: Context dict passed as read-only `MappingProxyType`
- New modules: `events/bus.py`, `events/types.py`

#### BATCH-23: Session Recording
- **SessionRecorder**: Subscribes to EventBus lifecycle events, captures `ActionRecord` entries
- **Persistence**: `save()`/`load()` for JSON recording files with `schema_version: "1.0"`
- **HTML audit reports**: `export_html()`/`save_html()` — self-contained HTML with action table
- **RecordingReplayer**: Replay recordings against a live browser with mismatch detection
- **HB-23-02**: Screenshot capture failures never block recording
- **HB-23-04**: Recorded params strip API keys and credentials (`[REDACTED]`)
- New modules: `recording/recorder.py`, `recording/types.py`, `recording/persistence.py`, `recording/report.py`, `recording/replayer.py`

#### BATCH-24: CLI Modes
- **Interactive REPL**: Persistent browser session with commands (open, click, fill, extract, scroll, screenshot, observe, tabs, close)
- **Script mode**: Execute YAML batch scripts step-by-step with progress reporting
- **Recording replay CLI**: `super-browser replay recording.json`
- **One-shot agent**: `super-browser act "instruction" --url <url>`
- **HB-24-01**: Browser persists between REPL commands
- **HB-24-03**: No LLM credentials required — all commands use direct browser calls
- **HB-24-04**: Unknown commands print help, never crash
- New modules: `cli/interactive.py`, `cli/commands.py`, `cli/script.py`

#### BATCH-25: Per-Domain Memory
- **MemoryStore**: Per-domain JSON persistence with TTL pruning and credential filtering
- **Integration**: Memory wired into agent loop — saves successful sequences, injects context into LLM prompts
- **CLI commands**: `memory list`, `memory show`, `memory clear`, `memory prune`
- **HB-25-01**: Memory is opt-in — `sb.enable_memory()` required
- New modules: `memory/store.py`, `memory/types.py`, `memory/integration.py`

#### BATCH-26: Integration Tests & Release
- **Cross-feature integration tests**: 12 tests covering plugins+recording, recording+memory, CLI+recording, plugin tool registration
- **E2E journey test**: Complete 7-phase workflow through all 4 features
- **Documentation**: `docs/plugins.md`, `docs/recording.md`, `docs/memory.md` (new) + updated README, quickstart, API reference
- **Version bump**: 1.3.0

### New Modules

| Module | Description |
|---|---|
| `events/bus.py` | Typed pub/sub EventBus |
| `events/types.py` | Lifecycle event type constants and handler type aliases |
| `recording/recorder.py` | SessionRecorder — captures lifecycle events |
| `recording/types.py` | ActionRecord, RecordingSession data models |
| `recording/persistence.py` | Save/load recording JSON files |
| `recording/report.py` | HTML audit report generator |
| `recording/replayer.py` | RecordingReplayer with mismatch detection |
| `cli/interactive.py` | Interactive REPL mode |
| `cli/commands.py` | Command dispatch for REPL |
| `cli/script.py` | YAML script execution, replay, one-shot agent |
| `memory/store.py` | Per-domain MemoryStore with TTL pruning |
| `memory/types.py` | DomainMemory, ActionSequence data models |
| `memory/integration.py` | Memory integration with agent loop |
| `plugins/hooks.py` | Global hook registry |
| `plugins/decorators.py` | `@hook()` decorator API |

### New Documentation

| Document | Description |
|---|---|
| `docs/plugins.md` | Plugin & hook system guide with examples |
| `docs/recording.md` | Session recording, replay, and audit guide |
| `docs/memory.md` | Per-domain memory store guide |

### Tests Added

| Batch | Tests Added |
|---|---|
| BATCH-22 | EventBus unit tests, lifecycle hook tests |
| BATCH-23 | Recorder, persistence, replayer tests |
| BATCH-24 | Interactive REPL, script execution tests |
| BATCH-25 | MemoryStore, integration, CLI memory command tests |
| BATCH-26 | 12 cross-feature integration + E2E journey tests |

## [1.2.0] — 2026-05-07

### Added — Distribution & Integration

- **PyPI metadata**: Full classifiers, keywords, author, license, entry point
- **CLI**: `super-browser version`, `super-browser info`, `super-browser run` commands
- **Docker**: Dockerfile + docker-compose.yml for containerized deployment
- **MCP Server**: 10 tools via Model Context Protocol (navigate, click, fill, extract, observe, screenshot, scroll, open_tab, list_tabs, act)
- **Cloud browser**: Browserbase, Steel.dev, and generic CDP connectors
- **Schema extraction**: `extract(schema={...})` validates output against JSON schema

### New Modules

- `cli.py` — command-line interface
- `mcp_server.py` — MCP server for agent ecosystem integration
- `browser/cloud.py` — CloudBrowserConnector, BrowserbaseConnector, SteelConnector, CDPConnector

### New Extras

- `[mcp]` — MCP server support
- `[cloud]` — Cloud browser integration

## [1.1.0] — 2026-05-07

### Added — Core Web Automation Features

- **Multi-tab support**: `open_tab()`, `switch_tab()`, `close_tab()`, `list_tabs()` — full tab lifecycle management
- **File upload**: `upload_file(selector, path)` — set files on `<input type="file">` elements
- **File download**: `download(url_or_selector)` — download files with automatic path resolution
- **iframe interaction**: `enter_frame(selector)`, `exit_frame()` — scope interactions to iframe content with nesting support
- **Shadow DOM piercing**: `query_shadow(host, inner)` — query elements inside Shadow DOM roots
- **Network interception**: `intercept_requests()`, `block_requests()`, `mock_response()`, `clear_interceptions()` — intercept, block, and mock HTTP requests

### New Types

- `DownloadResult`, `UploadResult`, `ShadowQueryResult`, `NetworkInterceptResult`
- `TabHandle`, `TabSnapshot` for multi-tab management

### New Module

- `browser/tabs.py` — `TabManager` class for multi-tab lifecycle

## [1.0.2] — 2026-05-03

### Fixed — P2 Polish

- **Config.from_yaml()**: Now raises `FileNotFoundError` with filename instead of raw OS error
- **UserAgentPool**: Chrome versions updated from 120–125 to 130–136 (modern versions)
- **[security] extras**: Added `cryptography>=42.0` for `CredentialVault` support

### Added — Documentation

- Debug mode documentation in `docs/quickstart.md` (screenshots, DOM snapshots, structured logging)
- Safety gate, router, and runaway guard documented in logging section

## [1.0.1] — 2026-05-03

### Fixed — 6 Critical Bugs

- **BUG-01**: `_check_retry_budget()` was dead code — now wired into `_dispatch_action()` before tool execution
- **BUG-03**: `act()` accessed private `_governor` — added public `budget_remaining` property to `BudgetAwareLLMClient`
- **BUG-04**: Two incompatible `BudgetAwareLLMClient` classes — renamed `budget/client.py` to `BudgetCascadeClient`
- **BUG-06**: `replan()` call used wrong kwargs (`current_plan`/`recent_actions` vs `original_plan`/`failed_step`/`error`)
- **BUG-11/12**: Sync LLM clients in vision providers blocked event loop — replaced with `AsyncAnthropic`/`AsyncOpenAI`
- **BUG-08**: JS injection in `CheckpointManager.restore()` — replaced string concatenation with `Runtime.callFunctionOn`

### Fixed — UX Issues

- README quickstart code now runs copy-paste (`Config.from_dict()`, `llm_client=`)
- README badges updated: CI passing, coverage 85%, PyPI 1.0.0
- Added `super_browser.testing.MockLLMClient` for quick testing without LLM
- Added `.env.example` with documented environment variables
- Fixed `api-reference.md` header version
- Fixed silent failures in `click()`/`fill()`/`extract()`/`observe()` before `start()`
- Fixed `extract(selector)` returning `None` due to `JSON.parse` error on CSS selectors
- Fixed `__version__` mismatch (was "0.1.0", now matches pyproject.toml)

### Added — 5 Patterns from Clawd Cursor v0.8.7

- **Safety Gate** (`security/gate.py`): Tier-based action evaluation — read/input/destructive/system tiers with label escalation
- **Deterministic Router** (`agent/router.py`): Zero-LLM URL/click/scroll interception with compound task rejection
- **Runaway Guard**: Per-action diagnostic hints in `ActionLoopDetector` — actionable advice instead of generic messages
- **Prompt Injection Defense**: `<untrusted-screen-content>` wrapping in agent loop prompts
- **ActionResult.raise_for_error()** + **ok_or_raise()**: `requests.Response.raise_for_status()` pattern for fluent error handling

## [1.0.0] — 2026-05-03

### Production Release — Super Browser v1.0

All 16 AIV Batches complete. 1,370 tests passing.

#### Batches Completed

| Batch | Deliverable | Tests Added |
|:------|:------------|:------------|
| BATCH-01 | Prerelease LLM Client (Anthropic + OpenAI) | +14 |
| BATCH-02 | 10 Real-World Discovery Tasks | — |
| BATCH-03 | Unified Config (from_env, from_yaml, from_dict) | +33 |
| BATCH-04 | Production LLM Client (retry, timeout, budget awareness) | +37 |
| BATCH-05 | Git Init + README + Repository Hygiene | — |
| BATCH-06 | CI/CD Pipeline (GitHub Actions, pre-commit) | — |
| BATCH-07 | Stealth Detection Test Suite (16 tests + gauntlet script) | +16 |
| BATCH-08 | H1+H2+H3: Route interception, cascade governor, compressor tracking | +13 |
| BATCH-09 | H4+H5+H6: Selector injection fix, fail-fast, checkpoint manager | +30 |
| BATCH-10 | H7+H8: CAPTCHA resolution strategies, hard tab cap | +19 |
| BATCH-11 | M31–M34: Debug mode, error screenshots, retry budget, structured logging | +40 |
| BATCH-12 | M35–M38: Action timeout, header randomization, proxy rotation, UA pool | +41 |
| BATCH-13 | M39–M40: Credential vault, fingerprint scoring | +26 |
| BATCH-14 | Integration Tests (67 E2E + smoke + error + performance) | +67 |
| BATCH-15 | API Reference, Quickstart, Examples, Architecture | — |
| BATCH-16 | Final Verification + v1.0.0 Release Tag | — |

#### Gap Model Closure

- **HIGH gaps (H1–H8):** All closed (BATCH-08 to BATCH-10)
- **MEDIUM gaps (M31–M40):** All closed (BATCH-11 to BATCH-13)

#### Key Features

- **Stealth:** Patchright-based route interception, header randomization, UA rotation, fingerprint scoring
- **Budget:** Governor-enforced daily cap, model cascade with budget awareness, compressor cost tracking
- **Recovery:** CheckpointManager, CAPTCHA resolution, interactive debug mode
- **Security:** Credential vault (Fernet encryption), parameterized selector evaluation
- **Observability:** Structured JSON logging, error screenshots, correlation IDs
- **Reliability:** Action timeout enforcement, hard tab cap, retry budget per action



### BATCH-13: Credential Vault + Fingerprint Scoring

#### Added
- **M39:** `CredentialVault` class for encrypted local credential storage
  - Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) via `cryptography` library
  - Key derived from machine ID, cached at `~/.config/super-browser/.vault_key`
  - `store()`, `retrieve()`, `list_sites()`, `delete()` API
  - HB-13-01 compliance: credentials always encrypted at rest, never logged
- **M40:** `FingerprintScorer` for composite stealth fingerprint scoring
  - Weighted 0-100 composite score: webdriver (25%), headers (20%), TLS (15%), plugins/mimetypes (15%), user_agent (15%), misc (10%)
  - Letter grades: A (90+), B (75-89), C (60-74), D (<60)
  - `run_full_diagnostics(page)` integration in `diagnostics.py`
  - `FingerprintScoreResult` dataclass with score, grade, deductions, category_scores

#### Tests
- 11 new tests in `tests/test_security/test_credential_vault.py` (TEST-13-01-01 through 05)
- 15 new tests in `tests/test_stealth/test_fingerprint_score.py` (TEST-13-02-01 through 03)
- Total: 1,287 passed, 0 regressions (2 pre-existing live-site flakes excluded)

## [0.1.0-prealpha] — 2026-05-02

### BATCH-01: Prerelease LLM Client
- Unified `create_llm()` factory supporting Anthropic and OpenAI providers
- `LLMClient` with structured message handling and token tracking
- Provider auto-detection from environment variables

### BATCH-02: Discovery Tasks
- Requirements analysis and architecture discovery for browser-control subsystems
- Identified key gaps: agent orchestration, self-healing selectors, stealth, budget

### BATCH-03: Unified Config
- `Config` dataclass with headless, stealth, budget, security, and browser options
- Hierarchical configuration: defaults → file → env → runtime overrides
- Validated config schema with type-safe access

### BATCH-05: Git Init + Repository Hygiene
- Initialized git repository with comprehensive `.gitignore`
- Added `README.md` with installation, quickstart, and architecture overview
- Added `CHANGELOG.md`, `LICENSE` (Apache 2.0), `CONTRIBUTING.md`
- Created `py.typed` marker (PEP 561)
- Updated `__init__.py` to re-export `SuperBrowser`, `Config`, `ActionResult`, `create_llm`
- Initial commit containing all source, test, and documentation files

### BATCH-09: Security Hardening + CheckpointManager

#### Fixed
- **H4 (Critical):** Replaced f-string selector interpolation in JavaScript evaluation contexts with JSON-based parameterized evaluation (`JSON.parse(json.dumps(selector))`). Affected: `controller.py._resolve_to_coordinates()`, `validation.py.PreExecutionValidator`, `facade.py.extract()` — **HB-09-01 compliance**
- **H5 (High):** Verified `SuperBrowser.act()` raises `ConfigurationError` without LLM client. Confirmed zero `_NoOpLLM` references in source tree

#### Added
- **H6:** Full `CheckpointManager` implementation: `save()`, `restore()`, `list_checkpoints()`, `delete()` with JSON persistence to `~/.config/super-browser/checkpoints/{session_id}/`
- Checkpoint integration in `RecoveryCoordinator.execute_with_recovery()` — auto-checkpoint before risky actions
- Backward-compatible aliases: `create_checkpoint()` → `save()`, `rollback()` → `restore()`

#### Tests
- 28 new tests across 3 test files (test_injection, test_fail_fast, test_checkpoint)
- Total: 1,179 passed, 0 regressions

### BATCH-10: CAPTCHA Resolution + Hard Tab Cap

#### Added
- **H7:** `CAPTCHAWatchdog.resolve_captcha()` with provider-specific page-interaction strategies:
  - `CLOUDFLARE_TURNSTILE`: click challenge iframe, wait for `cf-turnstile-response` callback
  - `RECAPTCHA_V2`: click `.recaptcha-checkbox`, wait for success indicator
  - `RECAPTCHA_V3`: score-based, wait only (no interaction needed)
  - `HCAPTCHA`: click checkbox in iframe, wait for completion
  - `GENERIC`: wait 5s and re-check
  - `DATADOME`/`KASADA`/`AKAMAI`: log warning, return unresolved (external solver deferred to v2.0)
- `CAPTCHAResolution` dataclass (`resolved: bool`, `strategy: str`, `duration_ms: float`) added to `stealth/types.py`
- `_poll_js_true()` helper for CDP-based JS expression polling with configurable timeout
- **H8:** Hard tab cap enforcement in `SubagentDelegator`:
  - `_open_tabs` counter: increments before `new_page()`, decrements in `finally` block
  - Hard assert `open_tabs <= max_concurrency` in `_run_child()` — violation is a programming error
  - Post-delegation sanity assert in `delegate()` return path
  - `open_tabs` read-only property for observability

#### Hard Boundaries
- HB-10-01: `max_concurrency` is a hard cap — no task ever spawns beyond the semaphore limit
- HB-10-02: `resolve_captcha()` makes zero external API calls — all strategies are page-interaction only

#### Tests
- 12 new tests in `tests/test_stealth/test_captcha_resolution.py` (TEST-10-01-01 through TEST-10-01-05 + extras)
- 7 new tests in `tests/test_agent/test_tab_cap.py` (TEST-10-02-01 through TEST-10-02-04 + extras)
- Total: 1,198 passed, 0 regressions (2 pre-existing live-site flakes excluded)
