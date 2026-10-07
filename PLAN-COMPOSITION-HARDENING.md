# Composition-Hardening Program — Frozen 2026-10-07

> **Status:** Frozen execution order for all post-2.13.0 engineering work.
> **Baseline:** `9aff421` ("chore: prepare v2.13.0 release metadata"). The
> `v2.13.0` tag already points at this commit, so the baseline is preserved
> on the remote with no extra work.
> **Supersedes:** the post-2.4 scope in `docs/roadmap.md` for this program.
> That file remains untouched as the earlier planning record.
> **Companion to:** `PLAN-P2-MCP-DIAGNOSTICS.md` (completed P2 work).

This is a hardening program, not a rewrite. The subsystems exist and pass
tests in isolation. The defects are in the connections between them: the
facade composes subsystems incorrectly or not at all, and release gates do
not exercise the public paths end to end.

## Verified defects this program fixes

Four wiring defects, each verified against this working tree in the
2026-10-07 review. Line numbers refer to `9aff421`.

**1. Backend selection routes every non-Patchright backend to Patchright.**
`agent/facade.py:113-124` special-cases only `"patchright"`. The names
`"playwright"`, `"selenium"`, and `"cdp"` all fall into the `else` branch
and construct a `BrowserSession`, which is itself Patchright-based.
`Config(backend="playwright")` silently gives you Patchright.

**2. Budget governance is dead wiring, and user budget limits are ignored.**
`agent/loop.py:70` stores `self._budget_client` and never reads it again
anywhere in the loop, so the LLM the loop actually calls is ungoverned.
Separately, `facade.py:153-158` constructs `TokenBudgetGovernor()` bare
even though its constructor accepts a `config: BudgetConfig` parameter, and
grep for `cfg.budget` in `facade.py` returns zero matches. A user who sets
`BudgetConfig(daily_cap_usd=1.0)` silently gets the default $10 cap.

**3. Abort is unwired.** `facade.py:68` creates `self._abort_signal`,
`facade.py:1240` sets it in `abort()`, and `act()` never passes it to
`AgentLoop`. Calling `sb.abort()` during a run sets an event nothing reads.

**4. The published 2.13.0 vision runtime is broken at fresh start.**
`facade.py:614` calls `self._page.screenshot(format=...)`. At `start()`,
`_page` is a `PatchrightPage` whose `screenshot(**kwargs)` forwards verbatim
to the raw Playwright page (`patchright_backend.py:192-194`), and Playwright
takes `type=`, not `format=` — so the call raises `TypeError`. After the
first `open_tab()`/`switch_tab()`, `facade.py:831` replaces `_page` with a
`PageHandle`, which accepts `format=`, and the tools start working:

```text
extract_image_text (facade.py:671) and analyze_image (facade.py:785)
fail from start() until the first tab switch, then recover.
```

The daily post-release smoke checks installation and console scripts only,
so it cannot see a runtime failure of this shape. **Confirmed by execution
on 2026-10-07** (working tree at `9aff421`, headless Patchright): a fresh
`start()` then `_capture_region_bytes(format="png")` raised `TypeError:
Page.screenshot() got an unexpected keyword argument 'format'` on a
`PatchrightPage`; the identical call after one `open_tab()` (when `_page`
became a `PageHandle`) succeeded, returning `image/png`. The fresh-start
failure and the after-tab recovery are both reproduced facts.

## Frozen execution order

Order is load-bearing. Steps 1–3 precede any refactor.

1. **Clean baseline.** Commit the three unrelated maintenance fixes currently
   sitting uncommitted in the working tree, each as its own commit, plus this
   plan document. Human-authored and DCO-signed (`git commit -s`); the
   messages:

   ```text
   fix(tracing): isolate prometheus collector registry
   test(selenium): avoid deprecated event-loop lookup
   test(stealth): relax windows timer tolerance
   docs: add frozen composition-hardening program
   ```

   The required-review blocker is resolved (reviews requirement removed
   2026-10-07; linear history / enforce-admins / no-force-push remain and are
   compatible — squash merges).
   **Do NOT commit `tests/test_composition/` here** — its fresh-start gate
   is intentionally red on `9aff421`; it rides the 2.13.1 PR (see the
   release-gate section's commit choreography).
2. **Characterization tests before refactoring.** Capture current public
   behavior across backend selection, `wait_for`, diagnostics attach,
   screenshot capture, and at least one MCP path, through two real backends.
   Must include the fresh-start OCR capture on default Patchright (the
   release gate below). These tests are expected to expose the known
   failures; that is their job.
3. **Supersede #246 and ship 2.13.1.** Treat branch
   `fix/v2.13.1-real-vision-runtime` as source material: its merge base is
   `9aff421` (current `main` tip), so no rebase is needed. Provider hunks
   inspected 2026-10-07; #246 test file executed at `736f5f8`, 12/12 passed.
   Proceed with the superseding PR, retaining its smoke fixtures and
   OpenAI-compatible `response_format` fallback, shipping the release (the
   branch already bumps `pyproject.toml` to 2.13.1), and validating it with
   the browser-backed step-2 gate, not only by the branch's own scripts.
4. **PR 1: composition root + normalized page abstraction.** APPROVED
   PACKAGE (2026-10-07, items P1–P7; branch `feat/2.14-composition-root`
   from `ecd6576`).

   - **P1 — characterization before refactoring.** Factory-matrix and
     mode-detection tests, plus real-browser verticals for Patchright and
     Playwright Chromium (start → navigate → screenshot → action → tab →
     stop). Verified new composition bug on `ecd6576`:
     `_detect_backend()` (engine.py:290-296) matches uppercase
     `"PATCHRIGHT"`/`"CLOAK"` inside `str(mode)`, but `SessionMode` values
     are lowercase (`"patchright_launch"`, `"cloak_launch"`), so the mode
     check can never fire; import probing masks it by returning patchright
     anyway. Target-behavior tests carry `xfail(strict=False)` until P2
     lands, keeping every push green while preserving red→green evidence.
   - **P2 — one engine factory (`browser/factory.py`); the façade stops
     constructing `BrowserSession`.** patchright → PatchrightEngine;
     playwright → PlaywrightEngine; selenium → SeleniumEngine;
     cdp → CDPDirectEngine; cloak mode → PatchrightEngine + cloak
     configuration. Cloak resolved smaller than feared (verified):
     `PatchrightEngine.__init__` already accepts `cloak_config`
     (patchright_backend.py:270) and threads it into the `BrowserSession`
     it creates, whose `start()` handles `CLOAK_LAUNCH` through
     `CloakBrowserAdapter`. No `CloakEngine` is invented. Unsupported
     capability combinations fail explicitly.
   - **P3 — backend-neutral normalized page; AMENDED invariant.** Do NOT
     canonize `PageHandle`: its `engine_page` hard-codes `PatchrightPage`,
     so making every backend a `PageHandle` would reintroduce Patchright
     coupling under a new name. Introduce a normalized façade page (the
     name matters less than the contract) owning exactly:
     `engine_page` (any `EnginePage` implementation), `backend_page`
     (optional native page/driver), `cdp` (optional coordinate/CDP
     transport), and normalized screenshot semantics
     `screenshot(*, full_page=False, format="png", quality=None) -> bytes`.
     The existing xfail in `tests/test_composition/test_vision_capture_regression.py`
     is REWRITTEN to assert this contract after `start()` and after tab
     operations, and the xfail marker is removed in this PR.
   - **P4 — `MultimodalController` capability-aware, not CDP-assuming.**
     Selector actions operate on the normalized `EnginePage`; coordinate
     actions are included only when a coordinate/CDP transport exists. When
     absent, `_cascade()` records the tier as UNAVAILABLE — never executing
     it and catching `AttributeError` afterwards. Capability map:
     Patchright selector+coordinate; Playwright Chromium selector+coordinate;
     Selenium selector (coordinate only via a future adapter); CDP direct
     selector+coordinate.
   - **P5 — engine-page tab ownership replaces the raw `TabManager` path.**
     open_tab: `Engine.new_page()` → normalized page → stored under tab id.
     switch_tab: select the stored page, rebuild the controller from its
     capabilities. close_tab: `EnginePage.close()`. This removes raw
     Patchright/Playwright page storage and `_attach_page()`'s
     `context.new_cdp_session()` rebuild, leaving no second construction
     path that can drift.
   - **P6 — raw-page leaks are explicit acceptance criteria.** Diagnostics
     attachment, selector-region capture, MCP `wait_for`, controller
     construction, uploads, and tab management must close or be isolated
     behind the normalized adapter. Diagnostics keep native-event access
     inside the backend implementation; `SuperBrowser` itself never
     inspects which backend it holds.
   - **P7 — remove the 2.13.1 screenshot workaround.** The try-`type=`-
     catch-`TypeError` probe in `_capture_region_bytes` disappears once
     normalized screenshot semantics own the translation.

   Merge gates (all required before merge): factory matrix and
   SessionMode-detection tests green; NormalizedPage after `start()`,
   `open_tab()`, and `switch_tab()` with the xfail removed; real Patchright
   and Playwright Chromium verticals green; missing coordinate transport
   yields UNAVAILABLE, not an accidental fallback; vision regression stays
   green (fresh-start capture, OCR vertical on Ubuntu); façade never
   instantiates `BrowserSession` and never branches on backend name; the
   temporary `format=`/`type=` probe is gone; entire six-cell matrix plus
   DCO/lint/mypy green; stop on surprise.
5. **PR 2: budget truth + abort lifecycle.** Two budget defects: the facade
   constructs the governor with the user's actual `cfg.budget`, and the
   governed LLM sits in the real `AgentLoop` call chain
   (`create_plan`, `propose_action`, `replan`, streaming). Regression test
   uses a deliberately tiny configured cap and proves the configured cap —
   not the default $10 — is enforced. Merge or rename the two classes both
   named `BudgetAwareLLMClient` (`agent/llm/budget_aware.py` vs the budget
   package). Abort fix passes the facade signal into the loop with tested
   lifecycle semantics: abort an active run, verify termination, start
   another run, verify the stale signal does not abort it.
6. **Expand composition contracts.** A dedicated suite
   (`tests/test_composition/`) proving feature-flag combinations end to end:
   security on/off gates facade mutations, budget governs the loop's LLM,
   recovery routes through the coordinator, vision fallback connects,
   stealth policy evaluates at dispatch, lazy vision without the global
   flag, MCP default advertises 19 tools and refuses actions, MCP action
   mode advertises 31 with authorization active. Mock the engine only after
   verifying the correct engine class was selected.
7. **CI gates.** Fast gate (ruff, targeted mypy, unit), composition gate,
   browser-integration gate (Ubuntu/Chromium; Patchright + Playwright facade
   lifecycles, navigate/observe, click/verify, screenshot, dead-page MCP
   recovery), optional extended gate (Selenium/CDP, adversarial, live
   vision). Enforce `fail_under = 75` or delete the threshold — today the
   main CI job never runs `--cov`, so the gate is decorative. Expand mypy to
   composition-critical modules, staged: `agent/facade.py` and
   `agent/loop.py` first, `mcp_server.py` (~2,280 lines) last. Note the
   e2e/stress/benchmark workflows have zero runs in history: this gate is
   greenfield.
8. **Documentation derives from code.** Generate or validate MCP tool counts
   (19 default / 31 action), names, tiers, and schemas from
   `mcp_server.py`; add a consistency test that fails on stale counts or
   versions. Update `docs/architecture.md` (stamped v2.2.0). Fix the
   `superstl-browser-mcp` typos (`docs/mcp.md:45,243,246`). Publish the
   missing GitHub Releases (page shows Latest = v2.3.0 while PyPI serves
   2.13.0). Backend support tiers: continuously facade-tested / implemented
   and integration-tested / experimental. Mark old design docs
   `Status: Historical`.
9. **Audit only genuinely uncertain wiring.** Start from the known dead
   pair — `_budget_client` and the facade `_abort_signal` (both fixed in
   step 5) — then the two unwired islands: `DeterministicRouter`
   (`agent/router.py`, implemented, wired into nothing; the README
   "LLM falls through if unavailable" claim maps to it — wire or delete,
   and fix the README in the same change) and `security/gate.py`
   (`SafetyGate`, orphaned from the live pipeline). Known live, do not
   re-audit: `_stealth_manager` (loop.py:429-449), `_coordinator`
   (loop.py:230-240), `_memory_store` (loop.py:550-556), `_recorder`
   (EventBus, 5 lifecycle events), `_verifier` (facade.py:1356-1369).

## The 2.13.1 release gate

One test decides the hotfix:

```text
fresh SuperBrowser.start()
  → deterministic local page (HTML fixture with rendered text)
  → extract_image_text() must succeed
  → with NO intervening tab operation
```

**Step 2 is done (2026-10-07).** The gate is codified in
`tests/test_composition/test_vision_capture_regression.py` (untracked;
commit it with the 2.13.1 PR, not to main — it fails on 2.13.0 by design).
Split as planned: the browser-backed capture gate needs only Patchright;
the OCR vertical skips without a tesseract binary. Measured outcomes:

```text
on main (9aff421):        gate FAILED (the TypeError, reproduced),
                          after-tab PASSED, invariant XFAIL, OCR SKIPPED
on #246 fix (736f5f8):    gate PASSED, after-tab PASSED,
                          invariant XFAIL (correct — workaround, not the
                          invariant), OCR SKIPPED locally
```

That validates the #246 fix against a real browser, which its own 12
mock-based tests (all passing) do not do.

### Commit choreography (frozen 2026-10-07)

The gate test must never touch main before the fix. The split:

```text
A. On current main / baseline (9aff421)
   DCO-commit, separately:
     tracing/sinks.py
     tests/test_browser/test_selenium_backend.py
     tests/test_stealth/test_human.py
     PLAN-COMPOSITION-HARDENING.md
   Do NOT add tests/test_composition/ here (gate is red on this baseline).

B. Hotfix branch based on 736f5f8 (#246 fix branch)
   Add tests/test_composition/test_vision_capture_regression.py
   Run: #246's 12 tests + fresh-start capture gate + after-tab
   characterization + OCR vertical where Tesseract exists.

C. Open the superseding 2.13.1 PR
   CI must show the formerly-red fresh-start gate GREEN.
   Evidence property: the test is known to fail on 9aff421 and
   pass on 736f5f8 — it was executed both ways on 2026-10-07.

D. Squash or rebase merge (linear history is enforced), tag v2.13.1,
   publish through the existing trusted-publishing path.
```

### Tesseract in CI — verified gap

No workflow installs the Tesseract system binary (grep across
`.github/workflows/` returns nothing; the only `apt-get` uses are in
benchmark.yml and the never-run e2e.yml), and #246's diff touches no
workflow files. `pytesseract` the Python package ships via the `[vision]`
extra in `.[all]`, but the binary does not — so on current CI the OCR
vertical **skips on all six matrix cells** rather than fails. The hotfix
PR should add to the ubuntu cell(s) of `test.yml`:

```yaml
- if: runner.os == 'Linux'
  run: sudo apt-get update && sudo apt-get install -y tesseract-ocr
```

Until that lands, the OCR half of the gate runs only where a human
installed Tesseract; the capture half needs nothing but Patchright and is
the mandatory gate everywhere.

Provisioning detail (frozen): install **before the pytest step** and add a
sanity check so presence becomes a hard requirement, converting the OCR
vertical from "may silently skip" into an actual Ubuntu release gate. In
`test.yml`, immediately before the "Run tests" step:

```yaml
- if: runner.os == 'Linux'
  run: |
    sudo apt-get update
    sudo apt-get install -y tesseract-ocr
    tesseract --version   # fail the job if the binary is missing
```

macOS/Windows cells continue to skip the OCR vertical unless Tesseract is
intentionally provisioned there too. Acceptance evidence for the hotfix PR:

```text
9aff421   fresh-start gate → RED     (executed 2026-10-07)
736f5f8   fresh-start gate → GREEN   (executed 2026-10-07)
736f5f8   #246 mock tests   → 12/12  (executed 2026-10-07)
Ubuntu    OCR vertical      → must EXECUTE, not skip
```

## Release ladder

```text
2.13.1  vision runtime hotfix (step 3)
2.14.0  composition root + page normalization + budget/abort + contracts
2.14.1  compatibility fallout, if any
2.15.0  CI gates + generated docs + remaining cleanup
```

## Known-but-deferred defects (context, outside the frozen steps)

- Circuit breaker never receives `record_failure` through
  `BudgetCascadeClient.call` (`budget/client.py:104-108` reports 429/402 to
  the credential pool only; the breaker can close but never open).
- `ModelCascade.escalate` passes `cost_multiplier` as a USD estimate to the
  governor — a semantic mismatch with real prices.
- Budget state is in-memory by default (no `state_dir`), so caps reset on
  restart.
- `trace_enabled=True` is the only default-on feature flag; security,
  budget, recovery, vision, stealth, skills, and verification all default
  `False` (`config.py:154-162`). Documentation must say "available and
  configurable," never "on by default."

## Unverified at freeze

- ~~The `TypeError` at fresh start is signature reasoning, not an executed
  failure.~~ Resolved 2026-10-07: reproduced on a real browser (see defect 4).
- ~~`vision/providers.py` hunks of #246 are unread~~. Resolved 2026-10-07:
  read. The `analyze()` fix retries without `response_format=` when the
  server error names it, letting the controller's free-text fallback parse
  the answer; official OpenAI keeps strict JSON mode. Note: #246's 12 tests
  are all mock-based (fake page objects, mock OpenAI client) — the same
  test class that missed the bug in 2.13.0 — which is why this plan's gate
  is browser-backed.
- ~~#246's test file has been read but not executed.~~ Resolved 2026-10-07:
  executed in an isolated worktree at `736f5f8` — 12/12 passed. Caveat
  retained: these are mock-based tests, so the pass is necessary but not
  sufficient; the browser-backed gate is the sufficient check.
- Why the 2.13.0 suite missed the broken capture path remains a hypothesis
  (the after-tab recovery is now proven; whether earlier passing tests
  actually performed a tab operation before capturing is unconfirmed).
- The three uncommitted maintenance fixes pass their tests (42/42 on
  2026-10-07, with the fixes applied). They were not run without the fixes,
  so necessity was not independently demonstrated.
- ~~The two open GitWire lint PRs (#239, #244) are unresolved.~~ Resolved
  2026-10-07: both closed as superseded (their lint targets are ruff-clean
  on main since `d1f1dbf`/`261abb2`); the `gitwire/heal-*` branch refs were
  deleted. The one-review merge blocker was also removed the same day.
