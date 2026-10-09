"""Core result envelope, error types, enums, and factory functions."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ActionMethod(StrEnum):
    """Which interaction tier produced this result."""
    SELECTOR = "selector"
    COORDINATE = "coordinate"
    VISION = "vision"


class ErrorCategory(StrEnum):
    """Fixed taxonomy of error kinds for programmatic recovery."""
    TIMEOUT = "timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NAVIGATION = "navigation"
    SECURITY = "security"
    BROWSER_CRASH = "browser_crash"
    VALIDATION = "validation"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"


class CompletionReason(StrEnum):
    """Why a delegated/composite action terminated."""
    SUCCESS = "success"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"
    CANCELLED = "cancelled"
    MAX_STEPS = "max_steps"


class SuccessCategory(StrEnum):
    """Machine-readable classification of a successful action's effect."""
    NAVIGATION = "navigation"      # page navigated to new URL
    MUTATION = "mutation"          # DOM changed without navigation
    INSPECTION = "inspection"      # read-only query (snapshot, eval)
    ARTIFACT = "artifact"          # screenshot/download produced
    UNCHANGED = "unchanged"        # action succeeded, no page change


class FailureCategory(StrEnum):
    """Refined failure taxonomy — strict superset of ErrorCategory.

    All 8 ErrorCategory values are present (identity mapping).
    5 additional members provide finer-grained recovery signals.
    """
    # -- Shared with ErrorCategory (identity mapping) --
    TIMEOUT = "timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NAVIGATION = "navigation"
    SECURITY = "security"
    BROWSER_CRASH = "browser_crash"
    VALIDATION = "validation"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"
    # -- FailureCategory-exclusive members --
    STALE_REF = "stale_ref"            # element ref expired, needs re-snapshot
    ELEMENT_OBSCURED = "element_obscured"  # element exists but covered by overlay
    FRAME_DETACHED = "frame_detached"      # iframe was removed during action
    AUTH_REQUIRED = "auth_required"        # login/auth wall encountered
    RATE_LIMITED = "rate_limited"          # server returned 429 or equivalent


# ---------------------------------------------------------------------------
# Core Envelope
# ---------------------------------------------------------------------------

@dataclass
class NextAction:
    """Pre-validated recovery hint attached to a failure result."""
    action_id: str           # e.g. "refresh_snapshot", "retry_with_selector"
    description: str         # Human-readable guidance
    compiled_args: Optional[dict[str, Any]] = None  # Pre-validated kwargs


@dataclass
class ActionError:
    category: ErrorCategory
    message: str
    selector: Optional[str] = None
    recoverable: bool = True
    retry_hint: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ActionError:
        d["category"] = ErrorCategory(d["category"])
        return cls(**d)


@dataclass(frozen=True)
class PageFingerprint:
    """Lightweight snapshot of page state for before/after comparison.

    Uses the same signals as the agent loop's _compute_page_fingerprint
    (URL, title, node_count, interactive_count) — no full DOM hash.
    """
    url: str
    title: str
    node_count: int
    interactive_count: int


@dataclass
class PageChangeSummary:
    """Structured description of what changed on the page after an action."""
    change_type: str      # "navigation", "mutation", "unchanged"
    summary: str          # Human-readable one-line description
    title: Optional[str] = None
    url: Optional[str] = None
    artifact_hint: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> PageChangeSummary:
        return cls(**d)


def compute_page_change(
    before: PageFingerprint,
    after: PageFingerprint,
    artifact_hint: Optional[str] = None,
) -> PageChangeSummary:
    """Compare two page fingerprints and return a PageChangeSummary.

    Detection order:
      1. URL changed → "navigation"
      2. node_count or interactive_count changed → "mutation"
      3. Otherwise → "unchanged"
    """
    if before.url != after.url:
        return PageChangeSummary(
            change_type="navigation",
            summary=f"Navigated to {after.url}",
            title=after.title,
            url=after.url,
            artifact_hint=artifact_hint,
        )
    if (
        before.node_count != after.node_count
        or before.interactive_count != after.interactive_count
    ):
        return PageChangeSummary(
            change_type="mutation",
            summary="DOM mutated",
            title=after.title,
            url=after.url,
            artifact_hint=artifact_hint,
        )
    return PageChangeSummary(
        change_type="unchanged",
        summary="No observable change",
        title=after.title,
        url=after.url,
        artifact_hint=artifact_hint,
    )


@dataclass
class ResultMeta:
    trace_id: str
    duration_ms: float
    method: Optional[ActionMethod] = None
    screenshot_hash: Optional[str] = None
    token_cost: float = 0.0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.method is not None:
            d["method"] = str(self.method)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ResultMeta:
        if "method" in d and d["method"] is not None:
            d["method"] = ActionMethod(d["method"])
        return cls(**d)


@dataclass
class ActionResult:
    """Standard envelope for every browser action.

    Invariants:
      - ok=True  => error is None
      - ok=False => error is not None
      - meta is always present
    """
    ok: bool
    data: Any = None
    # P2-review: historical call sites pass plain strings (e.g.
    # "vision_unavailable"); the declared type reflects that runtime
    # contract instead of silently mistyping them.
    error: Optional[ActionError | str] = None
    meta: ResultMeta = field(default_factory=lambda: ResultMeta(
        trace_id=str(uuid.uuid4()), duration_ms=0.0,
    ))
    result_category: Optional[str] = None         # "success" or "failure"
    success_category: Optional[SuccessCategory] = None
    failure_category: Optional[FailureCategory] = None
    next_actions: Optional[list[NextAction]] = None
    page_change_summary: Any = None  # placeholder for TASK-02

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def _error_to_dict(self) -> Optional[dict]:
        """Serialize the error to the canonical ActionError dict shape.

        Historical call sites store plain strings (e.g.
        "vision_unavailable"); those serialize as UNKNOWN-category errors so
        the shape stays consistent and the message stays redactable.
        """
        if not self.error:
            return None
        if isinstance(self.error, str):
            return {
                "category": ErrorCategory.UNKNOWN,
                "message": self.error,
                "selector": None,
                "recoverable": True,
                "retry_hint": None,
            }
        return self.error.to_dict()

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok,
            "data": _serialize_data(self.data),
            "error": self._error_to_dict(),
            "meta": self.meta.to_dict(),
        }
        d["result_category"] = self.result_category
        d["success_category"] = self.success_category.value if self.success_category else None
        d["failure_category"] = self.failure_category.value if self.failure_category else None
        d["next_actions"] = [na.__dict__ for na in self.next_actions] if self.next_actions else None
        d["page_change_summary"] = (
            self.page_change_summary.to_dict() if self.page_change_summary else None
        )
        from super_browser.security.action_redaction import redact_result_dict

        d = redact_result_dict(d)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> ActionResult:
        meta = ResultMeta.from_dict(d["meta"])
        raw_error = d.get("error")
        error = (
            ActionError.from_dict(raw_error)
            if isinstance(raw_error, dict)
            else (ActionError(ErrorCategory.UNKNOWN, str(raw_error)) if raw_error else None)
        )
        return cls(
            ok=d["ok"],
            data=d.get("data"),
            error=error,
            meta=meta,
            result_category=d.get("result_category"),
            success_category=SuccessCategory(d["success_category"]) if d.get("success_category") else None,
            failure_category=FailureCategory(d["failure_category"]) if d.get("failure_category") else None,
            next_actions=[NextAction(**na) for na in d["next_actions"]] if d.get("next_actions") else None,
            page_change_summary=(
                PageChangeSummary.from_dict(d["page_change_summary"])
                if d.get("page_change_summary")
                else None
            ),
        )

    # -- Convenience methods --

    def raise_for_error(self) -> None:
        """Raise if not ok — like requests.Response.raise_for_status().

        :raises RuntimeError: When ok is False, with error details.
        """
        if not self.ok and self.error:
            if isinstance(self.error, str):
                raise RuntimeError(self.error)
            raise RuntimeError(f"{self.error.category.value}: {self.error.message}")
        elif not self.ok:
            raise RuntimeError("Action failed with no error detail")

    def ok_or_raise(self) -> Any:
        """Return data if ok, raise if not.

        :returns: The data payload when ok is True.
        :raises RuntimeError: When ok is False.
        """
        self.raise_for_error()
        return self.data


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _resolve_trace_id() -> str:
    """Return trace_id from FlowLogger context, or a random UUID."""
    try:
        from super_browser.tracing.flow_logger import _current_context
        ctx = _current_context.get()
        if ctx is not None:
            return ctx.trace_id
    except Exception:
        pass
    return str(uuid.uuid4())

def action_result(
    ok: bool,
    data: Any = None,
    error: Optional[ActionError | str] = None,
    method: Optional[ActionMethod] = None,
    screenshot_hash: Optional[str] = None,
    token_cost: float = 0.0,
) -> ActionResult:
    """Convenience factory matching Hermes's jsonResult() pattern."""
    trace_id = _resolve_trace_id()
    result = ActionResult(
        ok=ok, data=data, error=error,
        meta=ResultMeta(
            trace_id=trace_id, duration_ms=0.0,
            method=method, screenshot_hash=screenshot_hash,
            token_cost=token_cost,
        ),
    )
    if ok:
        result.result_category = "success"
    else:
        result.result_category = "failure"
    return result


def timed_action_result(
    ok: bool,
    start_ns: float,
    data: Any = None,
    error: Optional[ActionError] = None,
    method: Optional[ActionMethod] = None,
    screenshot_hash: Optional[str] = None,
    token_cost: float = 0.0,
) -> ActionResult:
    """Factory that computes duration from a monotonic start timestamp."""
    duration_ms = (time.monotonic() - start_ns) * 1000
    trace_id = _resolve_trace_id()
    result = ActionResult(
        ok=ok, data=data, error=error,
        meta=ResultMeta(
            trace_id=trace_id, duration_ms=duration_ms,
            method=method, screenshot_hash=screenshot_hash,
            token_cost=token_cost,
        ),
    )
    if ok:
        result.result_category = "success"
    else:
        result.result_category = "failure"
    return result


def _serialize_data(data: Any) -> Any:
    """Serialize typed result payloads for JSON output."""
    if data is None:
        return None
    if isinstance(data, dict):
        return data
    try:
        from super_browser.results.typed import TYPED_RESULT_TYPES
        if isinstance(data, TYPED_RESULT_TYPES):
            return asdict(data)
    except ImportError:
        pass
    return str(data)
