"""Trace sinks — abstract interface and concrete implementations."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from super_browser.tracing.types import SpanKind, TraceEvent

if TYPE_CHECKING:
    from super_browser.tracing.session_db import SessionDB

import abc


class TraceSink(abc.ABC):

    @abc.abstractmethod
    async def emit(self, event: TraceEvent) -> None:
        ...

    @abc.abstractmethod
    async def flush(self) -> None:
        ...

    @abc.abstractmethod
    async def close(self) -> None:
        ...


_LEVEL_ORDER: dict[SpanKind, int] = {
    SpanKind.CUSTOM: 0,
    SpanKind.SESSION: 1,
    SpanKind.CDP: 2,
    SpanKind.LLM: 3,
    SpanKind.PAGE: 4,
    SpanKind.ACTION: 5,
    SpanKind.ERROR: 6,
}


class ConsoleSink(TraceSink):

    def __init__(self, *, min_level: SpanKind = SpanKind.ACTION) -> None:
        self._min_level = _LEVEL_ORDER.get(min_level, 0)

    async def emit(self, event: TraceEvent) -> None:
        if _LEVEL_ORDER.get(event.span_kind, 0) >= self._min_level:
            line = f"[TRACE] {event.span_kind} {event.name} {event.duration_ms:.1f}ms {event.status}\n"
            sys.stderr.write(line)

    async def flush(self) -> None:
        sys.stderr.flush()

    async def close(self) -> None:
        pass


class FileSink(TraceSink):

    def __init__(self, path: Path, *, buffer_size: int = 100) -> None:
        self._path = path
        self._buffer_size = buffer_size
        self._buffer: list[str] = []
        self._file: Any = None

    def _ensure_open(self) -> None:
        if self._file is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._path, "a", encoding="utf-8")

    async def emit(self, event: TraceEvent) -> None:
        self._ensure_open()
        self._buffer.append(event.to_json())
        if len(self._buffer) >= self._buffer_size:
            await self.flush()

    async def flush(self) -> None:
        if self._buffer and self._file is not None:
            for line in self._buffer:
                self._file.write(line + "\n")
            self._file.flush()
            self._buffer.clear()

    async def close(self) -> None:
        await self.flush()
        if self._file is not None:
            self._file.close()
            self._file = None


class SQLiteSink(TraceSink):

    def __init__(self, db: SessionDB) -> None:
        self._db = db
        self._buffer: list[TraceEvent] = []

    async def emit(self, event: TraceEvent) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= 50:
            await self.flush()

    async def flush(self) -> None:
        if self._buffer:
            self._db.insert_events(self._buffer)
            self._buffer.clear()

    async def close(self) -> None:
        await self.flush()


class PrometheusSink(TraceSink):

    def __init__(self, *, port: int = 9090) -> None:
        self._available = False
        self._metrics: dict[str, Any] = {}
        try:
            from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

            # Use a private registry so multiple instances don't collide
            # on the global default registry with "Duplicated timeseries".
            self._registry = CollectorRegistry()

            self._metrics["cdp_calls"] = Counter(
                "sb_cdp_calls_total", "Total CDP calls", ["method"],
                registry=self._registry,
            )
            self._metrics["cdp_duration"] = Histogram(
                "sb_cdp_call_duration_seconds", "CDP call duration",
                registry=self._registry,
            )
            self._metrics["llm_tokens"] = Counter(
                "sb_llm_tokens_used", "LLM tokens used", ["model"],
                registry=self._registry,
            )
            self._metrics["actions"] = Counter(
                "sb_actions_total", "Actions by tier", ["tier"],
                registry=self._registry,
            )
            self._metrics["errors"] = Counter(
                "sb_errors_total", "Errors by category", ["category"],
                registry=self._registry,
            )
            self._metrics["active_sessions"] = Gauge(
                "sb_active_sessions", "Active sessions",
                registry=self._registry,
            )
            self._available = True
        except ImportError:
            pass

    async def emit(self, event: TraceEvent) -> None:
        if not self._available:
            return
        if event.span_kind == SpanKind.CDP:
            self._metrics["cdp_calls"].labels(method=event.name).inc()
            self._metrics["cdp_duration"].observe(event.duration_ms / 1000)
        elif event.span_kind == SpanKind.LLM:
            model = event.attributes.get("model", "unknown")
            self._metrics["llm_tokens"].labels(model=model).inc(
                event.token_input + event.token_output
            )
        elif event.span_kind == SpanKind.ACTION:
            tier = event.attributes.get("tier", "unknown")
            self._metrics["actions"].labels(tier=tier).inc()
        elif event.span_kind == SpanKind.ERROR:
            category = event.attributes.get("category", "unknown")
            self._metrics["errors"].labels(category=category).inc()

    async def flush(self) -> None:
        # Prometheus is pull-based; no explicit push on flush.
        pass

    async def close(self) -> None:
        pass
